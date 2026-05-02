"""
ARR Suite LLM - Media Enricher

Full pipeline per media item:
  1. Fetch rich metadata from TMDB (plot, cast, keywords, similar titles, ratings)
  2. Fetch AniList data for anime (themes, demographics, tags)
  3. Small fast LLM (configurable, default qwen2.5:3b) summarizes everything
     into a structured JSON profile
  4. nomic-embed-text generates the embedding
  5. Everything stored in ChromaDB + SQLite cache

The small LLM does ALL the synthesis work so the curator LLM never has to
guess or hallucinate facts about a title.
"""

import asyncio
import json
import logging
from typing import Optional, Any

import httpx

from src.config import settings
from src.cache.metadata_cache import MetadataCache

logger = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
# Small fast model for metadata summarization - change in .env as SUMMARIZER_MODEL
SUMMARIZER_MODEL = (
    getattr(settings, "SUMMARIZER_MODEL", None)
    or getattr(settings, "BASE_SUMMARIZER_MODEL", None)
    or "qwen2.5:3b"
)


# ── TMDB FULL FETCH ───────────────────────────────────────────────────────────

async def _tmdb_get(client: httpx.AsyncClient, path: str, params: dict = None) -> dict:
    """Single TMDB API call with error handling."""
    if not settings.TMDB_API_KEY:
        return {}
    p = {"api_key": settings.TMDB_API_KEY, "language": "en-US", **(params or {})}
    try:
        r = await client.get(f"https://api.themoviedb.org/3{path}", params=p)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug("TMDB %s error: %s", path, e)
    return {}


async def fetch_tmdb_full(tmdb_id: int, media_type: str = "movie") -> dict:
    """
    Fetch everything TMDB knows about a title in parallel:
    - Main details (plot, runtime, genres, rating, year)
    - Credits (top cast + director)
    - Keywords / tags
    - Similar titles
    - Videos (trailer available?)
    - External IDs (IMDb)
    """
    endpoint = "movie" if media_type in ("movie", "radarr") else "tv"

    async with httpx.AsyncClient(timeout=15) as client:
        details, credits, keywords, ext_ids = await asyncio.gather(
            _tmdb_get(client, f"/{endpoint}/{tmdb_id}", {"append_to_response": ""}),
            _tmdb_get(client, f"/{endpoint}/{tmdb_id}/credits"),
            _tmdb_get(client, f"/{endpoint}/{tmdb_id}/keywords"),
            _tmdb_get(client, f"/{endpoint}/{tmdb_id}/external_ids"),
        )

    if not details:
        return {}

    # Extract cast (top 8) and director
    cast = []
    director = None
    crew = credits.get("crew", [])
    for member in credits.get("cast", [])[:8]:
        cast.append(member.get("name", ""))
    for member in crew:
        if member.get("job") == "Director":
            director = member.get("name")
            break
    # For TV: creator instead of director
    if not director and endpoint == "tv":
        creators = details.get("created_by", [])
        if creators:
            director = creators[0].get("name")

    # Keywords / tags
    kw_list = keywords.get("keywords") or keywords.get("results", [])
    keyword_names = [k.get("name", "") for k in kw_list[:20]]

    # Genres
    genres = [g.get("name", "") for g in details.get("genres", [])]

    # Year
    date_str = details.get("release_date") or details.get("first_air_date") or ""
    year = int(date_str[:4]) if date_str else None

    # Rating
    vote_avg = details.get("vote_average")
    vote_count = details.get("vote_count")

    # Runtime
    runtime = details.get("runtime") or (
        details.get("episode_run_time", [None])[0]
        if details.get("episode_run_time") else None
    )

    # Seasons (TV)
    seasons = details.get("number_of_seasons")
    episodes = details.get("number_of_episodes")

    return {
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": details.get("title") or details.get("name", ""),
        "original_title": details.get("original_title") or details.get("original_name", ""),
        "year": year,
        "overview": details.get("overview", ""),
        "genres": genres,
        "cast": cast,
        "director": director,
        "keywords": keyword_names,
        "rating": vote_avg,
        "vote_count": vote_count,
        "runtime_min": runtime,
        "seasons": seasons,
        "episodes_total": episodes,
        "imdb_id": ext_ids.get("imdb_id"),
        "original_language": details.get("original_language"),
        "tagline": details.get("tagline", ""),
        "source": "tmdb",
    }


async def fetch_omdb_data(imdb_id: str) -> Optional[dict]:
    """
    Fetch additional metadata from OMDB (free, 1000 req/day).
    Returns plot, awards, Rotten Tomatoes score, Metacritic.
    Requires OMDB_API_KEY in .env — silently returns None if unavailable.
    """
    omdb_key = getattr(settings, "OMDB_API_KEY", None)
    if not omdb_key or not imdb_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get("https://www.omdbapi.com/", params={
                "apikey": omdb_key,
                "i": imdb_id,
                "plot": "full",
            })
        if r.status_code != 200:
            return None
        d = r.json()
        if d.get("Response") == "False":
            return None

        # Extract Rotten Tomatoes and Metacritic scores
        ratings = {}
        for rating in d.get("Ratings", []):
            src = rating.get("Source", "")
            val = rating.get("Value", "")
            if "Rotten Tomatoes" in src:
                ratings["rt"] = val
            elif "Metacritic" in src:
                ratings["metacritic"] = val.replace("/100", "")

        return {
            "plot_full": d.get("Plot", ""),
            "awards": d.get("Awards", ""),
            "ratings": ratings,
            "metacritic": d.get("Metascore"),
            "box_office": d.get("BoxOffice"),
            "writer": d.get("Writer", ""),
            "country": d.get("Country", ""),
            "language": d.get("Language", ""),
        }
    except Exception as e:
        logger.debug("OMDB error for %s: %s", imdb_id, e)
        return None


