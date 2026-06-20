"""
Curatarr 1.0 - Recommendations Engine

Uses taste vectors + LLM to generate personalised recommendations
with a written pitch per item, organised by category.

Features:
  - Cache persistence: results are stored in the DB.
  - Force refresh: manual regeneration is available via the UI.
  - Context-aware recommendations from the library or discovery feed.
"""

import json
import logging
from datetime import datetime
from typing import Optional

import httpx
import numpy as np

from src.config import settings
from src.services.llm_utils import clean_llm_text, strip_think_tags, ollama_options, CURATOR_KEEP_ALIVE
from src.services.app_state import get_datetime
from src.database.connection import get_db_session
from src.services.episodic_memory import retrieve_memories, format_memories_for_context
from src.database.models import (
    TasteVectorEntry, WatchHistoryEntry, User, CachedRecommendation,
    EncryptedTasteVector, ProtectedMedia, EpisodicMemory
)
from src.vector_store.chromadb_wrapper import chroma_db

logger = logging.getLogger(__name__)


# Pass 42 (A4): single source of truth for the LLM-forbidden phrases.
# Previously this list lived inline in three different prompt strings
# (recs pitch / external discovery / deletion pitch) and the three
# slowly drifted — Pass 39 extended the deletion-pitch blacklist with
# new crutch phrases the code review surfaced, but the recommendation
# pitch and discovery prompts kept the old shorter list, so banned
# words still leaked into other surfaces. One constant, one builder,
# three uses below.
BLACKLISTED_PITCH_PHRASES: tuple[str, ...] = (
    # Generic AI clichés
    "high-octane", "mind-bending", "cyber-rebellion", "adrenaline",
    "edge-of-your-seat", "relentless", "masterpiece", "epic",
    "rhythmic propulsion", "kinetic precision",
    # Curator-coined hooks that became repetitive over many pitches
    "sakuga density", "narrative propulsion", "lore economy",
    "exposition bloat", "structural discipline", "thematic depth",
    "tonal register",
)


def _blacklist_rule(rule_number: int) -> str:
    """Format the BLACKLIST rule line for a numbered-rule prompt template."""
    joined = ", ".join(f"'{p}'" for p in BLACKLISTED_PITCH_PHRASES)
    return (
        f"{rule_number}. UNIVERSAL BLACKLIST: You are STRICTLY FORBIDDEN from using "
        f"any of these crutch phrases: {joined}."
    )


def _cache_is_fresh(cached_items: list) -> bool:
    """Recs cache is fresh while neither watch-state sync nor enrichment has
    delivered new data since the cache was written.

    Both ``recs_invalidate_at`` (set by sync/enrichment when they actually
    added rows) and the row-level ``cached_at`` are naive UTC datetimes.
    """
    if not cached_items:
        return False
    invalidate_at = get_datetime("recs_invalidate_at")
    if invalidate_at is None:
        return True
    oldest = min(c.cached_at for c in cached_items if c.cached_at)
    return oldest >= invalidate_at

# ── Anti-tunneling: domain-specific vocabulary guidelines ─────────────────────

_VOCAB_GUIDELINES: dict[str, str] = {
    "documentary": (
        "DOMAIN VOCABULARY — Documentary: Focus on information density, research depth, "
        "pedagogical value, talking-head fatigue, and the insight-to-runtime ratio. "
        "Critique whether the film earns its runtime with genuine revelation or recycles "
        "surface-level takes. Use terms like 'archival depth', 'editorial discipline', "
        "'journalistic rigour', 'expository pacing'."
    ),
    "music": (
        "DOMAIN VOCABULARY — Music: You have the artist's GENRE, scene, themes and "
        "mood tags — NOT the audio. Do NOT judge timbre, mix, production, dynamics, "
        "compression or 'sheen': you cannot hear them, so any such claim is invented. "
        "Critique instead from what the tags tell you — genre/scene fit against the "
        "user's core sound, emotional register, cultural/era placement, catalogue "
        "depth vs novelty, replay value, and whether the library already covers this "
        "lane — and whether they still earn shelf space."
    ),
    "anime": (
        "DOMAIN VOCABULARY — Anime: Focus on worldbuilding coherence, animation fluidity "
        "versus static frames, trope fatigue, narrative propulsion, and pacing discipline. "
        "Use terms like 'sakuga density', 'lore economy', 'arc coherence', "
        "'tonal register', 'exposition-to-action ratio'."
    ),
    "show": (
        "DOMAIN VOCABULARY — TV Show: Focus on season-over-season quality decay, "
        "character-arc consistency, dialogue density, premise exhaustion, and whether "
        "the show still has narrative runway. Use terms like 'serialisation discipline', "
        "'character regression', 'procedural fatigue', 'showrunner vision'."
    ),
    "movie": (
        "DOMAIN VOCABULARY — Film: Focus on cinematography, character arcs, narrative "
        "economy, tonal consistency, and structural integrity. Use terms like "
        "'mise-en-scène', 'third-act collapse', 'tonal whiplash', 'screenplay economy', "
        "'directorial voice'."
    ),
}

_FORBIDDEN_CROSS_DOMAIN = (
    "STRICTLY FORBIDDEN CROSS-DOMAIN TERMS: Never use music-related terms "
    "(rhythmic, sonic, synthetic, frequency, beat, melody, tempo, harmonic) "
    "when critiquing films, documentaries, or TV shows unless the content is "
    "explicitly about music. Never use film-specific jargon (cinematography, "
    "mise-en-scène, frame composition) when critiquing music or anime."
)

# Injected into a pitch / discussion ONLY when there is no verified-data block
# (the item isn't enriched and the on-demand fast-enrich couldn't resolve it —
# the curator has nothing but a one-line synopsis). Without this the curator
# confabulates confident execution verdicts on zero data and dismisses the
# user's own signals as "noise" (the Fringe case: trashed an 8.4 show with
# invented "procedural fatigue / case-of-the-week" critique it could not know).
NO_VERIFIED_DATA_HEDGE = (
    "⚠️ NO VERIFIED DATA FOR THIS TITLE — you have ONLY a bare synopsis. No "
    "verified themes, year, significance, cast or production facts (it isn't "
    "enriched). This is a DATA-POOR judgment, so you MUST:\n"
    "- Open by naming the blind spot ('I only have a one-line synopsis here').\n"
    "- NOT invent execution verdicts (pacing, 'case of the week', 'formulaic', "
    "'bloated', 'dated', 'procedural fatigue', collapse of vision) — you cannot "
    "know any of that from a synopsis.\n"
    "- NOT dismiss the user's external signals (an IMDb/TMDB rating, their own "
    "knowledge) as 'noise' or 'irrelevant'. With no data of your own, THEIR "
    "signal outweighs your inference — engage it honestly.\n"
    "- Stay explicitly LOW-confidence and lean toward KEEPING / deferring until "
    "the title is properly enriched, rather than pressing for deletion."
)


def _get_vocab_guideline(category: str, genres_str: str) -> str:
    """
    Return exactly one domain-specific vocabulary guideline for this item.
    Only this single string enters the prompt — not the full dict.

    Priority order mirrors your select_guideline logic:
      1. Music (category beats genre — music is never misclassified as doco)
      2. Documentary (genre tag, case-insensitive)
      3. Anime (category OR genre tag — Sonarr sometimes sets category=show)
      4. Show
      5. Movie (default)
    """
    genres_lower = (genres_str or "").lower()

    if category == "music":
        return _VOCAB_GUIDELINES["music"]
    if "documentary" in genres_lower or "docuseries" in genres_lower:
        return _VOCAB_GUIDELINES["documentary"]
    if category == "anime" or "anime" in genres_lower:
        return _VOCAB_GUIDELINES["anime"]
    if category == "show":
        return _VOCAB_GUIDELINES["show"]
    return _VOCAB_GUIDELINES["movie"]


def _taste_section(summary_text: str, category: str) -> str:
    """Pick the taste-summary section matching this deletion category.

    The taste summary is authored as ``[MOVIE] … [SHOW] … [ANIME] … [MUSIC] …``
    blocks (compute_all_taste_vectors summarises all four when each has >=5
    entries — music included, e.g. "electro-house and hardstyle, German rap's
    incisive lyricism"). The old code took ``summary_text[:400]``, which ALWAYS
    returned the [MOVIE] block — so anime/show/music pitches were judged against
    the user's FILM taste ("cerebral tension", "psychological thrillers"), not
    their actual taste in the medium (the Skate-Leading pitch critiquing it for
    lacking "cerebral dissonance" — a film yardstick). This returns the block
    matching THIS category. If it's genuinely absent (a user with <5 music plays,
    or one who hasn't recomputed since music was added), we return "" — never the
    film blurb — so the pitch falls back to taste-free framing, not a wrong domain.
    """
    import re
    s = summary_text or ""
    if not s:
        return ""
    tag = {"movie": "MOVIE", "show": "SHOW", "anime": "ANIME",
           "music": "MUSIC"}.get(category, (category or "").upper())
    m = re.search(rf"\[{tag}\]\s*(.*?)(?=\n\s*\[[A-Z]+\]|\Z)", s, re.S)
    return m.group(1).strip()[:600] if m else ""


