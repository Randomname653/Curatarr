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
import re
import time
from typing import Optional, Any

import httpx

from src.config import settings
from src.cache.metadata_cache import MetadataCache
from src.services.llm_utils import clean_llm_text, strip_think_tags, ollama_options

logger = logging.getLogger(__name__)


# Pass 99-fu2: prompt version tag for the two-tier cache split.
#
# BUMP THIS WHENEVER the curator or summariser system prompts change in a
# way that should invalidate previously-polished profiles. The next
# enrichment run after the bump will:
#   - skip any ``enriched:*`` cache entry whose ``prompt_version`` doesn't
#     match (treats it as "needs re-polish")
#   - reuse the matching ``raw:*`` cache entry (no API re-fetch) and feed
#     it straight to the consumer's LLM polish
#
# A bump costs ~LLM-summarizer-throughput × library-size — i.e. for a
# 30k-item library at ~5 s/item, roughly 40 hours of LLM compute, but
# ZERO new API calls. The raw cache absorbs the API cost across bumps.
#
# Format: opaque short string. Bump format is "v{n}" for clarity but
# anything that changes the literal counts as a bump.
_PROMPT_VERSION = "v1"

# Pass 99-fu2: tier-2 raw cache TTL. Long enough that prompt bumps don't
# trigger API re-fetches; short enough that genuinely-changed upstream
# metadata (a TMDB rewrite, a Last.fm artist merge) still gets refreshed.
_RAW_CACHE_DAYS = 90


def _write_raw_cache(cat: str, id_key, raw_dict: dict, days: int = _RAW_CACHE_DAYS) -> None:
    """Persist a fresh API fetch under raw:{cat}:{id_key} for reuse.

    Best-effort: any cache failure is logged at debug and swallowed so a
    flaky cache write can't break the producer's hand-off to the consumer.
    Strips underscore-prefixed transport fields before writing — those
    are caller-instance specific (plex_rating_key, _cache_key, etc.)
    and get re-injected on read in ``fetch_and_prepare_raw``.
    """
    if not id_key:
        return
    try:
        c = MetadataCache()
        try:
            cleaned = {k: v for k, v in raw_dict.items() if not str(k).startswith("_")}
            c.set_cache(f"raw:{cat}:{id_key}", cleaned, days=days)
        finally:
            c.close()
    except Exception as e:
        logger.debug("[enricher] raw-cache write failed for raw:%s:%s — %s",
                     cat, id_key, e)


# ── Pass 99-fu3: Per-service concurrency caps for the parallel producer ─────
#
# Sized to stay well within each service's published rate limits even with
# the bulk producer running N workers in parallel. Anything more would risk
# bursts that trigger 429s (which we now handle gracefully via Pass 99,
# but better to never trigger them in the first place).
#
# MusicBrainz already has its own ``_MB_SEM = asyncio.Semaphore(1)`` in
# ``src/services/music_metadata.py`` — not duplicated here.
# Last.fm uses a 250 ms inter-call sleep in music_matcher; 4 concurrent
# callers stay under its 5 req/sec cap with margin to spare.
_SEM_TMDB:     "asyncio.Semaphore | None" = None  # 16 — TMDB has no published cap on free tier; 16 generous
_SEM_OMDB:     "asyncio.Semaphore | None" = None  # 4 — OMDb 1000/day; 4 concurrent safe
_SEM_JIKAN:    "asyncio.Semaphore | None" = None  # 2 — Jikan 3 req/sec; 2 leaves headroom
_LOCK_ANILIST: "asyncio.Lock | None"      = None  # serialises AniList; _anilist_wait isn't reentrant-safe


def _ensure_concurrency_primitives() -> None:
    """Lazy-init the module-level semaphores/locks on first use.

    They CAN be created at module import in modern Python, but lazy-init
    keeps test imports + tools that import this module outside an event
    loop safe (Semaphore() still works without a loop, but Lock() in
    some older patches did not — defensive).
    """
    global _SEM_TMDB, _SEM_OMDB, _SEM_JIKAN, _LOCK_ANILIST
    if _SEM_TMDB is None:
        _SEM_TMDB     = asyncio.Semaphore(16)
        _SEM_OMDB     = asyncio.Semaphore(4)
        _SEM_JIKAN    = asyncio.Semaphore(2)
        _LOCK_ANILIST = asyncio.Lock()


# ── ANILIST RATE-LIMIT CIRCUIT BREAKER ────────────────────────────────────────
# AniList allows 90 req/min. We enforce a 0.75 s floor (~80/min) and honour
# any Retry-After header from 429 responses via a shared module-level backoff.
# Pass 99-fu3: the wait function is now wrapped in ``_LOCK_ANILIST`` so
# parallel producer workers serialise correctly through it.

_anilist_backoff_until: float = 0.0   # monotonic timestamp; 0 = not backed off
_anilist_last_req: float = 0.0        # monotonic timestamp of last request sent
_ANILIST_MIN_INTERVAL: float = 0.75   # seconds between requests


async def _anilist_wait() -> None:
    """Block until AniList is no longer backed off and the minimum interval has passed.

    Pass 99-fu3: held under ``_LOCK_ANILIST`` so concurrent producer
    workers serialise through the throttle. The pre-fu3 implementation
    read + wrote ``_anilist_last_req`` without a lock, so N parallel
    callers could all pass the ``since_last < interval`` check at the
    same time and burst-request — triggering the very 429s the throttle
    was meant to avoid.
    """
    _ensure_concurrency_primitives()
    async with _LOCK_ANILIST:
        global _anilist_last_req
        now = time.monotonic()
        # Honour circuit-breaker backoff first
        backoff_wait = _anilist_backoff_until - now
        if backoff_wait > 0:
            logger.info("AniList circuit-breaker: sleeping %.0fs", backoff_wait)
            await asyncio.sleep(backoff_wait)
        # Enforce minimum per-request spacing
        since_last = time.monotonic() - _anilist_last_req
        if since_last < _ANILIST_MIN_INTERVAL:
            await asyncio.sleep(_ANILIST_MIN_INTERVAL - since_last)
        _anilist_last_req = time.monotonic()


def _anilist_set_backoff(headers) -> float:
    """Parse Retry-After from a 429 response and set the module-level backoff.

    Returns the number of seconds we will wait.
    """
    global _anilist_backoff_until
    raw = headers.get("retry-after") or headers.get("Retry-After") or ""
    try:
        seconds = max(float(raw), 1.0) if raw else 60.0
    except ValueError:
        seconds = 60.0
    seconds += 3.0  # small safety buffer
    _anilist_backoff_until = time.monotonic() + seconds
    logger.warning("AniList 429 — circuit-breaker active for %.0fs", seconds)
    return seconds

# ── CONFIG ────────────────────────────────────────────────────────────────────
# Small fast model for metadata summarization - change in .env as SUMMARIZER_MODEL
SUMMARIZER_MODEL = (
    getattr(settings, "SUMMARIZER_MODEL", None)
    or getattr(settings, "BASE_SUMMARIZER_MODEL", None)
    or "qwen2.5:3b"
)


# ── TMDB FULL FETCH ───────────────────────────────────────────────────────────