async def fetch_jikan_data(mal_id: int = None, title: str = None) -> Optional[dict]:
    """
    Fetch additional anime metadata from Jikan (MAL API proxy).
    Free, no key needed. Returns synopsis, genres, themes, demographics, score.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if mal_id:
                r = await client.get(f"https://api.jikan.moe/v4/anime/{mal_id}/full")
            elif title:
                r = await client.get("https://api.jikan.moe/v4/anime",
                    params={"q": title, "limit": 1, "type": "tv"})
                if r.status_code == 200:
                    results = r.json().get("data", [])
                    if not results:
                        return None
                    mal_id = results[0]["mal_id"]
                    r = await client.get(f"https://api.jikan.moe/v4/anime/{mal_id}/full")
            else:
                return None

            if r.status_code == 429:
                logger.debug("Jikan rate limited for '%s' — skipping supplement", title or mal_id)
                return None
            if r.status_code != 200:
                return None
        if not data:
            return None

        # Extract themes, demographics, explicit genres
        explicit_genres = [g["name"] for g in data.get("explicit_genres", [])]
        themes = [t["name"] for t in data.get("themes", [])]
        demographics = [d["name"] for d in data.get("demographics", [])]
        genres = [g["name"] for g in data.get("genres", [])]

        return {
            "mal_id": data.get("mal_id"),
            "synopsis": (data.get("synopsis") or "")[:800],
            "genres": genres,
            "themes": themes,
            "demographics": demographics,
            "explicit_genres": explicit_genres,
            "score": data.get("score"),
            "scored_by": data.get("scored_by"),
            "rank": data.get("rank"),
            "popularity": data.get("popularity"),
            "episodes": data.get("episodes"),
            "status": data.get("status"),
            "rating": data.get("rating"),  # e.g. "R - 17+ (violence & profanity)"
            "studios": [s["name"] for s in data.get("studios", [])],
            "source_material": data.get("source"),  # e.g. "Manga", "Light novel"
        }
    except Exception as e:
        logger.debug("Jikan error for %s/%s: %s", mal_id, title, e)
        return None


def _merge_raw_metadata(primary: dict, *supplements) -> dict:
    """
    Merge metadata from multiple sources into a richer raw profile.
    Primary source takes precedence. Supplements add/extend fields.
    Also derives tone hints from structural metadata to help LLM mood classification.
    """
    merged = dict(primary)
    all_keywords = list(primary.get("keywords", []))
    all_genres = list(primary.get("genres", []))
    extra_context = []
    tone_hints = []

    for sup in supplements:
        if not sup:
            continue

        # Merge keywords/tags — deduplicated
        for key in ("themes", "tags", "keywords", "explicit_genres"):
            for tag in (sup.get(key) or []):
                if tag and tag.lower() not in [k.lower() for k in all_keywords]:
                    all_keywords.append(tag)

        # Merge genres
        for g in (sup.get("genres") or []):
            if g and g not in all_genres:
                all_genres.append(g)

        # Use longer/better plot if available
        if sup.get("plot_full") and len(sup["plot_full"]) > len(merged.get("overview", "")):
            merged["overview_extended"] = sup["plot_full"]

        # Add synopsis if from Jikan and better than what we have
        if sup.get("synopsis") and len(sup["synopsis"]) > len(merged.get("overview", "")):
            merged["overview_extended"] = sup.get("overview_extended", "") or sup["synopsis"]

        # Append awards/ratings info as context
        if sup.get("awards") and sup["awards"] not in ("N/A", ""):
            extra_context.append(f"Awards: {sup['awards']}")
        if sup.get("ratings"):
            for src, val in sup["ratings"].items():
                extra_context.append(f"{src.upper()}: {val}")
        if sup.get("source_material"):
            extra_context.append(f"Source: {sup['source_material']}")
        if sup.get("rating"):  # MAL content rating
            extra_context.append(f"Rating: {sup['rating']}")
        if sup.get("demographics"):
            extra_context.append(f"Target audience: {', '.join(sup['demographics'])}")
            # Derive tone hints from demographics
            for demo in sup["demographics"]:
                if demo in ("Shounen", "Shoujo"):
                    tone_hints.append(f"{demo} demographic — typically optimistic/kinetic/adventurous")
                elif demo in ("Seinen", "Josei"):
                    tone_hints.append(f"{demo} demographic — can be mature/dark/complex")

        # Tone hints from MAL content rating
        mal_rating = sup.get("rating", "")
        if mal_rating:
            extra_context.append(f"Content rating: {mal_rating}")
            if "Rx" in mal_rating or "Hentai" in mal_rating:
                tone_hints.append("Adult/explicit content")
            elif "R+" in mal_rating:
                tone_hints.append("Mature content — likely intense/dark")
            elif "G" == mal_rating or "PG" in mal_rating:
                tone_hints.append("All-ages content — likely lighthearted/optimistic/comedic")

        # Tone hints from explicit genres (Comedy is a strong signal)
        all_explicit = sup.get("explicit_genres", []) + sup.get("genres", [])
        if "Comedy" in all_explicit and "Horror" not in all_explicit and "Psychological" not in all_explicit:
            if "Ecchi" in all_explicit or "Harem" in all_explicit:
                tone_hints.append("Ecchi comedy — primarily comedic/lighthearted despite adult themes")
            else:
                tone_hints.append("Comedy is a primary genre — mood should reflect comedic tone")

        # Fill in missing fields
        for field in ("studios", "score", "episodes", "mal_id", "rank",
                      "director", "writer", "country", "year", "popularity"):
            if sup.get(field) and not merged.get(field):
                merged[field] = sup[field]

    merged["keywords"] = list(dict.fromkeys(all_keywords))[:25]  # dedup, max 25
    merged["genres"] = list(dict.fromkeys(all_genres))
    if extra_context:
        merged["extra_context"] = " | ".join(extra_context)
    if tone_hints:
        merged["tone_hints"] = " | ".join(tone_hints)

    return merged

ANILIST_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {
    id
    title { romaji english native }
    description(asHtml: false)
    genres
    tags { name rank isMediaSpoiler }
    averageScore popularity
    episodes duration
    startDate { year }
    endDate { year }
    studios(isMain: true) { nodes { name } }
    staff(sort: RELEVANCE) { edges { role node { name { full } } } }
    recommendations(sort: RATING_DESC) { nodes { mediaRecommendation { title { romaji english } } } }
    demographics: tags(sort: RANK_DESC) { name rank category }
  }
}
"""