# ── Enrichment cache lookup for rating context ────────────────────────────────

def _get_cached_rating(item: dict, category: str) -> tuple:
    """
    Look up TMDB/AniList rating from the enrichment SQLite cache.

    Returns (rating_context_str, rating_float | None, cached_genres_list)
    rating_context_str is injected into the deletion prompt.
    """
    tmdb_id  = item.get("tmdb_id")
    tvdb_id  = item.get("tvdb_id")
    title    = item.get("title", "")

    # Mirror the cache key logic in media_enricher.py
    id_key    = tmdb_id or tvdb_id or title[:40]
    cache_key = f"enriched:{category}:{id_key}"

    try:
        from src.cache.metadata_cache import MetadataCache
        cache = MetadataCache()
        cached = cache.get_cache(cache_key)
        cache.close()

        if cached:
            profile    = cached.get("response", {})
            rating     = profile.get("rating")
            vote_count = profile.get("vote_count") or 0
            genres_raw = profile.get("genres") or []

            # Pass 54: a rating of exactly 0 is NOT a 0/10 verdict — it's the
            # "no rating data" sentinel. TMDB returns 0 for titles with no
            # votes, AniList's averageScore is null (→ 0 here) for fresh /
            # niche titles. Treating it as a real score punished obscure
            # small titles hard (rating_swing of -20 in the deletion score)
            # and fed the curator a literal "zero rating" pitch argument.
            # Require a positive rating to count; 0 falls through to the
            # caller's neutral-5.5 default. ``genres_raw`` is still returned
            # so cached genres survive the rating gate.
            if rating is not None and float(rating) > 0:
                reliability = "limited votes — treat as indicative" if vote_count < 200 else f"{vote_count:,} votes"
                ctx = f"External rating: {rating:.1f}/10 ({reliability})"
                return ctx, float(rating), genres_raw
            # Rating absent or zero — still hand back the cached genres.
            return None, None, genres_raw
    except Exception as exc:
        logger.debug("Enrichment cache lookup failed for '%s': %s", title, exc)

    return None, None, []


def _aggregate_arr_rating(item: dict) -> tuple[float, dict]:
    """Aggregate ratings across every source the ARR item exposes.

    Radarr v3 nests ratings by source:
      ``{"tmdb":{"value":7.5,"votes":1000}, "imdb":{"value":7.2,"votes":5000},
         "rottenTomatoes":{"value":85,"votes":0}, "metacritic":{"value":78,"votes":0}}``

    Strategy:
      * Pull the value from each known source.
      * Normalise 0-100 scales (RT %, Metacritic) → 0-10.
      * Weight TMDB / IMDb by their vote counts, audience scores
        (RT / Metacritic) by a fixed editorial weight (200) so they have
        meaningful pull but don't dominate when a film has 50k IMDb votes.
      * If only one source has data, return that source — no averaging
        side-effects.

    Returns ``(weighted_avg_0_10, breakdown)`` where ``breakdown`` lists the
    contributing sources for logging / display, e.g.
    ``{"tmdb": 7.5, "imdb": 7.2, "rt": 8.5, "metacritic": 7.8}``.
    """
    ratings = item.get("ratings") or {}
    if not isinstance(ratings, dict):
        # Pre-Radarr-v3 flat format — fall back to vote_average.
        legacy = item.get("vote_average", 0) or 0
        return (float(legacy), {"legacy": float(legacy)} if legacy else {})

    breakdown: dict[str, float] = {}
    weighted_sum = 0.0
    weight_total = 0.0

    # Editorial weight for RT / Metacritic — lets them show up even when their
    # 'votes' field is empty in Radarr's payload. Tuned so a single critic
    # score sits roughly at-par with a few hundred IMDb votes.
    EDITORIAL_WEIGHT = 200.0

    for source_key, normalised_key, is_percent in (
        ("tmdb",            "tmdb",       False),
        ("imdb",            "imdb",       False),
        ("rottenTomatoes",  "rt",         True),
        ("metacritic",      "metacritic", True),
    ):
        src = ratings.get(source_key) or {}
        if not isinstance(src, dict):
            continue
        val = src.get("value") or 0
        votes = src.get("votes") or 0
        if not val:
            continue
        if is_percent and val > 10:
            val = val / 10  # 85% RT → 8.5
        # Audience scores → use vote count as weight; critic scores → editorial.
        weight = max(votes, 0) if not is_percent else EDITORIAL_WEIGHT
        if weight <= 0:
            weight = EDITORIAL_WEIGHT  # zero-vote sources still count, just less.
        weighted_sum += float(val) * weight
        weight_total += weight
        breakdown[normalised_key] = round(float(val), 2)

    # Flat / legacy format on top of nested (some Sonarr versions).
    if not breakdown:
        flat = ratings.get("value", 0) or 0
        if flat:
            breakdown["flat"] = float(flat)
            return (float(flat), breakdown)

    if weight_total <= 0:
        return (float(item.get("vote_average", 0) or 0), breakdown)

    return (weighted_sum / weight_total, breakdown)


def _extract_arr_rating(item: dict) -> float:
    """Backwards-compatible wrapper — returns the aggregated rating only."""
    return _aggregate_arr_rating(item)[0]


CATEGORY_LABELS = {
    "music": "🎵 Music",
    "movie": "🎬 Movies",
    "show":  "📺 TV Shows",
    "anime": "⛩️ Anime",
}


async def _call_llm(prompt: str, max_tokens: int = 800, skip_priority: bool = False) -> Optional[str]:
    """Call curator model, fall back to base model. Signals enrichment to pause.

    Pass skip_priority=True when the caller manages the curator lifecycle itself
    (e.g. a batch loop that wraps all calls in a single curator_start/done).
    """
    from src.services.llm_priority import curator_start, curator_done
    if not skip_priority:
        await curator_start()
    try:
        for model in [settings.CURATOR_MODEL, settings.BASE_CURATOR_MODEL]:
            if not model:
                continue
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    r = await client.post(
                        f"{settings.effective_ollama}/api/chat",
                        json={
                            "model": model,
                            "messages": [{"role": "user", "content": prompt}],
                            "stream": False,
                            "keep_alive": CURATOR_KEEP_ALIVE,
                            **ollama_options(temperature=0.7, num_predict=max_tokens),
                        },
                    )
                if r.status_code == 200:
                    content = strip_think_tags(
                        r.json().get("message", {}).get("content", "").strip()
                    )
                    logger.debug("LLM response (%d chars) from %s", len(content), model)
                    return content
                logger.warning("LLM HTTP %s from model %s", r.status_code, model)
                if r.status_code == 404:
                    continue
            except httpx.TimeoutException:
                logger.warning("LLM timeout on model %s", model)
            except Exception as e:
                logger.warning("LLM call failed (%s): %s", type(e).__name__, e)
        return None
    finally:
        if not skip_priority:
            curator_done()