class TMDBTransientError(Exception):
    """Pass 99: raised by ``_tmdb_get`` for 429 / 5xx / network failures.

    The pre-99 silent ``return {}`` on any non-200 was indistinguishable
    from a legitimate "TMDB has no record of this title" — the caller
    chain then collapsed to ``raw=None`` and the producer wrote a
    ``not_found`` sentinel. A short TMDB outage during a bulk run could
    poison thousands of rows with bogus not_found sentinels in minutes.

    Raising on transient errors lets the producer (a) sleep for the
    Retry-After window, (b) skip the item without writing a sentinel —
    the next enrichment run picks it up cleanly when TMDB recovers.

    ``retry_after_s`` carries the server's Retry-After header value when
    present (in seconds), or a sensible default the producer can use.
    """
    def __init__(self, status_code: int, retry_after_s: float = 5.0,
                 path: str = "", body_snippet: str = ""):
        self.status_code = status_code
        self.retry_after_s = retry_after_s
        self.path = path
        self.body_snippet = body_snippet
        super().__init__(f"TMDB {path} → HTTP {status_code} (retry in {retry_after_s}s)")


async def _tmdb_get(client: httpx.AsyncClient, path: str, params: dict = None) -> dict:
    """Single TMDB API call with error handling.

    Returns:
      - ``{}`` only when TMDB_API_KEY isn't configured (caller skips TMDB entirely).
      - ``r.json()`` on HTTP 200 (may be ``{"results": []}`` for a legitimate miss).

    Raises:
      - ``TMDBTransientError`` on 429 (rate-limit), 5xx (server error),
        or any network-level exception. Caller should back off and retry,
        NOT treat as "title not findable".
      - On 4xx other than 429 (e.g. 404 for a stale movie ID): returns ``{}``.
        That's a real "no data" answer — caller is welcome to write a
        not_found sentinel.
    """
    if not settings.TMDB_API_KEY:
        return {}
    _ensure_concurrency_primitives()
    p = {"api_key": settings.TMDB_API_KEY, "language": "en-US", **(params or {})}
    async with _SEM_TMDB:   # Pass 99-fu3: cap parallel TMDB calls (16 concurrent)
        try:
            r = await client.get(f"https://api.themoviedb.org/3{path}", params=p)
        except Exception as e:
            # Network-level failure (DNS, connection reset, timeout). Always
            # treat as transient — the next run may well succeed.
            raise TMDBTransientError(0, retry_after_s=5.0, path=path,
                                     body_snippet=f"network: {type(e).__name__}: {e}")

    if r.status_code == 200:
        return r.json()

    if r.status_code == 429 or 500 <= r.status_code < 600:
        # Honour Retry-After if present; else back off conservatively.
        # TMDB usually returns it as integer seconds; sometimes HTTP-date,
        # which we don't parse — fall back to 5s in that case.
        ra_raw = r.headers.get("retry-after", "")
        try:
            ra = float(ra_raw) if ra_raw else 5.0
        except (TypeError, ValueError):
            ra = 5.0
        ra = max(1.0, min(ra, 120.0))   # clamp to [1s, 2min] sane window
        raise TMDBTransientError(r.status_code, retry_after_s=ra, path=path,
                                 body_snippet=r.text[:120])

    # 4xx other than 429 — real "not found" / "bad request". Caller can
    # interpret an empty result as "TMDB really doesn't have this".
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
    _ensure_concurrency_primitives()
    try:
        async with _SEM_OMDB, httpx.AsyncClient(timeout=8) as client:   # Pass 99-fu3: cap 4 concurrent
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
            "source": "omdb",
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
    _ensure_concurrency_primitives()
    try:
        async with _SEM_JIKAN, httpx.AsyncClient(timeout=10) as client:   # Pass 99-fu3: cap 2 concurrent
            if mal_id:
                r = await client.get(f"https://api.jikan.moe/v4/anime/{mal_id}/full")
            elif title:
                r = await client.get("https://api.jikan.moe/v4/anime",
                    params={"q": title, "limit": 8})
                if r.status_code == 200:
                    results = r.json().get("data", [])
                    if not results:
                        return None
                    # Validate title match before using any result
                    matched = None
                    for result in results:
                        candidate_titles = [
                            result.get("title", ""),
                            result.get("title_english", ""),
                            result.get("title_japanese", ""),
                        ]
                        if _titles_close_enough(title, candidate_titles):
                            matched = result
                            break
                    if not matched:
                        logger.debug(
                            "Jikan title search '%s' → no close match in top %d results, skipping",
                            title, len(results)
                        )
                        return None
                    mal_id = matched["mal_id"]
                    r = await client.get(f"https://api.jikan.moe/v4/anime/{mal_id}/full")
            else:
                return None

            if r.status_code == 429:
                logger.debug("Jikan rate limited for '%s' — skipping supplement", title or mal_id)
                return None
            if r.status_code != 200:
                return None
            data = r.json().get("data", {})
        if not data:
            return None

        # Extract themes, demographics, explicit genres
        explicit_genres = [g["name"] for g in data.get("explicit_genres", [])]
        themes = [t["name"] for t in data.get("themes", [])]
        demographics = [d["name"] for d in data.get("demographics", [])]
        genres = [g["name"] for g in data.get("genres", [])]

        return {
            "source": "mal",
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


_ADULT_TAGS = {
    "bdsm", "ecchi", "nudity", "sexual content", "fan service", "fanservice",
    "hentai", "erotica", "explicit sex", "sexual violence", "rape",
    "exhibitionism", "voyeurism", "bondage", "sadomasochism",
}
_DARK_TAGS = {
    "gore", "body horror", "graphic violence", "torture", "war crimes",
    "psychological horror", "disturbing", "death game", "survival game",
}
_SUBVERSION_TAGS = {
    "villain protagonist", "antihero", "dark magical girl",
    "magical girl subversion", "deconstruction",
}


def _merge_raw_metadata(primary: dict, *supplements) -> dict:
    """
    Merge metadata from multiple sources into a richer raw profile.
    Primary source takes precedence. Supplements add/extend fields.
    Collects ALL plot descriptions across sources (instead of picking the longest)
    so the LLM receives the full picture. Derives tone hints from structural
    metadata (demographics, content rating, explicit tags) to guide mood output.
    """
    merged = dict(primary)
    all_keywords = list(primary.get("keywords", []))
    all_genres = list(primary.get("genres", []))
    extra_context = []
    tone_hints = []

    # Track all distinct plot descriptions by source name
    plot_sources: dict[str, str] = {}
    primary_overview = (primary.get("overview") or "").strip()
    if primary_overview:
        plot_sources["primary"] = primary_overview

    for sup in supplements:
        if not sup:
            continue

        # Collect supplement plot texts under their source label
        sup_source = sup.get("source", "supplement")
        for plot_field in ("synopsis", "plot_full", "overview"):
            text = (sup.get(plot_field) or "").strip()
            if text and text != primary_overview and text not in plot_sources.values():
                plot_sources[sup_source] = text
                break

        # Merge keywords/tags — deduplicated
        for key in ("themes", "tags", "keywords", "explicit_genres"):
            for tag in (sup.get(key) or []):
                if tag and tag.lower() not in [k.lower() for k in all_keywords]:
                    all_keywords.append(tag)

        # Merge genres
        for g in (sup.get("genres") or []):
            if g and g not in all_genres:
                all_genres.append(g)

        # Append awards/ratings info as context
        if sup.get("awards") and sup["awards"] not in ("N/A", ""):
            extra_context.append(f"Awards: {sup['awards']}")
        if sup.get("ratings"):
            for src, val in sup["ratings"].items():
                extra_context.append(f"{src.upper()}: {val}")
        if sup.get("source_material"):
            extra_context.append(f"Source: {sup['source_material']}")
        if sup.get("demographics"):
            extra_context.append(f"Target audience: {', '.join(sup['demographics'])}")
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
                tone_hints.append("Adult/explicit sexual content — do not sanitize in summary")
            elif "R+" in mal_rating:
                tone_hints.append("Mature content (R+) — likely intense/dark/adult themes")
            elif mal_rating in ("G", "PG"):
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

    # Tone hints from tags across ALL sources (primary + supplements)
    all_tags_lower = {t.lower() for t in all_keywords}
    matched_adult = all_tags_lower & _ADULT_TAGS
    matched_dark = all_tags_lower & _DARK_TAGS
    matched_subversion = all_tags_lower & _SUBVERSION_TAGS

    if matched_adult:
        tag_list = ", ".join(sorted(matched_adult))
        tone_hints.append(
            f"Explicit/adult tags present: [{tag_list}] — describe these accurately, do not sanitize"
        )
    if matched_dark:
        tag_list = ", ".join(sorted(matched_dark))
        tone_hints.append(f"Dark/disturbing content tags: [{tag_list}]")
    if matched_subversion:
        tag_list = ", ".join(sorted(matched_subversion))
        tone_hints.append(f"Subversive premise tags: [{tag_list}] — make the subversion explicit in plot_summary")

    # Build overview_extended from the longest available plot text
    best_plot = max(plot_sources.values(), key=len) if plot_sources else ""
    if best_plot and best_plot != primary_overview:
        merged["overview_extended"] = best_plot

    # Store all alternative plot descriptions for the LLM
    alt_plots = {k: v for k, v in plot_sources.items() if k != "primary" and v != best_plot}
    if alt_plots:
        merged["alt_plot_sources"] = alt_plots

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
    averageScore meanScore popularity
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
        await _anilist_wait()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://graphql.anilist.co",
                json={"query": ANILIST_QUERY, "variables": {"id": anilist_id}},
            )
            if r.status_code == 429:
                _anilist_set_backoff(r.headers)
                return {}
            if r.status_code != 200:
                return {}
            media = r.json().get("data", {}).get("Media")
            if not media:
                return {}
    except Exception as e:
        logger.debug("AniList %s error: %s", anilist_id, e)
        return {}

    # Pass 56: AniList can return ``title: null`` on some entries. dict.get
    # with a default only substitutes when the KEY is missing — a present-
    # but-null value passes straight through, so ``media["title"].get(...)``
    # crashed with "'NoneType' object has no attribute 'get'". ``or {}``
    # catches the null case too.
    _mt = media.get("title") or {}
    title = _mt.get("english") or _mt.get("romaji", "")

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
        # Pass 56: ``or {}`` (not a .get default) — AniList may send
        # mediaRecommendation: null or title: null on sparse entries.
        rec = node.get("mediaRecommendation") or {}
        t = rec.get("title") or {}
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
        # Pass 54: averageScore is AniList's weighted score — it stays null
        # for fresh / niche titles that haven't cleared the confidence
        # threshold yet. meanScore (plain average) is populated earlier, so
        # fall back to it before giving up. Either way it's a real user
        # rating; 0 only if AniList genuinely has neither.
        "rating": (media.get("averageScore") or media.get("meanScore") or 0) / 10,
        "episodes_total": media.get("episodes"),
        "runtime_min": media.get("duration"),
        "similar_titles": similar,
        "cast": [],
        "keywords": tags,
        "source": "anilist",
    }