async def fetch_anilist_full(anilist_id: int) -> dict:
    """Fetch rich AniList data including tags, demographics, staff."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://graphql.anilist.co",
                json={"query": ANILIST_QUERY, "variables": {"id": anilist_id}},
            )
            if r.status_code != 200:
                return {}
            media = r.json().get("data", {}).get("Media")
            if not media:
                return {}
    except Exception as e:
        logger.debug("AniList %s error: %s", anilist_id, e)
        return {}

    title = media["title"].get("english") or media["title"].get("romaji", "")

    # Tags: filter spoilers, keep top 15 by rank
    tags = [
        t["name"] for t in sorted(
            [t for t in (media.get("tags") or []) if not t.get("isMediaSpoiler")],
            key=lambda t: t.get("rank", 0), reverse=True
        )[:15]
    ]

    # Staff: director/series director
    director = None
    for edge in (media.get("staff", {}).get("edges") or []):
        if "Director" in (edge.get("role") or ""):
            director = edge["node"]["name"]["full"]
            break

    # Recommendations
    similar = []
    for node in (media.get("recommendations", {}).get("nodes") or [])[:8]:
        rec = node.get("mediaRecommendation", {})
        t = rec.get("title", {})
        name = t.get("english") or t.get("romaji", "")
        if name:
            similar.append(name)

    studios = [s["name"] for s in (media.get("studios", {}).get("nodes") or [])]

    return {
        "anilist_id": anilist_id,
        "media_type": "anime",
        "title": title,
        "year": (media.get("startDate") or {}).get("year"),
        "overview": (media.get("description") or "").replace("<br>", "\n")[:1000],
        "genres": media.get("genres", []),
        "tags": tags,
        "director": director,
        "studios": studios,
        "rating": (media.get("averageScore") or 0) / 10,
        "episodes_total": media.get("episodes"),
        "runtime_min": media.get("duration"),
        "similar_titles": similar,
        "cast": [],
        "keywords": tags,
        "source": "anilist",
    }


# ── SMALL LLM SUMMARIZER ──────────────────────────────────────────────────────

SUMMARIZE_MUSIC_PROMPT = """[MODE: MUSIC METADATA STRUCTURING]
Produce a structured JSON profile for a music artist. Be precise — this drives recommendations.

ARTIST: {title}
GENRES: {genres}
TAGS: {tags}
BIO: {bio}
SIMILAR ARTISTS: {similar}
LISTENERS: {listeners}

MOOD REFERENCE — pick ONLY 1-3 moods dominant in this artist's work:
-- bleak: hopeless, nihilistic atmosphere
-- melancholic: sadness, longing, loss as primary emotional register
-- intense: sustained emotional or sonic pressure
-- kinetic: propulsive energy, high-tempo, danceable
-- darkly comedic: dark subjects treated with humor
-- unsettling: dissonant, disturbing, uncomfortable
-- contemplative: slow, introspective, philosophical
-- optimistic: uplifting, affirming, hopeful
-- cathartic: emotional release, purging
-- euphoric: transcendent highs, exhilarating
-- romantic: love and longing as primary driver
-- epic: grand scale, anthemic, sweeping
-- dreamlike: surreal, hypnagogic, hazy
-- nostalgic: memory-driven, retro feel
-- tense: suspenseful, on-edge
-- comedic: humor is primary
-- raw: unpolished, visceral, immediate

Output this exact JSON (no extra text, no markdown fences):
{{
  "title": "...",
  "media_type": "music",
  "genres": ["2-5 music genres"],
  "themes": ["3-6 lyrical or sonic themes — be specific, e.g. 'seafaring mythology', 'political rage', 'hedonism'"],
  "mood": ["1-3 from MOOD REFERENCE"],
  "artist_summary": "2-3 sentences. What defines this artist's sound and identity specifically.",
  "why_listen": "1 sentence — the single quality that sets them apart.",
  "keywords": ["8-12 descriptors: era, subgenre, instrumentation, vocal style, lyrical topics, cultural references"],
  "similar_artists": {similar_json},
  "rating": {rating},
  "embedding_text": "Dense text for semantic search: artist name, genres, themes, mood, similar artists, defining qualities."
}}"""

SUMMARIZE_PROMPT = """[MODE: METADATA STRUCTURING]
Produce a structured JSON profile. Be precise — this data drives recommendations for a real person.