async def generate_recommendations(
    user_id: int,
    category: str = None,
    limit: int = 10,
    arr_library: list = None,  # list of {title, genres, year, ...} from ARR
    force_refresh: bool = False  # Allows bypassing the cache
) -> list:
    """
    Generate recommendations with LLM pitch.
    If arr_library is provided, recommend from those items.
    Otherwise ask the LLM to suggest based on taste alone.
    """

    # Pass 40: detect the user's language once so each pitch can be
    # generated in the same language they chat in. The directive line
    # is later injected into every prompt below. One cheap SELECT per
    # call — only the cache-miss path actually uses the result.
    from src.services.llm_utils import detect_user_language, language_directive
    with get_db_session() as _ld_db:
        _lang_code = detect_user_language(user_id, _ld_db)
    lang_directive_str = language_directive(_lang_code)

    # 1. Open the DB session for the cache check
    with get_db_session() as db:
        if not force_refresh:
            cache_q = db.query(CachedRecommendation).filter(
                CachedRecommendation.user_id == user_id
            )
            if category:
                cache_q = cache_q.filter(CachedRecommendation.category == category)

            cached_items = cache_q.all()
            if cached_items and _cache_is_fresh(cached_items):
                logger.info("Loading recommendations from cache for user %d", user_id)
                return [
                    {
                        "title": c.title,
                        "reason": c.reason,
                        "confidence": c.confidence,
                        "genres": c.genres,
                        "category": c.category,
                        "category_label": CATEGORY_LABELS.get(c.category, c.category)
                    } for c in cached_items
                ]
            elif cached_items:
                logger.info("Recs cache stale for user %d (sync/enrichment changed) — regenerating",
                            user_id)

        # 2. Kein Cache vorhanden oder Refresh erzwungen: Taste Context sammeln
        tv = db.query(TasteVectorEntry).filter(
            TasteVectorEntry.user_id == user_id
        ).first()
        if not tv:
            return []

        type_data = json.loads(tv.genre_affinity or "{}")
        summary_text = tv.summary_text or ""

        watched_q = db.query(WatchHistoryEntry.series_title, WatchHistoryEntry.title).filter(
            WatchHistoryEntry.user_id == user_id
        )
        if category:
            watched_q = watched_q.filter(WatchHistoryEntry.media_type == category)
        watched = {r.series_title or r.title for r in watched_q.all()}

    # --- Generierungs-Logik ---
    cats = [category] if category else list(type_data.keys())
    all_recs = []

    for cat in cats:
        ts = type_data.get(cat)
        if not ts or not isinstance(ts, dict):
            continue

        top_genres = list((ts.get("genre_affinity") or {}).keys())[:6]
        top_themes = list((ts.get("themes") or {}).keys())[:5]
        top_moods = list((ts.get("moods") or {}).keys())[:4]
        top_titles = ts.get("top_titles", [])[:8]

        import re
        match = re.search(rf'\[{cat.upper()}\]([^\[]*)', summary_text)
        cat_summary = match.group(1).strip() if match else ""

        if arr_library:
            unwatched = [
                item for item in arr_library
                if item.get("title") not in watched
            ][:50]

            if not unwatched:
                continue

            # Ground the candidate list with cached verified context (themes +
            # a short plot) — NO LLM, cache-only — so the curator selects AND
            # pitches from real data instead of just title+genres. These are the
            # user's own library items, so they're already enriched; this is just
            # fast cache reads, and the pitch the user sees is now grounded.
            # Honour the verified-data DEMAND here too (not just deletion): use
            # ensure_verified_data so each candidate gets its on-demand OMDb +
            # Wikipedia significance fetched right before the curator pitches it.
            # Concurrent + per-item time-boxed (inside ensure) + cached after the
            # first run, so a recommendation pass warms the library's significance
            # over time instead of pitching from a thin profile.
            from src.services.media_enricher import ensure_verified_data
            from src.services.episodic_memory import retrieve_considerations
            import asyncio as _asyncio

            async def _cand_line(i):
                line = f"- {i['title']} ({i.get('year', '?')}) — {i.get('genres', '')}"
                try:
                    vd = await ensure_verified_data(
                        i["title"], cat,
                        tmdb_id=i.get("tmdb_id"), tvdb_id=i.get("tvdb_id"),
                        anilist_id=i.get("anilist_id"),
                        plex_rating_key=i.get("plex_rating_key"),
                    )
                except Exception:
                    vd = None
                if vd:
                    th = vd.get("themes") or []
                    if th:
                        line += " | themes: " + ", ".join(str(t) for t in th[:3])
                    sig = vd.get("significance")
                    if sig:
                        line += " | significance: " + str(sig)[:160]
                    plot = vd.get("plot")
                    if plot:
                        line += " | " + str(plot)[:90]
                # Learned value-considerations — the SAME bridge that protects
                # items on the deletion side, applied with the opposite sign here:
                # does the user have standing keep/value feedback (a treasured
                # franchise, a partner favourite, cultural weight) that plausibly
                # applies to THIS candidate? If so surface it so the curator can
                # give taste-fitting, user-valued items clear preference. Profile
                # built from the verified themes/plot when present (richer match)
                # else title+genres. Embeds one short string — marginal next to
                # the verified-data fetch already happening in this gather.
                prof_parts = [str(i.get("title") or ""), str(i.get("genres") or "")]
                if vd:
                    _th = vd.get("themes") or []
                    if _th:
                        prof_parts.append(", ".join(str(t) for t in _th[:5]))
                    _pl = vd.get("plot") or ""
                    if _pl:
                        prof_parts.append(str(_pl)[:200])
                prof = " — ".join(p for p in prof_parts if p.strip())
                try:
                    cons = await retrieve_considerations(
                        user_id, prof, media_category=cat, top_k=2)
                except Exception:
                    cons = []
                # Precision guard for the VISIBLE ⭐ surfacing (stricter than the
                # deletion side's silent, capped score nudge — a wrong tag the
                # user reads is worse than a missed one). retrieve_considerations
                # is recall-leaning and anisotropic embeddings let a strong, broad
                # NULL-category memory match cross-domain on pure embedding alone
                # (e.g. a music memory firing on "The Pianist"). Such bleed has an
                # EMPTY lexical overlap; genuine generalisations keep a lexical
                # anchor ("franchise", "anime", a title token) or an exact-category
                # match. Require one of those before showing the user a ⭐.
                cons = [c for c in cons
                        if c.get("overlap") or c.get("media_category") == cat]
                if cons:
                    line += " | ⭐ USER VALUES: " + "; ".join(
                        str(c.get("content") or "")[:80] for c in cons[:2])
                return line

            items_text = "\n".join(
                await _asyncio.gather(*[_cand_line(i) for i in unwatched[:30]])
            )

            prompt = f"""[MODE: ELITE RECOMMENDATION PITCH]
You are Curatarr, a highly analytical and slightly opinionated personal media curator.

{lang_directive_str}

USER'S {cat.upper()} TASTE PROFILE:
{cat_summary or f"Top genres: {', '.join(top_genres)}. Often watches: {', '.join(top_titles[:5])}."}

AVAILABLE {cat.upper()} LIBRARY (unwatched):
{items_text}

MISSION: Select the best {min(limit, 5)} recommendations from the library above.

CRITICAL RULES AND GUARDRAILS:
1. THE PITCH: For each item, write exactly ONE sentence explaining specifically why it fits the user.
2. SYNTHESIZE, DON'T QUOTE: Read the Taste Profile to UNDERSTAND the user, then describe the recommended item in YOUR OWN vocabulary. The profile is reference material for you, not a phrasebook to echo back at them.
{_blacklist_rule(3)}
4. NO LAZY ANCHORING: DO NOT explicitly name titles from the Taste Profile (e.g., "If you liked [Title], you'll love this"). The pitch must stand on its own merits.
5. STANDING USER VALUES: An item tagged "⭐ USER VALUES: …" carries a preference the user has TAUGHT you over time — a treasured franchise, a partner's favourite, cultural/archival weight. When such an item ALSO fits the taste profile, give it clear preference; reflecting what they value is part of the job. But the value is a booster and tie-breaker, NOT an override — do not recommend an item that plainly clashes with the taste profile just because it's flagged.
6. JSON ONLY: Output as a strictly valid JSON array.

Output format:
[{{"title": "Exact Title", "reason": "Your elite 1-sentence pitch.", "confidence": 0.0-1.0}}]"""

        else:
            noun = "tracks/artists" if cat == "music" else "titles"
            prompt = f"""[MODE: ELITE EXTERNAL DISCOVERY]
You are Curatarr, a highly analytical and slightly opinionated personal media curator.

{lang_directive_str}

USER'S {cat.upper()} TASTE PROFILE:
{cat_summary or f"Top genres: {', '.join(top_genres)}. Often watches: {', '.join(top_titles[:5])}."}

ALREADY WATCHED/LISTENED TO (DO NOT RECOMMEND THESE):
{', '.join(list(watched)[:15])}

MISSION: Suggest {limit} {cat} {noun} they haven't seen/heard yet.

CRITICAL RULES AND GUARDRAILS:
1. THE PITCH: Each suggestion needs a specific 1-sentence pitch. Be direct and opinionated.
2. SYNTHESIZE, DON'T QUOTE: Use the Taste Profile to UNDERSTAND what fits, then describe each suggestion in YOUR OWN vocabulary. Don't narrate the profile back at the user — they wrote those signals, they don't need them echoed.
{_blacklist_rule(3)}
4. NO LAZY ANCHORING: DO NOT explicitly name titles from the Taste Profile to draw comparisons.
5. JSON ONLY: Output as a strictly valid JSON array.

Output format:
[{{"title": "...", "reason": "Your elite 1-sentence pitch.", "confidence": 0.0-1.0, "genres": "..."}}]"""

        # Pass 42 (A7): bumped from 600 → 1500. Five JSON items with
        # ~50-token sentence pitches each ≈ 300 tokens of content; the
        # remaining 300 used to be eaten by JSON structure overhead +
        # leading whitespace + occasional model preamble, leaving the
        # response truncated mid-string and json.loads silently failing.
        # 1500 keeps the model honest (still concise) but leaves enough
        # headroom that legitimate full responses survive intact.
        response = await _call_llm(prompt, max_tokens=1500)
        if not response:
            continue

        try:
            recs = json.loads(clean_llm_text(response))
            if not isinstance(recs, list):
                continue
            for rec in recs:
                rec["category"] = cat
                rec["category_label"] = CATEGORY_LABELS.get(cat, cat)
                all_recs.append(rec)
        except Exception as e:
            logger.debug("Recommendation parse error: %s", e)

    # 3. Persist the results to the cache
    if all_recs:
        with get_db_session() as db:
            # Drop the old cache for this selection
            del_q = db.query(CachedRecommendation).filter(
                CachedRecommendation.user_id == user_id
            )
            if category:
                del_q = del_q.filter(CachedRecommendation.category == category)
            del_q.delete()

            # Insert the new entries
            for r in all_recs:
                db.add(CachedRecommendation(
                    user_id=user_id,
                    category=r.get("category"),
                    title=r.get("title"),
                    reason=r.get("reason"),
                    confidence=r.get("confidence", 0.7),
                    genres=r.get("genres", "")
                ))
            db.commit()

    return all_recs