# ── SMALL LLM SUMMARIZER ──────────────────────────────────────────────────────

SUMMARIZE_MUSIC_PROMPT = """[MODE: MUSIC METADATA STRUCTURING]
Produce a structured JSON profile for a music artist. Be precise — this drives semantic search.

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
  "artist_summary": "2-3 sentences outlining their defining sound and identity.",
  "why_listen": "1 sentence — the specific sonic or lyrical quality that defines them.",
  "embedding_text": "A dense, objective 3-4 sentence paragraph for semantic vector search. CRITICAL RULE: Do NOT write a music review or use marketing filler. Densely pack the specific subgenres, lyrical themes, instrumentation, and emotional tone into a factual summary.",
  "genres": ["2-5 music genres"],
  "themes": ["4-8 lyrical or sonic themes — be highly specific"],
  "mood": ["1-3 from MOOD REFERENCE"],
  "keywords": ["8-12 precise descriptors: era, subgenre, instrumentation, vocal style, cultural references"],
  "similar_artists": {similar_json},
  "rating": {rating}
}}"""

SUMMARIZE_PROMPT = """[MODE: METADATA STRUCTURING]
Produce a structured JSON profile. Be precise — this data drives semantic vector search and recommendations.

TITLE: {title} ({year})
TYPE: {media_type}
GENRES: {genres}
TAGS/KEYWORDS: {keywords}
OVERVIEW: {overview}{alt_plots_section}
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
  "plot_summary": "2-3 sentences. What actually happens and what is the core conflict.",
  "why_watch": "1 sentence. The main hook or specific appeal of the show.",
  "embedding_text": "A dense, objective 3-4 sentence paragraph for semantic vector search. CRITICAL RULE: Do NOT write a review, critique, or use marketing filler. Instead, densely pack the plot premise, core character dynamics, specific tropes, visual style, and emotional tone into a cohesive summary.",
  "genres": [...],
  "themes": ["4-8 highly specific narrative tropes, story elements, or visual themes"],
  "mood": ["pick 2-3 from the MOOD REFERENCE above"],
  "keywords": ["10 precise descriptors: tone, setting, tropes, style, era, subgenre"],
  "cast_top3": [...],
  "director": "...",
  "rating": ...
}}"""