TITLE: {title} ({year})
TYPE: {media_type}
GENRES: {genres}
TAGS/KEYWORDS: {keywords}
OVERVIEW: {overview}
EXTENDED INFO: {extra_context}
TONE HINTS (use to calibrate mood): {tone_hints}
CAST: {cast}
DIRECTOR/CREATOR: {director}
RATING: {rating}/10 ({votes} votes)

MOOD REFERENCE — pick ONLY 1-3 moods that are DOMINANT throughout the whole work:
- bleak: hopeless atmosphere, no redemption
- melancholic: the PRIMARY emotional register is sadness, loss, or longing
- intense: sustained PHYSICAL or PSYCHOLOGICAL pressure
- kinetic: propulsive energy, action-forward, fast pace
- darkly comedic: dark/taboo subjects ARE THE CORE SOURCE OF HUMOR
- unsettling: persistent dread, wrongness, psychological discomfort
- contemplative: genuinely slow and philosophical
- optimistic: genuinely hopeful, affirming tone
- cathartic: built around emotional release
- euphoric: exhilarating transcendent highs
- harrowing: deeply disturbing, demands emotional fortitude
- romantic: love/longing as primary emotional driver
- epic: grand mythic scale, sweeping stakes
- dreamlike: surreal logic, hypnagogic atmosphere
- nostalgic: emotional pull of the past, memory-driven
- tense: thriller-mode suspense, persistent threat
- comedic: humor is the primary mode
- informative: educational/documentary tone