# ── Taste-mismatch (semantic distance) helpers ────────────────────────────────

def _normalize_vec(vec):
    """Unit-normalise a vector for cosine math. Returns an np array, or None if
    empty/zero. The taste vector is already ~unit, but the ChromaDB item
    embeddings are stored RAW (norm ~13) — both sides must be normalised or the
    dot product lands far outside [-1, 1] and the mismatch signal collapses."""
    if vec is None:
        return None
    a = np.asarray(vec, dtype=float)
    n = np.linalg.norm(a)
    return a / n if n else None


def _cosine_anchors(cos_vals: list) -> tuple:
    """p10/p90 of a batch of cosines — the stretch anchors that map the model's
    anisotropic, narrow cosine band (movies cluster ~0.6-0.74) onto a full
    [0, 1] mismatch range. Falls back to sensible defaults when too few
    embeddings are present, and guarantees hi > lo."""
    if len(cos_vals) >= 20:
        lo = float(np.percentile(cos_vals, 10))
        hi = float(np.percentile(cos_vals, 90))
    else:
        lo, hi = 0.61, 0.72
    if hi - lo < 1e-3:
        hi = lo + 1e-3
    return lo, hi


def _cosine_to_mismatch(cosine, lo: float, hi: float) -> float:
    """Map a cosine onto a 0-1 taste-mismatch: cos >= hi (best-fit) -> 0,
    cos <= lo (worst-fit) -> 1, linear between. No embedding -> neutral 0.5
    (the historical default for un-enriched items)."""
    if cosine is None:
        return 0.5
    return max(0.0, min(1.0, (hi - cosine) / (hi - lo)))


def _watch_pitch_line(status: dict) -> str:
    """One framed line on the candidate's own Plex watch status for the pitch.

    Deliberately NEUTRAL — watch status cuts BOTH ways and must not become an
    automatic verdict (that's why it nudges no score, only the reasoning):
      • unwatched   → untested clutter OR a deliberate to-watch pick
      • watched 1×  → got the value and done (deletable) OR a one-off they loved
      • watched n×  → re-watches = real attachment (leans keep)
      • abandoned   → a bounce (supports deletion) OR merely on-hold
    The curator weighs which; the line just hands it the fact it was missing."""
    if not status:
        return ("WATCH STATUS: the user has NOT watched this yet — weigh whether it's "
                "untested clutter or a deliberate to-watch pick; 'unwatched' alone "
                "proves neither, so don't treat it as automatic justification.")
    n = status.get("count", 0)
    if status.get("completed"):
        when = f", last seen {status['last'].strftime('%b %Y')}" if status.get("last") else ""
        if n > 1:
            return (f"WATCH STATUS: the user has watched this {n}×{when} — repeat views "
                    f"signal real attachment; a deletion pitch needs a genuine case, not "
                    f"just taste-distance.")
        return (f"WATCH STATUS: the user watched this once{when} — they may have got the "
                f"value and be done (fine to delete), or it may be a one-off they loved. "
                f"Weigh which before pitching.")
    return ("WATCH STATUS: the user STARTED but never finished this — possibly a bounce "
            "(supports deletion), possibly just on-hold. Weigh, don't assume.")