async def summarize_with_small_llm(raw_metadata: dict) -> Optional[dict]:
    """
    Use the small/fast summarizer model to create a structured profile.
    Falls back to a rule-based profile if Ollama is unavailable.
    """
    import json as _json

    if raw_metadata.get("media_type") == "music":
        similar = raw_metadata.get("similar_artists", [])
        # Strip any Wikipedia-style footnote citations ([4], [12], etc.) that
        # may have slipped through from cached Last.fm bios before the fix.
        _raw_bio = (raw_metadata.get("bio", "") or "")
        _clean_bio = re.sub(r'\[\d+\]', '', _raw_bio).strip()
        prompt = SUMMARIZE_MUSIC_PROMPT.format(
            title=raw_metadata.get("title") or raw_metadata.get("name", "Unknown"),
            genres=", ".join(raw_metadata.get("genres", [])),
            tags=", ".join(raw_metadata.get("tags", [])[:15]),
            bio=_clean_bio[:500],
            similar=", ".join(similar[:8]),
            similar_json=_json.dumps(similar[:8]),
            listeners=raw_metadata.get("listeners") or "N/A",
            rating=raw_metadata.get("rating") or "N/A",
        )
    else:
        # Build alternative plot section if multiple sources are available
        alt_plots = raw_metadata.get("alt_plot_sources", {})
        if alt_plots:
            lines = ["\nALTERNATIVE DESCRIPTIONS (synthesize with OVERVIEW above):"]
            for src_name, text in alt_plots.items():
                lines.append(f"- {src_name.upper()}: {text[:400]}")
            alt_plots_section = "\n".join(lines)
        else:
            alt_plots_section = ""

        prompt = SUMMARIZE_PROMPT.format(
            title=raw_metadata.get("title", "Unknown"),
            year=raw_metadata.get("year", "Unknown"),
            media_type=raw_metadata.get("media_type", "movie"),
            genres=", ".join(raw_metadata.get("genres", [])),
            keywords=", ".join((raw_metadata.get("keywords") or raw_metadata.get("tags", []))[:20]),
            overview=(raw_metadata.get("overview_extended") or raw_metadata.get("overview", ""))[:800],
            alt_plots_section=alt_plots_section,
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
                    **ollama_options(temperature=0.1, num_predict=2600),
                },
            )
        if r.status_code != 200:
            logger.warning("Summarizer HTTP %s for '%s' (model=%s)",
                           r.status_code, raw_metadata.get("title", "?"), SUMMARIZER_MODEL)
            return _rule_based_profile(raw_metadata)

        raw_content = r.json().get("message", {}).get("content", "").strip()
        if not raw_content:
            logger.debug("Summarizer returned empty content for %s", raw_metadata.get("title"))
            return _rule_based_profile(raw_metadata)

        content = clean_llm_text(raw_content)

        try:
            result = json.loads(content)
            raw_source = raw_metadata.get("source", "unknown")
            result["source"] = f"{raw_source}+llm"
            logger.debug("Summarizer success for '%s': source=%s",
                         raw_metadata.get("title", "?"), result["source"])
            return result
        except json.JSONDecodeError as e:
            recovered = None
            try:
                partial = content.strip()

                # Strategy 1: cut at the last cleanly closed string/array field
                for marker in ['",\n', '"\n', '],\n', ']\n', '",']:
                    pos = partial.rfind(marker)
                    if pos > 100:
                        try:
                            recovered = json.loads(partial[:pos + 1] + "\n}")
                            break
                        except json.JSONDecodeError:
                            continue

                # Strategy 2: truncation happened mid-string value
                # Close the open string, then close the JSON object
                if not recovered:
                    # Find last complete key-value pair boundary before the cut
                    last_comma = partial.rfind(',\n  "')
                    if last_comma > 100:
                        try:
                            recovered = json.loads(partial[:last_comma] + "\n}")
                        except json.JSONDecodeError:
                            pass

                # Strategy 3: close the dangling string then the object
                if not recovered and not partial.endswith("}"):
                    for suffix in ['"}', '"]}', '"]\n}']:
                        try:
                            recovered = json.loads(partial + suffix)
                            break
                        except json.JSONDecodeError:
                            continue

                if recovered:
                    recovered["source"] = f"{raw_metadata.get('source', 'unknown')}+llm"
                    logger.debug("Recovered truncated JSON for '%s'", raw_metadata.get("title", "?"))
            except Exception as _e:
                # Pass 47 (B3-rest): the suffix-loop is a recovery best-effort,
                # but a silent miss here masks bugs in the recovery itself.
                logger.debug("[enricher] truncated-JSON recovery failed: %s", _e)
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
    overview_short = overview[:500].rsplit(' ', 1)[0] + "..." if len(overview) > 500 else overview
    
    embedding_text = (
        f"{m.get('title', '')} ({m.get('year', '')}). "
        f"Genres: {', '.join(genres)}. "
        f"Tags: {', '.join(keywords[:10])}. "
        f"{overview_short}"
    )

    return {
        "title": m.get("title", ""),
        "year": m.get("year"),
        "media_type": m.get("media_type", "movie"),
        "genres": genres,
        "themes": keywords[:4],
        "mood": [],
        "audience": "",
        "plot_summary": overview_short,
        "why_watch": "",
        "keywords": keywords[:10],
        "cast_top3": m.get("cast", [])[:3],
        "director": m.get("director"),
        "rating": m.get("rating"),
        "embedding_text": embedding_text,
        "source": f"{m.get('source', 'unknown')}:rule_based",
    }


# ── PRODUCER-CONSUMER SPLIT FUNCTIONS ────────────────────────────────────────