Output this exact JSON (no extra text, no markdown fences):
{{
  "title": "...",
  "year": ...,
  "media_type": "...",
  "genres": [...],
  "themes": ["3-6 concrete thematic elements — be specific"],
  "mood": ["pick 2-3 from the MOOD REFERENCE above"],
  "plot_summary": "2-3 sentences. What makes this specific, not just the premise.",
  "why_watch": "1 sentence — the single defining quality that sets this apart from similar titles.",
  "keywords": ["10 precise descriptors: tone, setting, tropes, style, era, subgenre"],
  "cast_top3": [...],
  "director": "...",
  "rating": ...,
  "embedding_text": "2-4 sentences optimised for semantic curation. Act as an objective, highly critical analyst. Accurately describe the actual emotional experience, pacing, and visual style. Explicitly highlight if the execution is highly stylized, conceptually unique, or groundbreaking. Explicitly penalize it if it is a generic, derivative, or watered-down execution of its genres. CRITICAL RULE: Base your critique ONLY on the provided metadata. Do NOT invent tropes, do not call reality TV a psychological thriller, and do not hallucinate character dynamics that are not explicitly implied by the summary/tags. NO cast names."
}}"""


async def summarize_with_small_llm(raw_metadata: dict) -> Optional[dict]:
    """
    Use the small/fast summarizer model to create a structured profile.
    Falls back to a rule-based profile if Ollama is unavailable.
    """
    import json as _json

    if raw_metadata.get("media_type") == "music":
        similar = raw_metadata.get("similar_artists", [])
        prompt = SUMMARIZE_MUSIC_PROMPT.format(
            title=raw_metadata.get("title") or raw_metadata.get("name", "Unknown"),
            genres=", ".join(raw_metadata.get("genres", [])),
            tags=", ".join(raw_metadata.get("tags", [])[:15]),
            bio=(raw_metadata.get("bio", "") or "")[:500],
            similar=", ".join(similar[:8]),
            similar_json=_json.dumps(similar[:8]),
            listeners=raw_metadata.get("listeners") or "N/A",
            rating=raw_metadata.get("rating") or "N/A",
        )
    else:
        prompt = SUMMARIZE_PROMPT.format(
            title=raw_metadata.get("title", "Unknown"),
            year=raw_metadata.get("year", "Unknown"),
            media_type=raw_metadata.get("media_type", "movie"),
            genres=", ".join(raw_metadata.get("genres", [])),
            keywords=", ".join((raw_metadata.get("keywords") or raw_metadata.get("tags", []))[:20]),
            overview=(raw_metadata.get("overview_extended") or raw_metadata.get("overview", ""))[:800],
            extra_context=raw_metadata.get("extra_context", "N/A"),
            tone_hints=raw_metadata.get("tone_hints", "N/A"),
            cast=", ".join(raw_metadata.get("cast", [])[:5]),
            director=raw_metadata.get("director") or "Unknown",
            rating=raw_metadata.get("rating") or "N/A",
            votes=raw_metadata.get("vote_count") or "N/A",
        )

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{settings.effective_ollama}/api/chat",
                json={
                    "model": SUMMARIZER_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 1200},
                },
            )
        if r.status_code != 200:
            logger.warning("Summarizer HTTP %s for '%s' (model=%s)",
                           r.status_code, raw_metadata.get("title", "?"), SUMMARIZER_MODEL)
            return _rule_based_profile(raw_metadata)

        content = r.json().get("message", {}).get("content", "").strip()
        if not content:
            logger.debug("Summarizer returned empty content for %s", raw_metadata.get("title"))
            return _rule_based_profile(raw_metadata)

        # Strip markdown fences
        if "```" in content:
            parts = content.split("```")
            if len(parts) >= 2:
                content = parts[1]
                if content.startswith("json"):
                    content = content[4:]

        try:
            result = json.loads(content.strip())
            raw_source = raw_metadata.get("source", "unknown")
            result["source"] = f"{raw_source}+llm"
            logger.debug("Summarizer success for '%s': source=%s",
                         raw_metadata.get("title", "?"), result["source"])
            return result
        except json.JSONDecodeError as e:
            # Try to recover truncated JSON (num_predict cutoff mid-array)
            recovered = None
            try:
                partial = content.strip()
                for marker in ['",\n', '"\n', '],\n', ']\n', '",']:
                    pos = partial.rfind(marker)
                    if pos > 100:
                        try:
                            recovered = json.loads(partial[:pos + 1] + "\n}")
                            recovered["source"] = f"{raw_metadata.get('source', 'unknown')}+llm"
                            logger.debug("Recovered truncated JSON for '%s'", raw_metadata.get("title", "?"))
                            break
                        except json.JSONDecodeError:
                            continue
            except Exception:
                pass
            if recovered:
                return recovered
            logger.warning("LLM JSON parse failed for '%s': %s — raw: %s",
                           raw_metadata.get("title", "?"), e, content[:300])

    except httpx.TimeoutException:
        logger.warning("Summarizer timeout for '%s'", raw_metadata.get("title", "?"))
    except Exception as e:
        logger.warning("Summarizer LLM error for '%s': %s — %s",
                       raw_metadata.get("title", "?"), type(e).__name__, e)

    # Rule-based fallback
    return _rule_based_profile(raw_metadata)


def _rule_based_profile(m: dict) -> dict:
    """Deterministic profile when LLM is unavailable."""
    genres = m.get("genres", [])
    keywords = m.get("keywords") or m.get("tags", [])

    if m.get("media_type") == "music":
        similar = m.get("similar_artists", [])
        bio = (m.get("bio", "") or "")[:300]
        embedding_text = (
            f"{m.get('title', '')} — {', '.join(genres[:6])}. "
            f"Tags: {', '.join(keywords[:8])}. "
            + (f"Similar to: {', '.join(similar[:5])}. " if similar else "")
            + bio
        )
        return {
            "title": m.get("title", ""),
            "media_type": "music",
            "genres": genres,
            "themes": keywords[:6],
            "mood": [],
            "artist_summary": bio,
            "why_listen": "",
            "keywords": keywords[:12],
            "similar_artists": similar,
            "rating": m.get("rating"),
            "embedding_text": embedding_text,
            "source": f"{m.get('source', 'unknown')}:rule_based",
        }

    overview = m.get("overview", "")
    embedding_text = (
        f"{m.get('title', '')} ({m.get('year', '')}). "
        f"Genres: {', '.join(genres)}. "
        f"Tags: {', '.join(keywords[:10])}. "
        f"{overview[:300]}"
    )

    return {
        "title": m.get("title", ""),
        "year": m.get("year"),
        "media_type": m.get("media_type", "movie"),
        "genres": genres,
        "themes": keywords[:4],
        "mood": [],
        "audience": "",
        "plot_summary": overview[:300],
        "why_watch": "",
        "keywords": keywords[:10],
        "cast_top3": m.get("cast", [])[:3],
        "director": m.get("director"),
        "rating": m.get("rating"),
        "embedding_text": embedding_text,
        "source": f"{m.get('source', 'unknown')}:rule_based",
    }


# ── MAIN ENRICHMENT ENTRY POINT ───────────────────────────────────────────────

async def enrich_media_item(
    title: str,
    media_type: str = "movie",
    tmdb_id: Optional[int] = None,
    anilist_id: Optional[int] = None,
    anidb_id: Optional[int] = None,
    tvdb_id: Optional[int] = None,
    imdb_id: Optional[str] = None,
    plex_rating_key: Optional[str] = None,
    sonarr_series_type: Optional[str] = None,  # "anime"/"standard"/"daily" from Sonarr
) -> Optional[dict]:
    """
    Full pipeline for a single media item.
    Looks up MediaIdentity for all known IDs first, then picks
    the best API for each content type:
      - Anime: AniList (anilist_id) > AniDB lookup > title search
      - Movies: TMDB (tmdb_id) > title search
      - Shows: TMDB (tmdb_id) > TVDB > title search
    """
    cache = MetadataCache()

    # If we have a plex_rating_key, look up all known IDs from MediaIdentity
    if plex_rating_key and not all([tmdb_id, anilist_id]):
        try:
            from src.database.connection import get_db_session
            from src.database.models import MediaIdentity
            with get_db_session() as db:
                mi = db.query(MediaIdentity).filter(
                    MediaIdentity.plex_rating_key == plex_rating_key
                ).first()
                if mi:
                    tmdb_id    = tmdb_id    or mi.tmdb_id
                    anilist_id = anilist_id or mi.anilist_id
                    anidb_id   = anidb_id   or mi.anidb_id
                    tvdb_id    = tvdb_id    or mi.tvdb_id
                    imdb_id    = imdb_id    or mi.imdb_id
        except Exception:
            pass

    # Cache key: prefer stable IDs, fall back to title
    # Determine if this is anime first — needed for cache key and routing
    is_anime = (
        media_type == "anime"
        or sonarr_series_type == "anime"
        or (sonarr_series_type is None and _looks_like_anime(title))
    )

    id_key = anilist_id or anidb_id or tmdb_id or tvdb_id or title[:40]
    cache_key = f"enriched:{media_type}:{id_key}"
    cached = cache.get_cache(cache_key)
    if cached:
        cached_profile = cached.get("response", {})
        cached_source = cached_profile.get("source", "")
        # Use cache if:
        # 1. Has LLM structuring (source contains '+llm'), OR
        # 2. Cache is fresh (< 7 days old) — avoid re-hitting APIs unnecessarily
        from datetime import datetime
        cached_at_str = cached.get("created_at") or cached.get("cached_at")
        cache_age_days = 999
        if cached_at_str:
            try:
                cached_dt = datetime.fromisoformat(str(cached_at_str))
                cache_age_days = (datetime.utcnow() - cached_dt).days
            except Exception:
                pass
        if "+llm" in cached_source or cached_source == "llm" or cache_age_days < 7:
            cache.close()
            return cached_profile

    # For anime with tvdb_id: resolve via anime-lists crossref database
    # tvdb_id → anidb_id → AniList search (much more reliable than title search)
    if is_anime and tvdb_id and not anilist_id and not anidb_id:
        try:
            from src.services.anime_mapping import get_anime_mapping
            mapping = await get_anime_mapping()
            ids = mapping.lookup_tvdb(tvdb_id)
            if ids.get("anidb_id"):
                anidb_id = ids["anidb_id"]
                logger.debug("Resolved tvdb:%d → anidb:%d via anime-lists", tvdb_id, anidb_id)
            if ids.get("anilist_id"):
                anilist_id = ids["anilist_id"]
                logger.debug("Resolved tvdb:%d → anilist:%d via anime-lists", tvdb_id, anilist_id)
        except Exception as e:
            logger.debug("anime-lists lookup failed: %s", e)

    # ── MUSIC: fetch via MusicBrainz + Last.fm, then LLM-summarize ──────────
    if media_type == "music":
        from src.services.music_metadata import enrich_artist
        artist_raw = await enrich_artist(title)
        if not artist_raw:
            cache.close()
            return None
        artist_raw["title"] = artist_raw.get("name", title)
        artist_raw["media_type"] = "music"
        artist_raw["source"] = "musicbrainz+lastfm"
        # Run through LLM summarizer for structured genres/themes/mood/embedding_text
        profile = await summarize_with_small_llm(artist_raw)
        if not profile:
            cache.close()
            return None
        profile["source"] = "musicbrainz+lastfm+llm"
        profile["plex_rating_key"] = plex_rating_key
        cache.set_cache(cache_key, profile, days=30)
        # Generate embedding and store in ChromaDB
        try:
            from src.embeddings.embedding_generator import EmbeddingGenerator
            from src.vector_store.chromadb_wrapper import chroma_db
            text_to_embed = profile.get("embedding_text") or (
                f"{title} — {', '.join(profile.get('genres', [])[:6])}. "
                f"Tags: {', '.join(profile.get('keywords', [])[:8])}."
            )
            gen = EmbeddingGenerator()
            vec = await gen.generate_embedding(text_to_embed)
            if vec:
                doc_id = str(plex_rating_key or title)
                chroma_meta = {
                    "title": title,
                    "media_type": "music",
                    "genres": ", ".join(profile.get("genres", [])[:6]),
                    "themes": ", ".join(profile.get("themes", [])[:6]),
                    "mood": ", ".join(profile.get("mood", [])),
                    "year": 0,
                }
                try:
                    chroma_db.add_documents(
                        documents=[text_to_embed],
                        embeddings=[vec],
                        metadatas=[chroma_meta],
                        ids=[doc_id],
                    )
                except Exception:
                    chroma_db.update_metadata(doc_id, chroma_meta)
            await gen.close()
        except Exception as e:
            logger.debug("Music ChromaDB store failed for '%s': %s", title, e)
        cache.close()
        return profile

    raw = None
    # is_anime already defined above

    # 1. Fetch raw metadata — priority by ID quality, then content type
    if anilist_id:
        # Best case: direct AniList ID
        raw = await fetch_anilist_full(anilist_id)
    elif anidb_id and is_anime:
        # AniDB ID available — search AniList by title (no direct AniDB→AniList API)
        raw = await search_anilist_by_title(title)
        if raw:
            raw["anidb_id"] = anidb_id
            # Store discovered AniList ID back to mapping for future lookups
            if raw.get("id") and anidb_id:
                try:
                    from src.services.anime_mapping import update_anilist_id
                    update_anilist_id(anidb_id, raw["id"])
                except Exception:
                    pass
    elif tmdb_id and media_type == "movie":
        # Movie with TMDB ID — direct fetch
        raw = await fetch_tmdb_full(tmdb_id, "movie")
    elif tmdb_id and not is_anime:
        # TV series with TMDB ID — direct fetch
        raw = await fetch_tmdb_full(tmdb_id, "tv")
        # Check if it's actually anime (Sonarr may mis-classify)
        if raw and "Animation" in raw.get("genres", []) and _looks_like_anime(title):
            al = await search_anilist_by_title(title)
            if al:
                al["cast"] = raw.get("cast", [])
                al["similar_titles"] = al.get("similar_titles") or raw.get("similar_titles", [])
                raw = al
    elif is_anime:
        # Anime without direct ID — AniList title search
        raw = await search_anilist_by_title(title)
        if not raw and tmdb_id:
            raw = await fetch_tmdb_full(tmdb_id, "tv")
        if not raw:
            raw = await _tmdb_search_and_fetch(title, "tv")
    elif imdb_id:
        # Have IMDb ID — TMDB can find by external ID
        raw = await _tmdb_fetch_by_external_id(imdb_id, media_type)
        if not raw:
            endpoint = "movie" if media_type == "movie" else "tv"
            raw = await _tmdb_search_and_fetch(title, endpoint)
    elif tvdb_id:
        # Have TVDB ID — use IMDb search if available, else title search
        endpoint = "movie" if media_type == "movie" else "tv"
        raw = await _tmdb_search_and_fetch(title, endpoint)
    else:
        # Last resort: title search
        endpoint = "movie" if media_type in ("movie",) else "tv"
        if is_anime:
            raw = await search_anilist_by_title(title)
        if not raw:
            raw = await _tmdb_search_and_fetch(title, endpoint)

    if not raw:
        cache.close()
        return None

    # 1b. Supplement with additional sources
    supplements = []

    if is_anime:
        # Get MAL ID from raw data if AniList stored it
        mal_id_val = raw.get("mal_id")
        await asyncio.sleep(0.3)
        jikan_data = await fetch_jikan_data(
            mal_id=mal_id_val,
            title=None if mal_id_val else title,
        )
        if jikan_data:
            supplements.append(jikan_data)
            if jikan_data.get("mal_id") and not raw.get("mal_id"):
                raw["mal_id"] = jikan_data["mal_id"]
        elif not jikan_data:
            # No Jikan data — derive basic tone hints from what we have
            genres = raw.get("genres", [])
            tags = raw.get("tags", raw.get("keywords", []))
            tone_hints = []
            if "Comedy" in genres and "Horror" not in genres and "Psychological" not in genres:
                if any(t in tags for t in ["Ecchi", "Harem", "ecchi", "harem"]):
                    tone_hints.append("Ecchi comedy — primarily comedic/lighthearted")
                else:
                    tone_hints.append("Comedy is primary genre")
            if tone_hints:
                raw["tone_hints"] = " | ".join(tone_hints)
    else:
        imdb_id_val = raw.get("imdb_id") or imdb_id
        if imdb_id_val:
            omdb_data = await fetch_omdb_data(imdb_id_val)
            if omdb_data:
                supplements.append(omdb_data)

    # Merge all sources into enriched raw dict
    if supplements:
        raw = _merge_raw_metadata(raw, *supplements)

    raw["plex_rating_key"] = plex_rating_key

    # 2. Summarize with small LLM
    profile = await summarize_with_small_llm(raw)
    if not profile:
        cache.close()
        return None

    # Merge in IDs
    profile["tmdb_id"] = tmdb_id or raw.get("tmdb_id")
    profile["anilist_id"] = anilist_id or raw.get("anilist_id")
    profile["plex_rating_key"] = plex_rating_key

# 3. Cache result (30 days)
    cache.set_cache(cache_key, profile, days=30)

    # ─────────────────────────────────────────────────────────────────────────
    # 4 & 5. EMBEDDING GENERIEREN UND IN CHROMADB SPEICHERN
    # ─────────────────────────────────────────────────────────────────────────
    try:
        from src.embeddings.embedding_generator import EmbeddingGenerator
        from src.vector_store.chromadb_wrapper import chroma_db
        
        text_to_embed = profile.get("embedding_text")
        
        if text_to_embed:
            # Vektor mit nomic-embed-text generieren
            generator = EmbeddingGenerator()
            embedding_vector = await generator.generate_embedding(text_to_embed)
            
            if embedding_vector:
                # Metadaten für die ChromaDB-Suche aufbereiten
                chroma_metadata = {
                    "title": profile.get("title", ""),
                    "media_type": profile.get("media_type", "movie"),
                    "genres": ", ".join(profile.get("genres", [])),
                    "themes": ", ".join(profile.get("themes", [])),
                    "mood": ", ".join(profile.get("mood", [])),
                    "year": profile.get("year") or 0
                }
                
                # Eine eindeutige ID für das Dokument finden
                doc_id = str(plex_rating_key or tmdb_id or anilist_id or profile["title"])
                
                # In ChromaDB wegschreiben
                chroma_db.add_documents(
                    documents=[text_to_embed],
                    embeddings=[embedding_vector],
                    metadatas=[chroma_metadata],
                    ids=[doc_id]
                )
                logger.debug("Successfully stored '%s' in ChromaDB.", profile.get("title"))
                
            await generator.close()
    except Exception as e:
        logger.error("Failed to store '%s' in ChromaDB: %s", profile.get("title", "?"), e)

    cache.close()
    return profile


# ── HELPERS ───────────────────────────────────────────────────────────────────

ANIME_HINTS = {
    # Japanese name patterns or common suffixes/words
    "no", "wa", "ga", "wo", "ni", "de", "na", "to", "ka",  # particles
    "shonen", "shounen", "seinen", "shoujo", "isekai", "mecha",
    "nakama", "senpai", "sensei", "chan", "kun", "san", "sama",
    "hentai", "ecchi", "kawaii",
}

def _looks_like_anime(title: str) -> bool:
    """Heuristic: does this title look like it might be anime?"""
    if not title:
        return False
    lower = title.lower()
    words = set(lower.split())
    # Check for Japanese-style words or common anime title patterns
    if words & ANIME_HINTS:
        return True
    # Colon-heavy titles common in anime (e.g. "Re:Zero", "No Game: No Life")
    if ":" in title and len(title) < 60:
        return True
    return False


ANILIST_SEARCH_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
    id
    title { romaji english native }
    description(asHtml: false)
    genres
    tags { name rank isMediaSpoiler }
    averageScore popularity
    episodes duration
    startDate { year }
    studios(isMain: true) { nodes { name } }
    staff(sort: RELEVANCE) { edges { role node { name { full } } } }
    recommendations(sort: RATING_DESC) { nodes { mediaRecommendation { title { romaji english } } } }
  }
}
"""