async def generate_deletion_proposals(
    user_id: int,
    arr_items: list,
    category: str = "movie",
    monitor_task=None,
) -> list:
    """Surgical Deletion: Identifies trash, protects classics, respects whitelists.

    Pass 99-fu5: optional ``monitor_task`` enables intra-function progress
    reporting via task_monitor. Without it, the function ran for 1-3
    minutes per category showing "Analysing N items" with no inner movement
    — and ~80% of the time was actually phase B (LLM pitch generation for
    the top 10 candidates), invisible to the user. Now the message updates
    at phase boundaries and per-pitch so it's obvious what's happening.
    """
    def _msg(text: str) -> None:
        """Best-effort task_monitor message update; no-op when called outside the scheduler/API task path."""
        if monitor_task is None:
            return
        try:
            from src.services.task_monitor import task_monitor as _tm
            _tm.update(monitor_task, message=text)
        except Exception:
            pass

    # Pass 40: detect user's chat language once so each pitch can be
    # written in the same language the user chats in.
    from src.services.llm_utils import detect_user_language, language_directive
    with get_db_session() as _ld_db:
        _lang_code = detect_user_language(user_id, _ld_db)
    lang_directive_str = language_directive(_lang_code)

    with get_db_session() as db:
        # 1. Load Taste Vector
        tv = db.query(TasteVectorEntry).filter(TasteVectorEntry.user_id == user_id).first()
        user_vector = None
        taste_blurb = ""
        if tv:
            # Use the taste section that matches THIS category — not a blind
            # [:400] that always returned the [MOVIE] block (which judged anime/
            # show/music pitches by the user's film taste).
            taste_blurb = _taste_section(tv.summary_text, category)
            encrypted = db.query(EncryptedTasteVector).filter(
                EncryptedTasteVector.user_id == user_id,
                EncryptedTasteVector.media_category == category
            ).first()
            if encrypted and encrypted.encrypted_blob:
                user_vector = json.loads(encrypted.encrypted_blob).get("embedding")

        # 2. Load Whitelist (ProtectedMedia)
        protected = {
            p.identifier for p in db.query(ProtectedMedia).filter(ProtectedMedia.user_id == user_id).all()
        }

        # 3. Pass 37: pre-compute the set of candidate titles the curator
        # has positioned on in recent chat. The cold-metadata pitch path
        # doesn't know about prior discussions, so it would happily
        # propose deleting something the curator endorsed two days ago
        # ("you yourself said Nukitashi is the gold standard for
        # transgressive art … but the algorithm now wants to delete it
        # for being superficial") — the user can weaponize that
        # contradiction in the discuss thread, with no defensible
        # comeback from the curator. Skipping these items means we only
        # auto-propose deletion for titles the curator has NOT staked a
        # position on in chat. Manual deletion via the UI still works.
        from src.database.models import ConversationMessage
        from datetime import datetime as _dt, timedelta as _td
        cutoff = _dt.utcnow() - _td(days=60)
        candidate_titles = {item.get("title") for item in arr_items if item.get("title")}
        curator_positioned: set[str] = set()
        for t in candidate_titles:
            try:
                hit = (
                    db.query(ConversationMessage.id)
                    .filter(
                        ConversationMessage.user_id == user_id,
                        ConversationMessage.role == "assistant",
                        ConversationMessage.content.like(f"%{t}%"),
                        ConversationMessage.created_at >= cutoff,
                    )
                    .limit(1)
                    .first()
                )
                if hit:
                    curator_positioned.add(t)
            except Exception as e:
                logger.debug("[deletions] curator-stance check failed for %r: %s", t, e)
        if curator_positioned:
            logger.info(
                "[deletions] %d/%d candidates skipped — curator already positioned on them in chat: %s",
                len(curator_positioned), len(candidate_titles),
                ", ".join(sorted(curator_positioned)[:5])
                + (", ..." if len(curator_positioned) > 5 else ""),
            )

        # 4. Pass 38: titles the user has actively engaged with in the
        # last 90 days. Like Pass 37 (which catches titles the curator
        # discussed) but watch-history-driven — scales to libraries where
        # the user hasn't chatted about every item but still has clear
        # consumption signals. WatchHistory is the strongest "this is
        # being used" indicator we have; pitching deletion of something
        # the user actively watched would be either contradictory
        # (mid-binge) or premature (just finished). We err conservative:
        # ANY watch activity in 90 days → skip auto-pitch.
        from src.database.models import WatchHistoryEntry
        watched_cutoff = _dt.utcnow() - _td(days=90)
        recent_watched: set[str] = set()
        try:
            rows = (
                db.query(WatchHistoryEntry.title, WatchHistoryEntry.series_title)
                .filter(
                    WatchHistoryEntry.user_id == user_id,
                    WatchHistoryEntry.viewed_at >= watched_cutoff,
                )
                .all()
            )
            for r in rows:
                if r.title:
                    recent_watched.add(r.title)
                if r.series_title:
                    recent_watched.add(r.series_title)
        except Exception as e:
            logger.debug("[deletions] watch-history veto check failed: %s", e)
        # Count overlap with candidate pool — gives a useful "how many of
        # YOUR plex items are recently watched" stat in the log without
        # dumping the whole set (which can be hundreds of titles).
        recent_watched_in_pool = candidate_titles & recent_watched
        if recent_watched_in_pool:
            logger.info(
                "[deletions] %d/%d candidates skipped — recently watched (last 90d): %s",
                len(recent_watched_in_pool), len(candidate_titles),
                ", ".join(sorted(recent_watched_in_pool)[:5])
                + (", ..." if len(recent_watched_in_pool) > 5 else ""),
            )

    import math

    # ── Pass 82c: Plex user-rating lookup for music ──────────────────────────
    # Music-only because Kometa overwrites ``userRating`` on movies/shows
    # with aggregated platform ratings, so it isn't a personal-opinion
    # signal there. Aggregated per artist_name (lowercase) since Lidarr
    # proposals are artist-scoped — a single ≥4-star track is enough to
    # protect the whole artist.
    HARD_PROTECT_THRESHOLD = 8.0      # Plex 0-10 scale; 8.0 = 4 stars
    user_rating_by_artist: dict[str, float] = {}
    if category == "music":
        try:
            from src.database.models import PlexRating
            from sqlalchemy import func as _sqlfunc
            with get_db_session() as _rdb:
                rating_rows = (
                    _rdb.query(
                        _sqlfunc.lower(PlexRating.artist_name).label("artist_lc"),
                        _sqlfunc.max(PlexRating.rating).label("max_rating"),
                    )
                    .filter(
                        PlexRating.user_id == user_id,
                        PlexRating.artist_name.isnot(None),
                    )
                    .group_by(_sqlfunc.lower(PlexRating.artist_name))
                    .all()
                )
            user_rating_by_artist = {
                (r.artist_lc or ""): float(r.max_rating)
                for r in rating_rows if r.artist_lc
            }
            if user_rating_by_artist:
                hard_protected_count = sum(
                    1 for v in user_rating_by_artist.values()
                    if v >= HARD_PROTECT_THRESHOLD
                )
                logger.info(
                    "[deletions] loaded %d artist ratings (%d at ≥4 stars → hard-protected)",
                    len(user_rating_by_artist), hard_protected_count,
                )
        except Exception as e:
            logger.warning("[deletions] artist-rating preload failed: %s", e)

    _msg(f"{category}: scoring {len(arr_items):,} candidates (ChromaDB + taste vector)…")
    scored_candidates = []
    prelim: list[dict] = []                # all candidates, pre-calibration
    hard_protect_skipped: list[str] = []   # for the post-loop log line
    user_vector_n = _normalize_vec(user_vector)   # unit vector for cosine math
    for item in arr_items:
        title = item.get("title")
        tmdb_id = str(item.get("tmdb_id")) if item.get("tmdb_id") is not None else ""

        if title in protected or tmdb_id in protected:
            continue

        # Pass 37: skip auto-pitches for titles the curator has discussed
        # in chat — avoids the "your last message praised this, this
        # message proposes deleting it" self-contradiction trap.
        if title and title in curator_positioned:
            continue

        # Pass 38: skip auto-pitches for titles the user has actively
        # consumed in the last 90 days — recent engagement is the
        # strongest "this matters to me" signal, and the cold-metadata
        # mismatch score has no way to see it.
        if title and title in recent_watched:
            continue

        # Pass 39: skip kids/family/children content. Items like Tom and
        # Jerry get evaluated against the user's prestige-TV / anime
        # taste profile and produce absurd pitches ("lacks narrative
        # propulsion / thematic depth"). The format is supposed to be
        # repetitive slapstick — judging it on those axes is a category
        # error. If the user wants to delete kids content, they'll do
        # it manually; the auto-pitch shouldn't waste pixels on it.
        item_genres = item.get("genres", "")
        if isinstance(item_genres, list):
            item_genres = ", ".join(item_genres)
        gl = (item_genres or "").lower()
        if "children" in gl or "family" in gl or "kids" in gl:
            continue

        # Pass 82c: Plex-user-rating gate (music only). Look up the max
        # rating across ANY track/album/artist row tied to this artist
        # name; ≥ 4 stars (Plex 8.0) hard-excludes the candidate. The
        # soft-bias for 0 < rating < 8 is applied further down in the
        # score formula.
        user_rating = None
        if category == "music" and title:
            user_rating = user_rating_by_artist.get(title.lower())
            if user_rating is not None and user_rating >= HARD_PROTECT_THRESHOLD:
                hard_protect_skipped.append(title)
                continue

        # ── Rating: aggregate ALL ARR sources (TMDB + IMDb + RT + Metacritic),
        #          then fall back to enrichment cache (TMDB), then neutral. ──
        arr_rating, rating_breakdown = _aggregate_arr_rating(item)

        # Check enrichment cache at scoring time so well-rated items are never
        # unfairly proposed just because their ARR metadata field is empty/zero.
        _, cached_rating, _ = _get_cached_rating(item, category)

        if arr_rating > 0:
            # ARR has multi-source data — that's our richest signal.
            effective_rating = arr_rating
        elif cached_rating and cached_rating > 0:
            # Pass 54: explicit > 0 guard. ``_get_cached_rating`` already
            # returns None for a zero/absent rating, but keeping the check
            # here too means a future refactor of that helper can't
            # silently let a 0 back in as a real score.
            effective_rating = cached_rating
            rating_breakdown = {"tmdb_cache": round(cached_rating, 2)}
        else:
            # No rating data — use neutral to avoid punishing unknown-quality items.
            # 5.5 ≈ "slightly below average" on 0-10; enough protection to avoid
            # false-positive proposals while still allowing genuine trash through.
            # This is the branch obscure small titles (no TMDB votes, AniList
            # averageScore still null) land in after Pass 54 — they get judged
            # on taste-fit alone, not punished for a phantom 0/10.
            effective_rating = 5.5
            rating_breakdown = {}

        size_gb = (item.get("size_mb", 0) or 0) / 1024

        # ── Semantic taste-mismatch: proper cosine (user taste vector vs the
        # item's ChromaDB embedding). The lookup id is the enrichment-pipeline
        # key "{service}:{arr_id}", carried on the candidate as plex_rating_key
        # (Pass 64). Both vectors MUST be normalised: the stored embeddings are
        # raw (norm ~13), so a bare np.dot against the unit taste vector landed
        # at ≈ 8-14 — far outside [-1, 1] — which the old ``1 - dist`` clamp
        # pinned to 0, silently killing the dominant deletion signal. The cosine
        # is mapped to a 0-1 mismatch AFTER the loop, calibrated against this
        # batch's own (anisotropic, narrow) cosine distribution.
        doc_id = str(item.get("plex_rating_key") or item.get("tmdb_id") or title)
        item_vector_res = chroma_db.get_by_id(doc_id)
        item_vec = item_vector_res.get("embedding") if item_vector_res else None
        cosine = None
        if item_vec is not None and user_vector_n is not None:
            try:
                iv = np.asarray(item_vec, dtype=float)
                ivn = np.linalg.norm(iv)
                if ivn:
                    cosine = float(np.dot(user_vector_n, iv) / ivn)
            except Exception:
                cosine = None

        prelim.append({
            "item": item,
            "cosine": cosine,
            "size_gb": size_gb,
            "rating": effective_rating,
            "rating_breakdown": rating_breakdown,
            "arr_rating": arr_rating,
            "cached_rating": cached_rating,
            "user_rating": user_rating,
        })

    # Pass 82c: audit trail — log which artists were hard-protected by the
    # user's ≥ 4-star Plex rating. Helps explain "why isn't <artist> in the
    # deletion list?" without forcing the user to dig through the DB.
    if hard_protect_skipped:
        preview = ", ".join(sorted(hard_protect_skipped)[:5])
        more = f", +{len(hard_protect_skipped) - 5} more" if len(hard_protect_skipped) > 5 else ""
        logger.info(
            "[deletions] hard-protected by ≥ 4-star user rating: %d artist(s) — %s%s",
            len(hard_protect_skipped), preview, more,
        )

    # ── Calibrate taste-mismatch against THIS batch's cosine distribution ────
    # Embedding cosines are anisotropic — they cluster in a narrow band, so a
    # raw (1 - cosine) hardly varies. Stretch the batch's p10..p90 band onto
    # [0, 1] so the mismatch — and the resulting confidence — spreads across the
    # full range and reflects real RELATIVE taste-fit. Items with no embedding
    # get a neutral 0.5. Scoring (size + rating swings) is unchanged from here.
    _cos_vals = [p["cosine"] for p in prelim if p["cosine"] is not None]
    _lo, _hi = _cosine_anchors(_cos_vals)
    logger.info(
        "[deletions] %s: %d/%d candidates have embeddings; cosine anchors p10=%.3f p90=%.3f",
        category, len(_cos_vals), len(prelim), _lo, _hi,
    )
    # Tech-profile index for outlier-based size scoring — loaded once (a DB query
    # per candidate would be thousands). Empty when the tech sync hasn't run yet,
    # in which case size_pts falls back to today's blanket log1p below.
    from src.services.size_norms import load_tech_index, size_outlier
    _tech_idx = load_tech_index()
    for p in prelim:
        mismatch = _cosine_to_mismatch(p["cosine"], _lo, _hi)
        # Vector mismatch is the dominant signal (0-80). Size is logarithmic so
        # huge files don't dominate; rating swing is protective above 5/10 and
        # penalising below. USER rating swing (music only) weights the user's own
        # star rating heaviest. Same coefficients as before — only the mismatch
        # input is now a real, calibrated cosine instead of a flat constant.
        # Size penalty — OUTLIER-based, not blanket. A 4K film or a many-specials
        # series that is normal-per-minute for its class gets ~0; only genuine
        # bitrate-bloat is penalised (capped at 25 so it never dominates the taste
        # mismatch). Falls back to the old log1p blanket when no tech profile
        # exists (tech sync not run / item not in Plex), so nothing regresses.
        _it = p["item"]
        _tp = (_tech_idx.get(("tmdb", _it.get("tmdb_id")))
               or _tech_idx.get(("tvdb", _it.get("tvdb_id"))))
        _so = size_outlier(_tp["media_type"], _tp["resolution"], _tp["codec"],
                           _tp["mb_per_min"]) if _tp else None
        if _so:
            size_pts = (min(25.0, (_so["ratio"] - 1.0) * 12.0)
                        if _so["verdict"] == "bloated" else 0.0)
        else:
            size_pts = math.log1p(p["size_gb"]) * 5
        rating_swing = (p["rating"] - 5.0) * 4
        user_rating_swing = (
            (p["user_rating"] - 5.0) * 6 if p["user_rating"] is not None else 0.0
        )
        del_score = mismatch * 80 + size_pts - rating_swing - user_rating_swing
        if del_score > 30:
            scored_candidates.append({
                "item": p["item"],
                "score": del_score,
                "mismatch": mismatch,
                "rating_breakdown": p["rating_breakdown"],
                "rating": p["rating"],
                "arr_rating": p["arr_rating"],
                "cached_rating": p["cached_rating"],
                "user_rating": p["user_rating"],   # Plex stars (music) for the pitch
            })

    scored_candidates.sort(key=lambda x: x["score"], reverse=True)

    # ── Learned considerations (the "three pillars" and beyond) ───────────────
    # Let the user's standing "keep / value" preferences — kept franchises,
    # partner favourites, cultural value, and ANY other keep-feedback they've
    # given — pull a candidate DOWN the deletion list. Retrieved PER ITEM so a
    # principle the user stated about ONE title generalises to new, never-
    # discussed ones (the whole point: the curator should learn from feedback,
    # not just remember it). SOFT and capped: a strong taste mismatch still
    # surfaces; this never hard-protects. Only the top slice is scored (the only
    # items anywhere near being pitched), to bound the per-item embedding calls.
    from src.services.episodic_memory import retrieve_considerations
    _CONSIDER_CAP = 45.0
    _consider_slice = scored_candidates[:30]
    for cand in _consider_slice:
        it = cand["item"]
        g = it.get("genres") or it.get("genre") or ""
        if isinstance(g, list):
            g = ", ".join(str(x) for x in g)
        profile = " — ".join(p for p in (
            str(it.get("title") or ""),
            str(g),
            (it.get("overview") or it.get("synopsis") or "")[:200],
        ) if p.strip())
        try:
            cons = await retrieve_considerations(
                user_id, profile, media_category=category, top_k=3)
        except Exception as e:
            logger.debug("[deletions] considerations lookup failed for %r: %s",
                         it.get("title"), e)
            cons = []
        keep_pts = round(min(_CONSIDER_CAP, sum(c["strength"] for c in cons) * 30.0), 1)
        cand["considerations"] = cons
        cand["keep_value_pts"] = keep_pts
        cand["score"] = cand["score"] - keep_pts
    if any(c.get("keep_value_pts") for c in _consider_slice):
        scored_candidates.sort(key=lambda x: x["score"], reverse=True)
        _before = len(scored_candidates)
        scored_candidates = [c for c in scored_candidates if c["score"] > 30]
        _dropped = _before - len(scored_candidates)
        _protected = [(c["item"].get("title"), c["keep_value_pts"])
                      for c in _consider_slice if c.get("keep_value_pts")]
        logger.info(
            "[deletions] learned considerations nudged %d item(s); %d fell below "
            "the bar (soft-kept). Protected: %s",
            len(_protected), _dropped, _protected[:8],
        )

    final_proposals = []

    # Pass 39: random axis seed per pitch. Single-line nudge in the prompt
    # below — no batch-state tracking, no allocation problem (sampling
    # with replacement). Over 10 pitches the axes naturally distribute,
    # breaking the "every pitch sounds identical" rut without forcing
    # awkward angles when one genuinely doesn't fit (the prompt allows
    # opting out of the suggested axis with explicit instruction NOT to
    # fall back on the blacklisted crutch phrases).
    import random as _rand
    _PITCH_AXES = [
        ("PACING & RHYTHM",         "sluggishness, filler, runtime waste, episodic drag"),
        ("CHARACTER AGENCY",        "passive protagonists, stagnant dynamics, motivations that don't move"),
        ("CRAFT & EXECUTION",       "static frames, low-effort production, sloppy direction, flat mixing — whatever 'low effort' looks like for the medium"),
        ("PREMISE & WORLDBUILDING", "trope reliance, broken internal logic, gimmickry, lazy lore"),
        ("THEMATIC EMPTINESS",      "no satire, no critique, no point — entertainment as wallpaper"),
        ("CULTURAL DATEDNESS",      "era-stuck assumptions, aged-poorly tropes, requires niche cultural memory"),
    ]
    # Music needs its OWN axes — film angles like "character agency" or
    # "premise & worldbuilding" are nonsense for an artist and were leaking
    # plot/film framing into music pitches.
    # Every axis must be answerable from the metadata we actually have (genre,
    # scene, themes, mood tags). The old SONIC PALETTE / PRODUCTION axes asked
    # the curator to judge timbre, mix and "production sheen" it has never
    # heard, so it dutifully invented them — the root of the audio-hallucination
    # pitches. These angles critique fit, not fabricated sound.
    _PITCH_AXES_MUSIC = [
        ("GENRE & SCENE FIT",  "how their genre/scene sits against the user's core sound"),
        ("EMOTIONAL REGISTER", "the mood/intensity their music trades in vs the user's preferred register"),
        ("CULTURAL PLACEMENT", "era/scene baggage — dated, niche or novelty for this listener"),
        ("THEMATIC SUBSTANCE", "the themes their catalogue centres vs what the user values"),
        ("CATALOGUE DEPTH",    "a one-note / novelty act vs a substantive body of work"),
        ("LIBRARY REDUNDANCY", "whether the user's library already owns this lane, done better"),
    ]

    # Load relevant memories — scoped to this category so only domain-relevant
    # memories (e.g. film nostalgia for movie deletions) enter the context window.
    memories = await retrieve_memories(user_id, "deletion reasons nostalgia classics", top_k=5, media_category=category)
    memory_context = format_memories_for_context(memories)

    # Wrap all pitch LLM calls in a single curator session so the curator model
    # stays loaded throughout the batch and the summarizer is only evicted once.
    from src.services.llm_priority import curator_start, curator_done
    top_pitch_set = scored_candidates[:10]
    # Watch status for exactly the items we're about to pitch — ONE query.
    # The pitch otherwise knows the user OWNS the item but not whether they've
    # SEEN it, the signal that separates "untested clutter" from "watched and
    # done" / "re-watched and loved". Injected as neutral context (no score
    # nudge — see _watch_pitch_line), so the curator weighs it like a memory.
    from src.services.watch_status import watched_lookup as _watched_lookup
    watch_status_map = _watched_lookup(
        user_id, [c["item"].get("title") for c in top_pitch_set])
    _msg(
        f"{category}: scoring done ({len(scored_candidates):,} above threshold) — "
        f"generating LLM pitches for top {len(top_pitch_set)}…"
    )
    await curator_start()
    try:
        for _pidx, cand in enumerate(top_pitch_set, start=1):
            item = cand["item"]
            _msg(
                f"{category}: LLM pitch {_pidx}/{len(top_pitch_set)} — "
                f"{(item.get('title') or '?')[:50]}"
            )

            # ── Metadata extraction ───────────────────────────────────────────
            overview      = item.get("overview") or item.get("description") or "No description available."
            overview_short = overview[:800] + "…" if len(overview) > 800 else overview
            year           = item.get("year", "Unknown Year")

            # Genres: prefer enrichment cache (richer tags) over ARR field.
            # Re-use cache values pre-computed in the scoring phase via cand dict.
            cached_rating   = cand["cached_rating"]      # None if not enriched
            arr_rating_raw  = cand["arr_rating"]          # weighted-avg across all ARR sources
            rating_breakdown = cand["rating_breakdown"]   # per-source dict for the prompt
            rating_ctx, _, cached_genres = _get_cached_rating(item, category)

            genres_raw = cached_genres or item.get("genres", [])
            genres_str = ", ".join(genres_raw) if isinstance(genres_raw, list) else str(genres_raw)

            # ── Rating context block ──────────────────────────────────────────
            # Multi-source breakdown when available (TMDB / IMDb / RT / Metacritic
            # all in one block) so the LLM can sanity-check its critique against
            # multiple consensus signals, not just TMDB.
            if rating_breakdown:
                source_labels = {
                    "tmdb":       "TMDB",
                    "imdb":       "IMDb",
                    "rt":         "Rotten Tomatoes (audience score, 0-10)",
                    "metacritic": "Metacritic (0-10)",
                    "tmdb_cache": "TMDB (enrichment cache)",
                    "legacy":     "ARR (legacy flat)",
                    "flat":       "ARR (flat)",
                }
                breakdown_lines = "\n".join(
                    f"    · {source_labels.get(src, src)}: {val:.1f}/10"
                    for src, val in rating_breakdown.items()
                )
                rating_block = (
                    f"RATING (weighted across sources, {arr_rating_raw:.1f}/10):\n"
                    f"{breakdown_lines}"
                )
            elif rating_ctx:
                rating_block = f"RATING: {rating_ctx}"
            elif arr_rating_raw and arr_rating_raw > 0:
                rating_block = f"RATING: {arr_rating_raw:.1f}/10 (source: ARR metadata)"
            else:
                rating_block = (
                    "RATING: No external rating data available for this item. "
                    "DO NOT assume it is low quality. Base your critique ONLY on the "
                    "synopsis and taste mismatch. Defer judgment on quality entirely — "
                    "focus only on relevance fit."
                )

            # Music override: TMDB/IMDb/RT are FILM ratings and do not apply to
            # an artist — surfacing them produced "3.0 TMDB score" hallucinations.
            # Use the user's OWN Plex star rating when present (the real personal
            # signal); otherwise judge on taste-fit alone.
            if category == "music":
                _ur = cand.get("user_rating")
                if _ur and _ur > 0:
                    rating_block = (
                        f"USER RATING: {_ur / 2.0:.1f}/5 stars — the user's OWN "
                        f"rating of this artist (a strong personal signal; a low "
                        f"score means they don't care for them)."
                    )
                else:
                    rating_block = (
                        "RATING: none — the user hasn't rated this artist and no "
                        "objective music score applies. Judge ONLY on how their "
                        "sound fits the user's taste; do NOT invent or cite any "
                        "rating (no TMDB/IMDb — those are for films)."
                    )

            # ── Domain vocabulary guideline ───────────────────────────────────
            vocab_guideline = _get_vocab_guideline(category, genres_str)

            # Pass 39: random critique-axis seed. ONE line in the prompt
            # gently steers the LLM toward a specific angle so we don't
            # see the same hooks ("sakuga density", "narrative propulsion")
            # in every pitch of a batch. ``random.choice`` is sampling
            # with replacement — over 10 pitches the axes distribute
            # probabilistically without the "best axis already used up"
            # problem a deterministic rotation would have.
            axis_label, axis_hint = _rand.choice(
                _PITCH_AXES_MUSIC if category == "music" else _PITCH_AXES
            )

            # Feed the FULL verified dataset we already cached (creator/writer,
            # extended plot, themes, keywords, awards) — assembled with NO LLM,
            # cache-only — so the 27B curator reasons from FACTS instead of a
            # thin synopsis stub or its own training memory (the root of the
            # "Steppenwolf is a Dean Koontz film" hallucinations). Empty string
            # when nothing is cached → falls back to the thin item fields.
            from src.services.media_enricher import ensure_verified_data, format_verified_block
            # Pass EVERY id the candidate carries, not just tmdb_id. The
            # enrichment cache keys anime by anilist_id / tvdb_id and shows by
            # tvdb_id — so a tmdb-only lookup silently MISSED all ~5.3k cached
            # anime profiles, dumping the curator onto the thin synopsis stub →
            # cold-read / plot-inversion (the Skate-Leading bug). tvdb_id alone
            # reaches the cached anime/show data; plex_rating_key adds the
            # prefetch overview.
            verified_block = format_verified_block(
                await ensure_verified_data(
                    item.get("title") or "", category,
                    tmdb_id=item.get("tmdb_id"),
                    tvdb_id=item.get("tvdb_id"),
                    anilist_id=item.get("anilist_id"),
                    anidb_id=item.get("anidb_id"),
                    plex_rating_key=item.get("plex_rating_key"),
                )
            )
            if verified_block:
                item_details = verified_block
            else:
                item_details = (
                    "ITEM DETAILS:\n"
                    f"- Title: {item.get('title')} ({year})\n"
                    f"- Genres: {genres_str or 'Unknown'}\n"
                    f"- Synopsis: {overview_short}"
                )
            # No verified block → the curator has only a thin synopsis. Inject the
            # hedge so it acknowledges the blind spot instead of confabulating
            # confident execution verdicts (the Fringe case).
            data_poor_block = NO_VERIFIED_DATA_HEDGE if not verified_block else ""

            # Learned considerations stashed during scoring — the user's standing
            # keep/value preferences that plausibly apply to THIS item. The curator
            # must WEIGH them (acknowledge the tension), not blindly obey. Empty
            # string for the vast majority of items.
            from src.services.episodic_memory import format_considerations_for_pitch
            considerations_block = format_considerations_for_pitch(
                cand.get("considerations") or [])

            # Candidate's own watch status — neutral context the curator weighs.
            watch_block = _watch_pitch_line(watch_status_map.get(item.get("title")))

            # Size-outlier context (non-music): is this item's GB normal for its
            # resolution/codec class (don't flag) or genuine bitrate bloat (size
            # is a fair argument)? Stops the blanket "70 GB is too big" complaint.
            from src.services.size_norms import size_context_for
            size_ctx_block = "" if category == "music" else size_context_for(
                tmdb_id=item.get("tmdb_id"), tvdb_id=item.get("tvdb_id"),
                plex_rating_key=item.get("plex_rating_key"))

            if category == "music":
                # Music-specific pitch: artist framing, no synopsis, no film
                # ratings, and hard anti-hallucination rules (a band sharing a
                # name with a film is NOT that film — the Steppenwolf trap).
                prompt = f"""[MODE: SURGICAL DELETION PITCH — MUSIC]
You are Curatarr, an uncompromising, elite MUSIC curator. Pitch the deletion of this MUSIC ARTIST from the user's library.

{lang_directive_str}

ARTIST: {item.get('title')}
GENRES / STYLE: {genres_str or 'Unknown'}
{rating_block}
{verified_block}

REASON FOR DELETION CONSIDERATION: their sound is a mismatch with the user's music taste.
{f'USER MUSIC TASTE: {taste_blurb}' if taste_blurb else ''}
{f'KNOWN EXCEPTIONS & MEMORIES: {memory_context}' if memory_context else ''}
{considerations_block}
{data_poor_block}

{vocab_guideline}
{_FORBIDDEN_CROSS_DOMAIN}

PRIMARY CRITIQUE ANGLE for this pitch: {axis_label} ({axis_hint}).
If this angle genuinely doesn't fit, pick a different one — but do NOT default to the blacklisted crutch phrases.

CRITICAL RULES AND GUARDRAILS:
1. MAX 2 SENTENCES. Concise, highly opinionated, ruthless.
2. THIS IS A MUSIC ARTIST — never a film, show, book, episode, or a single "track". Call them an artist / act / band. There is NO plot and NO synopsis. NEVER invent a biography, a film or novel of the same name, an album, a song, a release year, or a rating you were not given. A same-named movie is NOT this artist — do not describe it.
3. CRITIQUE FROM METADATA — YOU HAVE NOT HEARD THEM: you have their genre, scene, themes and mood tags, NOT the audio. Critique how their genre/scene and emotional register clash with the user's sound, using the music vocabulary above. Do NOT invent production, mix, compression, dynamics, timbre or "polish/sheen" qualities you cannot hear, and never cite a rating as proof of how they sound.
4. SYNTHESIZE, DON'T QUOTE: use the USER MUSIC TASTE to understand what they value, then critique in YOUR OWN words — don't lift phrasings.
{_blacklist_rule(5)}
6. NO ANCHORING: do not name specific artists from the user's taste summary.
7. NO ECHOING: never start with "Given your…" or "Since you like…". State why this artist fails to earn its space.
8. NO TECH TALK: no file sizes, gigabytes, or vector distances; and NO film ratings (TMDB / IMDb / Rotten Tomatoes do not apply to music)."""
            else:
                prompt = f"""[MODE: SURGICAL DELETION PITCH]
You are Curatarr, an uncompromising, elite media curator. Pitch the deletion of this item.

{lang_directive_str}

{item_details}
{rating_block}

REASON FOR DELETION CONSIDERATION: Mismatch with user taste profile.
{f'USER TASTE SUMMARY: {taste_blurb}' if taste_blurb else ''}
{f'KNOWN EXCEPTIONS & MEMORIES: {memory_context}' if memory_context else ''}
{considerations_block}
{watch_block}
{data_poor_block}
{size_ctx_block}

{vocab_guideline}
{_FORBIDDEN_CROSS_DOMAIN}

PRIMARY CRITIQUE ANGLE for this pitch: {axis_label} ({axis_hint}).
If this angle genuinely doesn't fit the item, pick a different one — but do NOT default to the blacklisted crutch phrases.

CRITICAL RULES AND GUARDRAILS:
1. MAX 2 SENTENCES. Be concise, highly opinionated, and ruthless.
2. STRICT FACTUAL ACCURACY: Respect the objective qualities, genres, and synopsis. Do NOT invent false negatives to justify deletion.
3. INTRINSIC CRITIQUE: Critique the item's inherent structural or thematic flaws and explain how they clash with the user's demands. Use the domain vocabulary above.
4. SYNTHESIZE, DON'T QUOTE: Use the USER TASTE SUMMARY to UNDERSTAND what they value, then critique the item in YOUR OWN vocabulary. Do not lift adjectives, tropes, or phrasings from the summary — those are reference signals, not template fragments.
{_blacklist_rule(5)}
6. NO ANCHORING: Do not explicitly name titles from the User Taste Summary.
7. NO ECHOING: Never start with "Given your…" or "Since you like…". State why the item fails to earn its space.
8. SIZE TALK — OUTLIERS ONLY: Do not mention file sizes, gigabytes, or vector distances by default. EXCEPTION: if a SIZE CONTEXT line above flags this item as genuinely oversized for its class (bloated), its disproportionate size IS a fair, specific argument you may use. Never raise size when SIZE CONTEXT says it is normal — a big 4K film or a many-episode series is not bloat.
9. PREMISE & FIT — NOT A REVIEW: You have this item's premise, themes and metadata, NOT a screening of it. Argue why its premise / genre / themes CLASH with the user's taste. Do NOT pass verdicts on execution you cannot know — no "static", "hollow", "melodramatic stalemate", "flat", "lands/doesn't land", no claims about pacing, acting or direction — unless that judgement is explicitly in the data above. For fact-based works (history, true events, documentary) a known outcome is NOT a flaw: never call it "predictable"."""

            pitch = await _call_llm(prompt, skip_priority=True)
            # Pass 51: empty-pitch guard. ``_call_llm`` returns None on an
            # HTTP / timeout failure and "" when the model emitted nothing
            # but <think> tags (stripped out by strip_think_tags). Either
            # way an empty pitch reaches the UI as a blank deletion card —
            # a Delete button with no reasoning, which is both useless and
            # slightly alarming. One retry (the curator model is already
            # warm, so this is cheap), then a deterministic honest
            # fallback so the card stays actionable.
            if not pitch or not pitch.strip():
                logger.warning(
                    "[deletions] empty pitch for %r — retrying once", item.get("title"),
                )
                pitch = await _call_llm(prompt, skip_priority=True)
            if not pitch or not pitch.strip():
                logger.warning(
                    "[deletions] pitch still empty for %r — using honest fallback",
                    item.get("title"),
                )
                pitch = (
                    "Flagged as a taste-profile mismatch, but the curator model "
                    "returned an empty pitch — review this one manually."
                )
            size_mb = item.get("size_mb") or 0
            # del_score → confidence:
            # The recalibrated scoring (mismatch 0-80, size 0-25, rating-swing
            # ±20) lands clear bad-fits in the 60-90 range, mediocre fits at
            # 30-50, with a theoretical max around 125. /100 + clamp at 0.99
            # turns that into a meaningful 30-99 % confidence band — the
            # earlier formula capped most realistic candidates near 50 %.
            final_proposals.append({
                "title": item.get("title"),
                "pitch": pitch,
                "confidence": min(0.99, max(0.10, cand["score"] / 100)),
                "size_mb": size_mb,
                "size_gb": round(size_mb / 1024, 1),
                "arr_id": item.get("arr_id"),
                "service": item.get("service", ""),
                "arr_url": item.get("arr_url", ""),
                # Resolving IDs persisted on the proposal so the discussion path
                # can on-demand fast-enrich + cache an un-enriched title later.
                "tvdb_id": item.get("tvdb_id"),
                "tmdb_id": item.get("tmdb_id"),
                # Pass 17: forward the latest file-import timestamp so the
                # caller can persist it on the DeletionProposal row. NULL
                # when /history fetch failed or the item has no recent
                # imports — filter falls through cleanly in either case.
                "latest_activity_at": item.get("latest_activity_at"),
            })
    finally:
        curator_done()

    return final_proposals