async def fetch_and_prepare_raw(
    title: str,
    media_type: str = "movie",
    tmdb_id: Optional[int] = None,
    anilist_id: Optional[int] = None,
    anidb_id: Optional[int] = None,
    tvdb_id: Optional[int] = None,
    imdb_id: Optional[str] = None,
    mal_id: Optional[int] = None,
    plex_rating_key: Optional[str] = None,
    sonarr_series_type: Optional[str] = None,
    year: Optional[int] = None,    # disambiguation hint for title search
) -> Optional[dict]:
    """Phase 1: fetch API metadata only (no LLM).

    Returns a raw dict with ``_cache_key`` / ``_plex_rating_key`` /
    ``_tmdb_id`` / ``_anilist_id`` embedded so the consumer can save
    without needing those IDs separately.

    Three return shapes that the caller MUST distinguish (Pass 99-fu):
      - dict with ``_already_enriched=True`` + ``_cached_profile`` →
        the cache already holds a fully-enriched profile for this
        item (LLM-polished, < 7 days fresh, or both). Caller should
        reconcile the EnrichmentStatus row with the cached profile
        and skip — do NOT write a not_found sentinel, do NOT re-fetch.
      - non-None dict without those keys → fresh raw API data, ready
        for the consumer's LLM pipeline.
      - ``None`` → no API data exists for this title. Caller writes
        a not_found sentinel.

    Pre-99-fu this function conflated "already done" and "no data" by
    returning None for both, which made the producer write not_found
    sentinels for items that were already fully enriched (cache hit on
    one id-key, EnrichmentStatus row reset and re-queued, fetch sees
    fresh cache and returns None, producer writes a misleading
    not_found sentinel). After a bulk EnrichmentStatus reset this
    poisoned 200 rows in 3 seconds.
    """
    cache = MetadataCache()

    # Resolve IDs from MediaIdentity
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
                    # Pass 73: mal_id was written by plex_sync (from Plex Guid
                    # tags) but never pulled here — so the deterministic Jikan
                    # lookup below never fired from the stored ID, it fell back
                    # to a fuzzy title search.
                    mal_id     = mal_id     or mi.mal_id
        except Exception as _e:
            # Pass 43 (B3): MediaIdentity lookup is best-effort; the
            # caller fills missing IDs via direct API calls when this
            # fast-path returns nothing. Log so persistent DB issues
            # don't hide behind the silent swallow.
            logger.debug("[enricher] MediaIdentity merge failed: %s", _e)

    is_anime = (
        media_type == "anime"
        or sonarr_series_type == "anime"
        or (sonarr_series_type is None and _looks_like_anime(title))
    )

    id_key = anilist_id or anidb_id or tmdb_id or tvdb_id or title[:40]
    cache_key = f"enriched:{media_type}:{id_key}"

    # Check raw pre-fetch cache written by game-mode consumer.
    # If present, return it directly so no API call is needed.
    if plex_rating_key:
        raw_cached = cache.get_cache(f"raw_prefetch:{plex_rating_key}")
        if raw_cached:
            raw_data = dict(raw_cached["response"])
            raw_data["_cache_key"]       = cache_key
            raw_data["_plex_rating_key"] = plex_rating_key
            raw_data["_tmdb_id"]         = tmdb_id or raw_data.get("_tmdb_id")
            raw_data["_anilist_id"]      = anilist_id or raw_data.get("_anilist_id")
            cache.close()
            logger.debug("Raw prefetch cache hit for '%s' (%s)", title, plex_rating_key)
            return raw_data

    # Pass 99-fu2: two-tier cache read.
    #
    # Tier 1 (polished): ``enriched:{cat}:{id_key}`` carries the LLM-
    # polished profile tagged with ``prompt_version``. Hit + version
    # match → fully done; signal ``_already_enriched`` so the producer
    # reconciles the EnrichmentStatus row without re-running the LLM.
    #
    # Tier 2 (raw):     ``raw:{cat}:{id_key}`` carries just the API
    # fetch result (TMDB/AniList/MB/Last.fm response normalised). Hit
    # → hand straight to the consumer for LLM polish; SKIP the API
    # round-trip. This is what makes a prompt-version bump cheap: we
    # don't re-spam TMDB just because the LLM prompt changed.
    #
    # Pre-99-fu2 there was only one cache (the polished one), so a
    # version-stale entry would force a full re-fetch from the APIs —
    # multiplying the LLM-rebake cost by API rate limits and TMDB
    # outage risk. Splitting them keeps prompt-bumps to LLM-compute only.

    from datetime import datetime as _dt
    polished_hit = cache.get_cache(cache_key)  # tier 1
    raw_hit_key  = f"raw:{media_type}:{id_key}"
    raw_hit      = cache.get_cache(raw_hit_key)  # tier 2
    cache.close()

    if polished_hit:
        cached_profile = polished_hit.get("response", {})
        cached_source  = cached_profile.get("source", "")
        cached_version = cached_profile.get("prompt_version")
        cached_at_str  = polished_hit.get("created_at") or polished_hit.get("cached_at")
        cache_age_days = 999
        if cached_at_str:
            try:
                cached_dt = _dt.fromisoformat(str(cached_at_str))
                cache_age_days = (_dt.utcnow() - cached_dt).days
            except Exception as _e:
                logger.debug("[enricher] cached_at parse failed: %r → %s", cached_at_str, _e)

        is_llm_polished = "+llm" in cached_source or cached_source == "llm"
        is_recent_miss  = cached_source == "not_found" and cache_age_days < 3

        if is_llm_polished and cached_version == _PROMPT_VERSION:
            # Tier-1 hit, prompt version matches — terminal state.
            return {
                "_already_enriched": True,
                "_cached_profile":   cached_profile,
                "_cache_key":        cache_key,
                "_plex_rating_key":  plex_rating_key,
            }
        if is_recent_miss:
            # not_found sentinel still fresh — skip silently as before.
            return {
                "_already_enriched": True,
                "_cached_profile":   cached_profile,
                "_cache_key":        cache_key,
                "_plex_rating_key":  plex_rating_key,
            }
        if is_llm_polished and cached_version != _PROMPT_VERSION:
            # Polished profile from an OLDER prompt — fall through to
            # tier-2 raw cache (and then to fresh fetch as a last
            # resort). The consumer will re-polish with the current
            # prompt. We don't return the stale profile.
            logger.debug(
                "[enricher] polished cache version mismatch (have %r, want %r) "
                "for %s — will re-polish from raw cache or fresh fetch",
                cached_version, _PROMPT_VERSION, cache_key,
            )

    if raw_hit:
        # Tier-2 hit — we have the API fetch result, skip the round-trip.
        # Embed the cache routing fields so the consumer's ``process_and_save``
        # writes the polished cache back under the same id_key.
        raw_data = dict(raw_hit["response"])
        raw_data["_cache_key"]       = cache_key
        raw_data["_plex_rating_key"] = plex_rating_key
        raw_data["_tmdb_id"]         = tmdb_id    or raw_data.get("_tmdb_id")
        raw_data["_anilist_id"]      = anilist_id or raw_data.get("_anilist_id")
        raw_data["_from_raw_cache"]  = True
        return raw_data

    # Anime cross-ref ID resolution
    if is_anime and tvdb_id and not anilist_id and not anidb_id:
        try:
            from src.services.anime_mapping import get_anime_mapping
            mapping = await get_anime_mapping()
            ids = mapping.lookup_tvdb(tvdb_id)
            if ids.get("anidb_id"):
                anidb_id = ids["anidb_id"]
            if ids.get("anilist_id"):
                anilist_id = ids["anilist_id"]
        except Exception as e:
            logger.debug("anime-lists lookup failed: %s", e)

    # Music — MusicBrainz + Last.fm
    if media_type == "music":
        from src.services.music_metadata import enrich_artist
        artist_raw = await enrich_artist(title)
        if not artist_raw:
            return None
        artist_raw["title"] = artist_raw.get("name", title)
        artist_raw["media_type"] = "music"
        artist_raw["source"] = "musicbrainz+lastfm"
        # Pass 99-fu2: persist the raw fetch result to tier-2 cache so a
        # future ``_PROMPT_VERSION`` bump can re-polish without re-hitting
        # MB + Last.fm. Strip the underscore-prefixed transport fields —
        # those are caller-instance specific and get re-injected on read.
        _write_raw_cache(media_type, id_key, artist_raw)
        artist_raw["_cache_key"] = cache_key
        artist_raw["_plex_rating_key"] = plex_rating_key
        artist_raw["_tmdb_id"] = None
        artist_raw["_anilist_id"] = None
        return artist_raw

    # Non-music: fetch raw metadata
    raw = None
    if anilist_id:
        raw = await fetch_anilist_full(anilist_id)
    elif anidb_id and is_anime:
        raw = await search_anilist_by_title(title)
        if raw:
            raw["anidb_id"] = anidb_id
            if raw.get("id") and anidb_id:
                try:
                    from src.services.anime_mapping import update_anilist_id
                    update_anilist_id(anidb_id, raw["id"])
                except Exception as _e:
                    # Pass 43 (B3): anime-mapping update is best-effort —
                    # log so failures don't silently rot the cross-ref DB.
                    logger.debug("[enricher] update_anilist_id(%s) failed: %s", anidb_id, _e)
    elif tmdb_id and media_type == "movie":
        raw = await fetch_tmdb_full(tmdb_id, "movie")
    elif tmdb_id and not is_anime:
        raw = await fetch_tmdb_full(tmdb_id, "tv")
        if raw and "Animation" in raw.get("genres", []) and _looks_like_anime(title):
            al = await search_anilist_by_title(title)
            if al:
                al["cast"] = raw.get("cast", [])
                al["similar_titles"] = al.get("similar_titles") or raw.get("similar_titles", [])
                raw = al
    elif is_anime:
        raw = await search_anilist_by_title(title)
        if not raw and tmdb_id:
            raw = await fetch_tmdb_full(tmdb_id, "tv")
        if not raw:
            raw = await _tmdb_search_and_fetch(title, "tv", year=year)
    elif imdb_id:
        raw = await _tmdb_fetch_by_external_id(imdb_id, media_type)
        if not raw:
            endpoint = "movie" if media_type == "movie" else "tv"
            raw = await _tmdb_search_and_fetch(title, endpoint, year=year)
    elif tvdb_id:
        endpoint = "movie" if media_type == "movie" else "tv"
        raw = await _tmdb_search_and_fetch(title, endpoint, year=year)
    else:
        endpoint = "movie" if media_type in ("movie",) else "tv"
        if is_anime:
            raw = await search_anilist_by_title(title)
        if not raw:
            raw = await _tmdb_search_and_fetch(title, endpoint, year=year)

    if not raw:
        return None

    # Supplements (Jikan for anime, OMDB for others)
    supplements = []
    if is_anime:
        mal_id_val = mal_id or raw.get("mal_id")
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

    if supplements:
        raw = _merge_raw_metadata(raw, *supplements)

    # Pass 99-fu2: persist the (fresh + supplemented) API result to tier-2
    # cache. Done BEFORE the underscore transport fields get attached so
    # the cached blob is portable across callers. Next time the same
    # id_key is requested, the tier-2 hit short-circuits the TMDB +
    # AniList + OMDb + Jikan round-trip entirely.
    raw["plex_rating_key"] = plex_rating_key
    _write_raw_cache(media_type, id_key, raw)

    raw["_cache_key"] = cache_key
    raw["_plex_rating_key"] = plex_rating_key
    raw["_tmdb_id"] = tmdb_id or raw.get("tmdb_id")
    raw["_anilist_id"] = anilist_id or raw.get("anilist_id")
    return raw