async def search_anilist_by_title(title: str) -> Optional[dict]:
    """Search AniList by title, return full profile or None."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://graphql.anilist.co",
                json={"query": ANILIST_SEARCH_QUERY, "variables": {"search": title}},
            )
            if r.status_code == 429:
                # Rate limited — wait and retry once
                logger.debug("AniList rate limited for '%s' — retrying after 3s", title)
                await asyncio.sleep(3)
                r = await client.post(
                    "https://graphql.anilist.co",
                    json={"query": ANILIST_SEARCH_QUERY, "variables": {"search": title}},
                )
            if r.status_code != 200:
                return None
            if not media:
                return None
    except Exception as e:
        logger.debug("AniList search '%s' error: %s", title, e)
        return None

    found_title = media["title"].get("english") or media["title"].get("romaji", "")
    found_title_native = media["title"].get("native", "")

    # Validate match quality — reject if titles are too different
    def _title_similarity(a: str, b: str) -> float:
        """Word-overlap similarity, normalized."""
        if not a or not b:
            return 0.0
        a_words = set(a.lower().replace(":", "").replace("'", "").split())
        b_words = set(b.lower().replace(":", "").replace("'", "").split())
        stops = {"the", "a", "an", "of", "and", "in", "on", "at", "to", "for", "is", "no"}
        a_words -= stops
        b_words -= stops
        if not a_words or not b_words:
            return 0.0
        overlap = len(a_words & b_words)
        return overlap / max(len(a_words), len(b_words))

    def _titles_match(query: str, found_titles: list) -> bool:
        """Check if query title reasonably matches any of the found titles."""
        query_clean = query.lower().strip()
        query_words = [w for w in query_clean.replace(":", "").split()
                       if w not in {"the", "a", "an", "of"}]

        for found in found_titles:
            if not found:
                continue
            found_clean = found.lower().strip()

            # Exact match
            if query_clean == found_clean:
                return True

            # Query is substring of found or vice versa
            if query_clean in found_clean or found_clean in query_clean:
                return True

            # For multi-word queries: first word AND another word must match
            if len(query_words) >= 2:
                found_words = set(found_clean.replace(":", "").split())
                matches = sum(1 for w in query_words if w in found_words)
                if matches >= 2:  # at least 2 words match
                    return True
                # Overlap ratio
                if _title_similarity(query, found) >= 0.5:
                    return True
            else:
                # Single word: exact match only
                if _title_similarity(query, found) >= 0.8:
                    return True

        return False

    all_found_titles = [
        found_title,
        media["title"].get("romaji", ""),
        found_title_native,
        media["title"].get("english", ""),
    ]

    if not _titles_match(title, all_found_titles) and len(title.split()) >= 2:
        logger.debug(
            "AniList search '%s' → '%s' rejected (poor title match)",
            title, found_title
        )
        return None
    tags = [
        t["name"] for t in sorted(
            [t for t in (media.get("tags") or []) if not t.get("isMediaSpoiler")],
            key=lambda t: t.get("rank", 0), reverse=True
        )[:15]
    ]
    director = None
    for edge in (media.get("staff", {}).get("edges") or []):
        if "Director" in (edge.get("role") or ""):
            director = edge["node"]["name"]["full"]
            break
    similar = []
    for node in (media.get("recommendations", {}).get("nodes") or [])[:8]:
        rec = node.get("mediaRecommendation", {})
        t = rec.get("title", {})
        name = t.get("english") or t.get("romaji", "")
        if name:
            similar.append(name)
    studios = [s["name"] for s in (media.get("studios", {}).get("nodes") or [])]

    return {
        "anilist_id": media["id"],
        "media_type": "anime",
        "title": found_title,
        "year": (media.get("startDate") or {}).get("year"),
        "overview": (media.get("description") or "").replace("<br>", "\n")[:1000],
        "genres": media.get("genres", []),
        "tags": tags,
        "director": director,
        "studios": studios,
        "rating": (media.get("averageScore") or 0) / 10,
        "episodes_total": media.get("episodes"),
        "runtime_min": media.get("duration"),
        "similar_titles": similar,
        "cast": [],
        "keywords": tags,
        "source": "anilist",
    }


async def _tmdb_search_and_fetch(title: str, endpoint: str) -> Optional[dict]:
    """Search TMDB by title and fetch full details."""
    if not settings.TMDB_API_KEY:
        return None
    async with httpx.AsyncClient(timeout=10) as client:
        r = await _tmdb_get(client, f"/search/{endpoint}", {"query": title})
    results = r.get("results", [])
    if not results:
        return None
    found_id = results[0].get("id")
    if not found_id:
        return None
    media_type = "movie" if endpoint == "movie" else "tv"
    return await fetch_tmdb_full(found_id, media_type)


async def _tmdb_fetch_by_external_id(imdb_id: str, media_type: str) -> Optional[dict]:
    """Fetch TMDB item using an external IMDb ID — more reliable than title search."""
    if not settings.TMDB_API_KEY or not imdb_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await _tmdb_get(client, f"/find/{imdb_id}",
                                {"external_source": "imdb_id"})
        results = (
            r.get("movie_results", []) if media_type == "movie"
            else r.get("tv_results", [])
        )
        if not results:
            # Try the other type
            results = r.get("tv_results", []) if media_type == "movie" else r.get("movie_results", [])
        if not results:
            return None
        found_id = results[0].get("id")
        fetch_type = "movie" if media_type == "movie" else "tv"
        return await fetch_tmdb_full(found_id, fetch_type)
    except Exception as e:
        logger.debug("TMDB external ID lookup failed for %s: %s", imdb_id, e)
        return None