# ── PASS 81: LEVEL 2 RE-EVALUATION ────────────────────────────────────────────
#
# The standard deletion pitch is built from surface metadata: genre tags,
# synopsis, ratings, taste-mismatch vector distance. That's the right
# default — fast, cheap, calibrated. But it falls over on "Trojan Horse"
# narratives: shows whose generic premise is a performative mask for
# subversion, deconstruction, or political allegory. The synopsis says
# "high school comedy" but the work is actually an identity-fragmentation
# study; the curator pitches deletion, the user knows better, and the gap
# is a credibility crater.
#
# Re-evaluation is a USER-INITIATED escape hatch: the user clicks the 🔍
# button next to Discuss. The button does NOT run a parallel LLM pipeline
# — it just opens the standard deletion-discussion thread and auto-sends
# a Level-2 challenge prompt as the user's first message. The curator
# answers via the existing /api/chat/message streaming pipeline, the
# verdict shows up live in the chat, the user can immediately follow up
# ("but check director X"), and memory extraction sees the whole thread.
#
# Why this routing instead of a dedicated endpoint: zero new queue, zero
# new LLM lifecycle to manage, streaming UX instead of a 30-90 s spinner,
# and the verdict becomes searchable conversation history instead of an
# appended blob on the proposal card. The Level-2 *prompt* lives in the
# frontend (see ``onReevaluateDeletion`` in index.html) — it's just a
# pre-filled chat input, no server help needed: the chat backend's
# ``_build_discuss_context_block`` already injects the item title +
# original verdict + synopsis as RAG, so the user-side prompt only carries
# the *challenge framing*.