async def process_and_save(raw: dict) -> Optional[dict]:
    """Phase 2: LLM summarize + MetadataCache + ChromaDB embedding.

    ``raw`` must carry the ``_cache_key`` / ``_plex_rating_key`` /
    ``_tmdb_id`` / ``_anilist_id`` fields set by ``fetch_and_prepare_raw``.
    Returns the structured profile dict or None on LLM failure.
    """
    cache_key = raw.pop("_cache_key", None)
    plex_rating_key = raw.pop("_plex_rating_key", None) or raw.get("plex_rating_key")
    tmdb_id = raw.pop("_tmdb_id", None) or raw.get("tmdb_id")
    anilist_id = raw.pop("_anilist_id", None) or raw.get("anilist_id")
    media_type = raw.get("media_type", "movie")

    profile = await summarize_with_small_llm(raw)
    if not profile:
        return None

    if media_type == "music":
        profile["source"] = "musicbrainz+lastfm+llm"
    profile["tmdb_id"] = tmdb_id
    profile["anilist_id"] = anilist_id
    profile["plex_rating_key"] = plex_rating_key
    # Pass 99-fu2: tag the polished profile with the current prompt
    # version so a future bump invalidates this cache entry on read.
    profile["prompt_version"] = _PROMPT_VERSION

    if cache_key:
        cache = MetadataCache()
        cache.set_cache(cache_key, profile, days=30)
        cache.close()

    # Generate embedding and store in ChromaDB
    try:
        from src.embeddings.embedding_generator import EmbeddingGenerator
        from src.vector_store.chromadb_wrapper import chroma_db

        title = profile.get("title", raw.get("title", ""))
        text_to_embed = profile.get("embedding_text")
        if not text_to_embed and media_type == "music":
            text_to_embed = (
                f"{title} — {', '.join(profile.get('genres', [])[:6])}. "
                f"Tags: {', '.join(profile.get('keywords', [])[:8])}."
            )

        if text_to_embed:
            gen = EmbeddingGenerator()
            vec = await gen.generate_embedding(text_to_embed)
            if vec:
                doc_id = str(plex_rating_key or tmdb_id or anilist_id or title)
                chroma_meta = {
                    "title": title,
                    "media_type": media_type,
                    "domain": media_type,   # hard quarantine key for gated retrieval
                    "genres": ", ".join(profile.get("genres", [])[:6]
                                       if isinstance(profile.get("genres"), list) else []),
                    "themes": ", ".join(profile.get("themes", [])[:6]
                                       if isinstance(profile.get("themes"), list) else []),
                    "mood": ", ".join(profile.get("mood", [])
                                     if isinstance(profile.get("mood"), list) else []),
                    "year": profile.get("year") or 0,
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
        logger.debug("ChromaDB store failed for '%s': %s", profile.get("title", "?"), e)

    return profile


# ── MAIN ENRICHMENT ENTRY POINT ───────────────────────────────────────────────

async def enrich_media_item(
    title: str,
    media_type: str = "movie",
    tmdb_id: Optional[int] = None,
    anilist_id: Optional[int] = None,
    anidb_id: Optional[int] = None,
    tvdb_id: Optional[int] = None,
    imdb_id: Optional[str] = None,
    mal_id: Optional[int] = None,
    plex_rating_key: Optional[str] = None,
    sonarr_series_type: Optional[str] = None,  # "anime"/"standard"/"daily" from Sonarr
    year: Optional[int] = None,                 # disambiguation hint for title search
    skip_llm_summary: bool = False,             # Pass 14.8: chat-cascade fast path
) -> Optional[dict]:
    """
    Full pipeline for a single media item.
    Looks up MediaIdentity for all known IDs first, then picks
    the best API for each content type:
      - Anime: AniList (anilist_id) > AniDB lookup > title search
      - Movies: TMDB (tmdb_id) > title search
      - Shows: TMDB (tmdb_id) > TVDB > title search

    ``skip_llm_summary`` (Pass 14.8): chat-cascade fast path. When True we
    run all the API fetches (TMDB / AniList / Jikan / OMDB / MusicBrainz)
    but skip the final ``summarize_with_small_llm`` call — that step adds
    3-8 s of LLM-summarisation latency that the chat cascade can't afford
    inside its per-domain timeout. The returned dict still has the fields
    the curator actually needs (title, year, director, cast, plot, genres,
    rating, country) — just without the LLM-polished tone / synopsis tags.
    Cache is bypassed for save (the partial profile would mask future full
    enrichments) but cache lookup still works (full profiles get returned).
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
                    # Pass 73: mal_id was written by plex_sync (from Plex Guid
                    # tags) but never pulled here — so the deterministic Jikan
                    # lookup below never fired from the stored ID, it fell back
                    # to a fuzzy title search.
                    mal_id     = mal_id     or mi.mal_id
        except Exception as _e:
            # Pass 43 (B3): MediaIdentity lookup is best-effort; the
            # caller fills missing IDs via direct API calls when this
            # fast-path returns nothing. Log so persistent DB issues
            # don't hide behind the silent swallow.
            logger.debug("[enricher] MediaIdentity merge failed: %s", _e)

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
            except Exception as _e:
                # Pass 43 (B3): timestamp parse fail leaves cache_age_days at
                # the "very old" default — cache miss path takes over.
                logger.debug("[enricher] cached_at parse failed (%r): %s", cached_at_str, _e)
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

        # Pass 14.12: chat cascade fast-path also for music (was missing in
        # Pass 14.8 — that pass only fixed the movie/tv/anime branch). Music
        # lookups for "King Crimson" / "Sleep Token" hit MusicBrainz +
        # Last.fm in <1s but the subsequent summarize_with_small_llm pushes
        # total past the 10s cascade timeout. Skip-mode returns the raw
        # MusicBrainz/Last.fm profile directly with the curator-relevant
        # fields.
        if skip_llm_summary:
            cache.close()
            return {
                "title":           artist_raw.get("title") or title,
                "name":            artist_raw.get("name") or title,
                "year":            None,
                "media_type":      "music",
                "genres":          artist_raw.get("genres") or [],
                "country":         artist_raw.get("country") or "",
                "active_years":    artist_raw.get("active_years") or "",
                "similar_artists": artist_raw.get("similar_artists") or [],
                "top_albums":      artist_raw.get("top_albums") or [],
                "tags":            artist_raw.get("tags") or [],
                "bio":             artist_raw.get("bio") or "",
                "plot_summary":    artist_raw.get("bio") or "",
                "rating":          artist_raw.get("rating"),
                "source":          "musicbrainz+lastfm+raw",
            }

        # Run through LLM summarizer for structured genres/themes/mood/embedding_text
        profile = await summarize_with_small_llm(artist_raw)
        if not profile:
            cache.close()
            return None
        profile["source"] = "musicbrainz+lastfm+llm"
        profile["plex_rating_key"] = plex_rating_key
        profile["prompt_version"] = _PROMPT_VERSION   # Pass 99-fu2
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
                    "domain": "music",      # hard quarantine key for gated retrieval
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
                except Exception as _e:
                    # Pass 43 (B3): anime-mapping update is best-effort —
                    # log so failures don't silently rot the cross-ref DB.
                    logger.debug("[enricher] update_anilist_id(%s) failed: %s", anidb_id, _e)
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
            raw = await _tmdb_search_and_fetch(title, "tv", year=year)
    elif imdb_id:
        # Have IMDb ID — TMDB can find by external ID
        raw = await _tmdb_fetch_by_external_id(imdb_id, media_type)
        if not raw:
            endpoint = "movie" if media_type == "movie" else "tv"
            raw = await _tmdb_search_and_fetch(title, endpoint, year=year)
    elif tvdb_id:
        # Have TVDB ID — use IMDb search if available, else title search
        endpoint = "movie" if media_type == "movie" else "tv"
        raw = await _tmdb_search_and_fetch(title, endpoint, year=year)
    else:
        # Last resort: title search
        endpoint = "movie" if media_type in ("movie",) else "tv"
        if is_anime:
            raw = await search_anilist_by_title(title)
        if not raw:
            raw = await _tmdb_search_and_fetch(title, endpoint, year=year)

    if not raw:
        cache.close()
        return None

    # 1b. Supplement with additional sources
    supplements = []

    if is_anime:
        # Get MAL ID from raw data if AniList stored it
        mal_id_val = mal_id or raw.get("mal_id")
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

    # Pass 14.8 fast-path: skip LLM summarisation when called from the chat
    # cascade. We hand back a raw-derived profile with the curator-relevant
    # fields and DON'T cache (the partial profile would mask later full
    # enrichments). Cache misses repeat the API fetches but the API layer is
    # fast (1-3 s); the LLM step (3-8 s) was the timeout-killer.
    if skip_llm_summary:
        cache.close()
        fast_profile = {
            "title":         raw.get("title") or title,
            "original_title": raw.get("original_title") or "",
            "year":          raw.get("year"),
            "media_type":    raw.get("media_type") or media_type,
            "genres":        raw.get("genres") or [],
            "rating":        raw.get("rating"),
            "vote_count":    raw.get("vote_count"),
            "director":      raw.get("director") or "",
            "cast":          raw.get("cast") or [],
            "country":       raw.get("country") or "",
            "runtime":       raw.get("runtime"),
            "studios":       raw.get("studios") or raw.get("studio") or "",
            "episodes_total": raw.get("episodes_total") or raw.get("episodes"),
            "source_material": raw.get("source_material") or raw.get("source") or "",
            "plot_summary":  raw.get("overview_extended") or raw.get("overview") or "",
            "tmdb_id":       tmdb_id or raw.get("tmdb_id"),
            "anilist_id":    anilist_id or raw.get("anilist_id"),
            "source":        f"{raw.get('source', 'api')}+raw",
        }
        return fast_profile

    # 2. Summarize with small LLM
    profile = await summarize_with_small_llm(raw)
    if not profile:
        cache.close()
        return None

    # Merge in IDs
    profile["tmdb_id"] = tmdb_id or raw.get("tmdb_id")
    profile["anilist_id"] = anilist_id or raw.get("anilist_id")
    profile["plex_rating_key"] = plex_rating_key
    profile["prompt_version"] = _PROMPT_VERSION   # Pass 99-fu2

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
            # Generate the vector with nomic-embed-text
            generator = EmbeddingGenerator()
            embedding_vector = await generator.generate_embedding(text_to_embed)

            if embedding_vector:
                # Build the metadata payload for ChromaDB search
                chroma_metadata = {
                    "title": profile.get("title", ""),
                    "media_type": profile.get("media_type", "movie"),
                    "domain": profile.get("media_type", "movie"),  # hard quarantine key for gated retrieval
                    "genres": ", ".join(profile.get("genres", [])),
                    "themes": ", ".join(profile.get("themes", [])),
                    "mood": ", ".join(profile.get("mood", [])),
                    "year": profile.get("year") or 0
                }

                # Pick a stable unique ID for the document
                doc_id = str(plex_rating_key or tmdb_id or anilist_id or profile["title"])

                # In ChromaDB upserten — duplicate-id raises on `add`, so we
                # fall back to deleting + re-adding to refresh the embedding
                # too (update_metadata alone leaves a stale vector).
                try:
                    chroma_db.add_documents(
                        documents=[text_to_embed],
                        embeddings=[embedding_vector],
                        metadatas=[chroma_metadata],
                        ids=[doc_id]
                    )
                except Exception as add_exc:
                    logger.debug("ChromaDB add failed for '%s' (%s) — re-adding",
                                 doc_id, add_exc)
                    try:
                        chroma_db.delete_by_id(doc_id)
                    except Exception as _e:
                        # Pass 47 (B3-rest): a silent failure here masks vector-
                        # consistency problems — we'd re-add over the existing
                        # doc but the old vector might still leak through if
                        # delete_by_id partially succeeded. Log loudly enough
                        # to spot but don't escalate (re-add usually wins).
                        logger.warning(
                            "[enricher] chroma delete_by_id failed before re-add (%s): %s",
                            doc_id, _e,
                        )
                    chroma_db.add_documents(
                        documents=[text_to_embed],
                        embeddings=[embedding_vector],
                        metadatas=[chroma_metadata],
                        ids=[doc_id]
                    )
                logger.debug("Successfully stored '%s' in ChromaDB.", profile.get("title"))

            try:
                await generator.close()
            except Exception as _e:
                # Pass 47 (B3-rest): generator close is best-effort cleanup;
                # logging at debug surfaces leak-pattern bugs without
                # spamming the happy-path log.
                logger.debug("[enricher] generator close on happy path failed: %s", _e)
    except Exception as e:
        logger.error("Failed to store '%s' in ChromaDB: %s", profile.get("title", "?"), e)
        # Best-effort: ensure we don't leak the embedding generator on failure.
        try:
            await generator.close()
        except Exception as _e:
            # Pass 43 (B3): close-on-error is itself error-prone (HTTP client
            # already-closed, transport gone). Log at debug — a generator
            # leak is benign at process scope but worth seeing if it spikes.
            logger.debug("[enricher] generator close on error failed: %s", _e)

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
    if words & ANIME_HINTS:
        return True
    # Colon-notation like "Re:Zero" or "No Game:No Life" (no space around colon)
    # — but not normal subtitle patterns like "Captain America: The First Avenger"
    if re.search(r"\w:\w", title):
        return True
    return False


def _title_match_score(query: str, found: str) -> float:
    """
    Word-overlap similarity between two titles, normalized 0-1.
    Ignores common stop-words and punctuation.
    """
    if not query or not found:
        return 0.0
    stops = {"the", "a", "an", "of", "and", "in", "on", "at", "to", "for", "is", "no"}
    def _words(s: str) -> set:
        return set(re.sub(r"[^a-z0-9 ]", " ", s.lower()).split()) - stops
    q_words = _words(query)
    f_words = _words(found)
    if not q_words or not f_words:
        return 0.0
    overlap = len(q_words & f_words)
    return overlap / max(len(q_words), len(f_words))


def _titles_close_enough(query: str, candidates: list[str], threshold: float = 0.6) -> bool:
    """Sharper matcher that prevents false positives on long titles.

    Short-query guard (Pass 14.4): when the query is ≤ 4 characters
    (e.g. "It", "Up", "Big"), require an EXACT match. Substring-matching
    on 2-3 character words produces noise — "It" matches "Strike It Rich",
    "Big Little Lies", "Bring It On". The cure is worse than the disease
    so for tiny queries we just trust the exact-match path.

    Colon-suffix guard (Pass 14.10): "King Crimson" must NOT match
    "King Crimson: Deja VROOOM". A colon followed by extra text marks the
    candidate as a *specific* sub-item (live concert, special edition,
    season title, …) — if the user typed only the bare name, they meant
    the band/series, not the spin-off. Reject the substring match for
    these cases.
    """
    query_clean = query.lower().strip()
    is_short_query = len(query_clean) <= 4
    query_word_count = len(query_clean.split())

    for c in candidates:
        if not c: continue
        c_clean = c.lower().strip()

        # 1. Exact match — always wins
        if query_clean == c_clean: return True

        # Short-query guard: skip substring + fuzzy paths entirely
        if is_short_query:
            continue

        # Colon-suffix guard: candidate is "<query>: <subtitle>" → not a match
        if ":" in c_clean:
            base = c_clean.split(":", 1)[0].strip()
            if base == query_clean:
                # "King Crimson" vs "King Crimson: Deja VROOOM" — reject
                continue

        # Pass 15.2 single-word-query guard: when the query is a single word,
        # the substring must appear at the START of the candidate (followed
        # by a word boundary). Otherwise "Ghosts" matches "Inner Ghosts",
        # "Old Ghosts", "Ghost Story" — fuzzy noise that lets the wrong
        # title win the slot.
        if query_word_count == 1 and query_clean in c_clean:
            if not (
                c_clean.startswith(query_clean + " ")
                or c_clean.startswith(query_clean + ":")
                or c_clean.startswith(query_clean + "(")
            ):
                # Query word appears mid-candidate, not at start → reject.
                # Skip to the fuzzy-score path (which has its own threshold)
                # rather than falling through to the lax substring check.
                if _title_match_score(query, c) >= threshold:
                    return True
                continue

        # 2. Substring match with a length check
        # Stops a 1-word query like "Jesus" from matching the 8-word title
        # "Jesus Shows You the Way to the Highway".
        if query_clean in c_clean or c_clean in query_clean:
            q_len = len(query_clean.split())
            c_len = len(c_clean.split())
            # Only allow the match when the titles are close in word count,
            # or when the fuzzy match score is high enough to carry it anyway.
            if abs(q_len - c_len) <= 2:
                return True

        # 3. Fuzzy word-overlap (the established score)
        if _title_match_score(query, c) >= threshold:
            return True

    return False


ANILIST_SEARCH_QUERY = """
query ($search: String) {
  Page(perPage: 5) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      title { romaji english native }
      synonyms
      description(asHtml: false)
      genres
      tags { name rank isMediaSpoiler }
      averageScore meanScore popularity
      episodes duration
      startDate { year }
      studios(isMain: true) { nodes { name } }
      staff(sort: RELEVANCE) { edges { role node { name { full } } } }
      recommendations(sort: RATING_DESC) { nodes { mediaRecommendation { title { romaji english } } } }
    }
  }
}
"""


async def search_anilist_by_title(title: str) -> Optional[dict]:
    """Search AniList by title, iterate up to 5 candidates, return first close match or None."""
    try:
        await _anilist_wait()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://graphql.anilist.co",
                json={"query": ANILIST_SEARCH_QUERY, "variables": {"search": title}},
            )
            if r.status_code == 429:
                wait_s = _anilist_set_backoff(r.headers)
                logger.info("AniList 429 for '%s' — skipping (backed off %.0fs)", title, wait_s)
                return None
            if r.status_code != 200:
                return None
            media_list = r.json().get("data", {}).get("Page", {}).get("media") or []
            if not media_list:
                return None
    except Exception as e:
        logger.debug("AniList search '%s' error: %s", title, e)
        return None

    # Iterate all returned candidates and pick the first one whose title is
    # close enough to the query.  Using a Page query (5 results) instead of
    # a single Media result means we don't blindly accept the top-ranked hit
    # when it doesn't actually match (e.g. 'Futurama' → random mouse anime,
    # or 'Golden Boy' → 'Golden Kamuy').
    media = None
    found_title = title
    for candidate in media_list:
        # Pass 56: ``or {}`` — a present-but-null "title" would otherwise
        # slip past the .get default and crash on ct.get(...) below.
        ct = candidate.get("title") or {}
        candidate_titles = [
            ct.get("english") or "",
            ct.get("romaji") or "",
            ct.get("native") or "",
        ] + (candidate.get("synonyms") or [])
        if _titles_close_enough(title, candidate_titles):
            media = candidate
            found_title = ct.get("english") or ct.get("romaji") or title
            break

    if not media:
        logger.debug(
            "AniList search '%s' → no close match in top %d results",
            title, len(media_list),
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
        # Pass 56: ``or {}`` (not a .get default) — AniList may send
        # mediaRecommendation: null or title: null on sparse entries.
        rec = node.get("mediaRecommendation") or {}
        t = rec.get("title") or {}
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
        # Pass 54: averageScore is AniList's weighted score — it stays null
        # for fresh / niche titles that haven't cleared the confidence
        # threshold yet. meanScore (plain average) is populated earlier, so
        # fall back to it before giving up. Either way it's a real user
        # rating; 0 only if AniList genuinely has neither.
        "rating": (media.get("averageScore") or media.get("meanScore") or 0) / 10,
        "episodes_total": media.get("episodes"),
        "runtime_min": media.get("duration"),
        "similar_titles": similar,
        "cast": [],
        "keywords": tags,
        "source": "anilist",
    }


async def _tmdb_search_and_fetch(
    title: str,
    endpoint: str,
    year: Optional[int] = None,
) -> Optional[dict]:
    """Search TMDB by title (with optional year hint) and fetch full details.

    The ``year`` hint disambiguates same-name titles released in different
    years (e.g. *Jesus Shows You the Way to the Highway* — 2019 surreal
    sci-fi vs. *Jesus' Son* — 1999 drama). When set, we ask TMDB to filter
    by primary release year first; if that returns nothing, we fall back to
    the unfiltered search and prefer the result whose release year matches
    the hint.
    """
    if not settings.TMDB_API_KEY:
        return None

    params: dict = {"query": title}
    if year:
        # TMDB uses different param names per endpoint
        params["year" if endpoint == "movie" else "first_air_date_year"] = year

    async with httpx.AsyncClient(timeout=10) as client:
        r = await _tmdb_get(client, f"/search/{endpoint}", params)
    results = r.get("results", [])

    # Year-filtered search came back empty? Retry without the year filter and
    # prefer year-matching candidates manually.
    if not results and year:
        logger.debug("TMDB search '%s' year=%d → 0 results, retrying without year",
                     title, year)
        async with httpx.AsyncClient(timeout=10) as client:
            r = await _tmdb_get(client, f"/search/{endpoint}", {"query": title})
        results = r.get("results", [])

    if not results:
        return None

    # Find best-matching result rather than blindly taking the first
    title_key = "title" if endpoint == "movie" else "name"
    alt_key = "original_title" if endpoint == "movie" else "original_name"
    date_key = "release_date" if endpoint == "movie" else "first_air_date"

    def _result_year(res: dict) -> Optional[int]:
        d = (res.get(date_key) or "")[:4]
        return int(d) if d.isdigit() else None

    # Pass 1: prefer a year-matching candidate when we have a hint
    found_id = None
    if year:
        for result in results[:5]:
            candidates = [result.get(title_key, ""), result.get(alt_key, "")]
            if _titles_close_enough(title, candidates) and _result_year(result) == year:
                found_id = result.get("id")
                logger.info("TMDB '%s' → year-exact match id=%s (%d)",
                            title, found_id, year)
                break

    # Pass 2: best title match regardless of year
    if not found_id:
        for result in results[:5]:
            candidates = [result.get(title_key, ""), result.get(alt_key, "")]
            if _titles_close_enough(title, candidates):
                found_id = result.get("id")
                if year and _result_year(result) and _result_year(result) != year:
                    logger.warning(
                        "TMDB '%s' → title match id=%s but year mismatch "
                        "(hint=%d, found=%d) — using anyway",
                        title, found_id, year, _result_year(result),
                    )
                break

    if not found_id:
        logger.debug("TMDB search '%s' → no close match in top 5 results, skipping", title)
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