async def score_arr_items(user_id: int, category: str, items: list, top_n: int = 50) -> list:
    """Rank ARR items by taste-fit before the top slice goes to the LLM.

    Pass 70: ranks primarily by SEMANTIC similarity — cosine ``np.dot`` of
    the user's taste embedding against each item's ChromaDB embedding. This
    is the mirror of the Pass-64 deletion scoring (recommend the *closest*,
    delete the *farthest*). The embedding is the real taste signal the engine
    computes; before this it was completely unused on the recommendation
    side — items were ranked only by crude genre-tag overlap.

    Items with no embedding yet (not enriched into ChromaDB) fall back to
    genre-affinity overlap. The two scores live on different scales, so we
    do NOT mix them in one sort: enriched items rank among themselves by
    cosine, un-enriched items among themselves by genre overlap, and the
    enriched group is placed first — we have real taste-fit data for those.
    Small ``monitored`` nudge in both.

    Unlike the old version this ALWAYS ranks — no early-out for small
    libraries. ``generate_recommendations`` only feeds the LLM the first ~30
    items, so order matters even when every item survives the top_n cut.
    """
    with get_db_session() as db:
        tv = db.query(TasteVectorEntry).filter(TasteVectorEntry.user_id == user_id).first()
        type_data = json.loads(tv.genre_affinity or "{}") if (tv and tv.genre_affinity) else {}
        user_vector = None
        encrypted = db.query(EncryptedTasteVector).filter(
            EncryptedTasteVector.user_id == user_id,
            EncryptedTasteVector.media_category == category,
        ).first()
        if encrypted and encrypted.encrypted_blob:
            try:
                user_vector = json.loads(encrypted.encrypted_blob).get("embedding")
            except Exception:
                user_vector = None

    ts = type_data.get(category, {}) if isinstance(type_data, dict) else {}
    genre_affinity = {g.lower(): s for g, s in (ts.get("genre_affinity") or {}).items()}

    # No taste signal at all → nothing to rank by; preserve the old
    # "just take the first top_n" behavior.
    if not user_vector and not genre_affinity:
        return items[:top_n]

    def _monitored_bonus(item: dict) -> float:
        return 0.1 if item.get("monitored") else 0.0

    def _genre_score(item: dict) -> float:
        genres = [g.strip().lower() for g in (item.get("genres") or "").split(",") if g.strip()]
        return sum(genre_affinity.get(g, 0) for g in genres) + _monitored_bonus(item)

    def _vector_score(item: dict):
        """Cosine similarity (user taste vector · item ChromaDB embedding),
        or None when either side is missing."""
        if not user_vector:
            return None
        doc_id = str(item.get("plex_rating_key") or item.get("tmdb_id") or item.get("title") or "")
        if not doc_id:
            return None
        try:
            res = chroma_db.get_by_id(doc_id)
            # Pass 74: ChromaDB embeddings are numpy arrays — check
            # ``is not None``, never truthiness. ``bool(array)`` raises
            # ValueError; the old ``if res.get("embedding")`` raised inside
            # this try/except, so it was silently swallowed and EVERY item
            # fell through to the genre fallback — the pass-70 vector ranking
            # never actually ran.
            emb = res.get("embedding") if res else None
            if emb is not None:
                return float(np.dot(user_vector, emb)) + _monitored_bonus(item)
        except Exception:
            pass
        return None

    vectored: list = []
    genre_only: list = []
    for item in items:
        vs = _vector_score(item)
        if vs is not None:
            vectored.append((vs, item))
        else:
            genre_only.append((_genre_score(item), item))

    vectored.sort(key=lambda t: t[0], reverse=True)
    genre_only.sort(key=lambda t: t[0], reverse=True)
    # Enriched items first (real semantic taste-fit), genre-only fallback after.
    ranked = [it for _, it in vectored] + [it for _, it in genre_only]
    return ranked[:top_n]