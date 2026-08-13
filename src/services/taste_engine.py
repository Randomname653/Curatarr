"""
Curatarr 1.0 - Taste Engine

Computes REAL taste vectors as weighted average embeddings in ChromaDB space.

Pipeline per user per category:
  1. Get all watch history entries for this category
  2. For each entry: look up enriched metadata (themes, mood, tags)
  3. Build a text representation: "Title | genres | themes | mood | tags"
  4. Generate embedding via nomic-embed-text
  5. Weighted average of all embeddings:
       weight = recency_factor * completion_factor * replay_bonus
  6. Store the averaged vector + stats in EncryptedTasteVector (AES-256)
  7. Store plain-text summary in TasteVectorEntry for chat context

The weighted average vector can then be used to query ChromaDB directly:
  "find items closest to this taste vector"
"""

import asyncio
import json
import logging
import math
from collections import Counter
from datetime import datetime, timedelta
from typing import Optional

import httpx

from src.config import settings
from src.database.connection import get_db_session
from src.database.models import WatchHistoryEntry, TasteVectorEntry, User
from src.cache.metadata_cache import MetadataCache

logger = logging.getLogger(__name__)

from src.services.task_monitor import task_monitor


# ── RECENCY WEIGHTING ─────────────────────────────────────────────────────────

def recency_weight(viewed_at: datetime, now: datetime, half_life_days: int = 60) -> float:
    """
    Exponential decay: items watched today = weight 1.0
    Items watched half_life_days ago = weight 0.5
    This means recent taste matters more than old taste.
    """
    if not viewed_at:
        return 0.1
    age_days = max(0, (now - viewed_at).days)
    return math.exp(-age_days * math.log(2) / half_life_days)


# How hard a dropped item pushes the taste vector AWAY from its profile.
# The old code gave drops a small POSITIVE weight (0.05-0.39), so abandoned
# content still pulled the vector toward itself — the docstring's promised
# negative signal was never implemented.
DROP_PUSH_WEIGHT = 0.35


def _is_drop(entry: dict) -> bool:
    """True when the user demonstrably abandoned this item early (<40%
    watched, not completed). Music rows are always completed → never drops."""
    if entry.get("completed"):
        return False
    d, v = entry.get("duration_ms"), entry.get("view_offset_ms")
    if not d or v is None:
        return False
    return (v / d) < 0.4


def completion_weight(entry: WatchHistoryEntry) -> float:
    """
    Weight based on actual completion percentage.
    - Completed (100%): weight 1.0 — strong positive signal
    - 80-99%: weight 0.85 — very likely enjoyed, probably interrupted
    - 40-79%: weight 0.5 — ambiguous, might have dropped it
    - 1-39%: weight 0.1 — likely didn't enjoy it (negative signal territory)
    Returns negative-ish weight for very low completion to pull the taste
    vector away from this type of content.
    """
    if entry.get('duration_ms') and entry.get('duration_ms') > 0 and entry.get('view_offset_ms'):
        rate = min(1.0, entry.get('view_offset_ms') / entry.get('duration_ms'))
    elif entry.get('completed'):
        rate = 1.0
    else:
        rate = 0.3  # unknown completion, lean slightly negative

    if rate >= 0.9:
        return 1.0
    elif rate >= 0.8:
        return 0.85
    elif rate >= 0.4:
        return 0.5
    else:
        # Low completion — use as negative signal by returning small weight
        # The taste engine will interpret this differently
        return max(0.05, rate)


# ── EMBEDDING ─────────────────────────────────────────────────────────────────

async def embed_text(text: str) -> Optional[list]:
    """Generate embedding via Ollama nomic-embed-text."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{settings.effective_ollama}/api/embeddings",
                json={
                    "model": settings.EMBEDDING_MODEL,
                    "prompt": text,
                    "keep_alive": "2m",  # release RAM after 2min, not default 5min
                    # CPU-only: a GPU-resident nomic pushes the packed 27B into
                    # partial offload (0.5 t/s) — see episodic_memory._embed
                    "options": {"num_gpu": 0},
                },
            )
        if r.status_code == 200:
            return r.json().get("embedding")
    except Exception as e:
        logger.debug("Embedding failed: %s", e)
    return None


def weighted_average_embedding(embeddings_weights: list) -> Optional[list]:
    """
    Compute weighted average of embeddings.
    embeddings_weights: [(embedding_list, weight), ...]

    Weights may be NEGATIVE (dropped items push the vector away from their
    profile). The denominator uses |w| so negatives act as repulsion instead
    of shrinking/flipping the normalisation.
    """
    if not embeddings_weights:
        return None

    # Filter out None embeddings
    valid = [(emb, w) for emb, w in embeddings_weights if emb]
    if not valid:
        return None

    dim = len(valid[0][0])
    total_weight = sum(abs(w) for _, w in valid)
    if total_weight == 0:
        return None

    avg = [0.0] * dim
    for emb, w in valid:
        factor = w / total_weight
        for i in range(dim):
            avg[i] += emb[i] * factor

    # L2 normalize
    norm = math.sqrt(sum(x * x for x in avg))
    if norm > 0:
        avg = [x / norm for x in avg]

    return avg


# ── BUILD TEXT REPRESENTATION ─────────────────────────────────────────────────

def build_item_text_dict(entry: dict, enriched: Optional[dict]) -> str:
    """Dict version of build_item_text for use outside SQLAlchemy sessions."""
    media_type = entry.get("media_type", "")
    title = entry.get("title", "")
    series_title = entry.get("series_title")
    genres = entry.get("genres", "")

    parts = []
    if media_type == "music":
        title_part = f"{series_title} - {title}" if series_title else title
    elif series_title:
        title_part = f"{series_title}: {title}"
    else:
        title_part = title
    parts.append(title_part)

    if enriched:
        eg = enriched.get("genres", [])
        et = enriched.get("themes", [])
        em = enriched.get("mood", [])
        ek = enriched.get("tags", enriched.get("keywords", []))
        es = enriched.get("similar_to", enriched.get("similar_artists", []))
        eo = enriched.get("plot_summary") or enriched.get("overview", "")
        eb = enriched.get("bio", "")
        if eg: parts.append(f"Genres: {', '.join(eg[:6])}")
        if et: parts.append(f"Themes: {', '.join(et[:6])}")
        if em: parts.append(f"Mood: {', '.join(em[:4])}")
        if ek: parts.append(f"Tags: {', '.join(ek[:8])}")
        if es: parts.append(f"Similar to: {', '.join(es[:4])}")
        # Prefer dense LLM summary over raw TMDB overview slice
        llm_summary = enriched.get("embedding_text") or enriched.get("summary") or ""
        if llm_summary:
            parts.append(llm_summary[:300])
        elif eo:
            parts.append(eo[:200])
        if eb: parts.append(eb[:150])
    elif genres:
        parts.append(f"Genres: {genres}")

    return " | ".join(p for p in parts if p)


def build_item_text(entry: WatchHistoryEntry, enriched: Optional[dict]) -> str:
    """
    Build a rich text representation of a watch history item for embedding.
    Uses enriched metadata if available, falls back to Plex data.
    """
    parts = []

    # Title
    if entry.get('media_type') == "music":
        title_part = entry.get('title')
        if entry.get('series_title'):
            title_part = f"{entry.get('series_title')} - {entry.get('title')}"
    elif entry.get('series_title'):
        title_part = f"{entry.get('series_title')}: {entry.get('title')}"
    else:
        title_part = entry.get('title')
    parts.append(title_part)

    if enriched:
        # Rich metadata available
        genres = enriched.get("genres", [])
        themes = enriched.get("themes", [])
        mood = enriched.get("mood", [])
        tags = enriched.get("tags", enriched.get("keywords", []))
        similar = enriched.get("similar_to", enriched.get("similar_artists", []))
        overview = enriched.get("plot_summary") or enriched.get("overview", "")
        bio = enriched.get("bio", "")

        if genres:
            parts.append(f"Genres: {', '.join(genres[:6])}")
        if themes:
            parts.append(f"Themes: {', '.join(themes[:6])}")
        if mood:
            parts.append(f"Mood: {', '.join(mood[:4])}")
        if tags:
            parts.append(f"Tags: {', '.join(tags[:8])}")
        if similar:
            parts.append(f"Similar to: {', '.join(similar[:4])}")
        # Prefer dense LLM summary over raw TMDB overview slice
        llm_summary = enriched.get("embedding_text") or enriched.get("summary") or ""
        if llm_summary:
            parts.append(llm_summary[:300])
        elif overview:
            parts.append(overview[:200])
        if bio:
            parts.append(bio[:150])
    else:
        # Fallback to Plex genres
        if entry.get('genres'):
            parts.append(f"Genres: {entry.get('genres')}")

    return " | ".join(p for p in parts if p)


# ── MAIN COMPUTATION ──────────────────────────────────────────────────────────

async def compute_taste_vector_for_user(
    user_id: int,
    category: str = None,
    max_items: int = 0,  # 0 = no cap (safe since only enriched items get embedded)
) -> dict:
    """
    Compute taste vector for a user, optionally for a specific category.
    Returns dict with embedding, stats, and summary text.

    category: None = all, or "music"/"movie"/"show"/"anime"
    max_items: cap for embedding computation (most recent, highest weight)
    """
    now = datetime.utcnow()
    cache = MetadataCache()

    # Load all needed data within the session, convert to plain dicts
    # to avoid DetachedInstanceError when used outside the session
    with get_db_session() as db:
        q = db.query(WatchHistoryEntry).filter(WatchHistoryEntry.user_id == user_id)
        if category:
            q = q.filter(WatchHistoryEntry.media_type == category)
        raw_entries = q.order_by(WatchHistoryEntry.viewed_at.desc()).all()
        # Convert to plain dicts immediately while session is open
        entries = [
            {
                "plex_item_id": e.plex_item_id,
                "title": e.title,
                "series_title": e.series_title,
                "media_type": e.media_type,
                "genres": e.genres,
                "duration_ms": e.duration_ms,
                "view_offset_ms": e.view_offset_ms,
                "completed": e.completed,
                "viewed_at": e.viewed_at,
            }
            for e in raw_entries
        ]

    logger.info("Computing taste vector: user=%d category=%s entries=%d",
                user_id, category or "all", len(entries))
    _tv_task = task_monitor.create(
        name=f"Taste vector: {category or 'all'}",
        category="taste",
        total=len(entries),
        task_id=f"taste-{category or 'all'}-{int(__import__('time').time())}",
    )
    task_monitor.start(_tv_task)

    if not entries:
        cache.close()
        task_monitor.skip(_tv_task, "No entries for this category")
        return {}

    # ── STATS (use ALL entries) ───────────────────────────────────────────────
    genre_counter: Counter = Counter()
    theme_counter: Counter = Counter()
    mood_counter: Counter = Counter()
    title_counter: Counter = Counter()
    completions = []
    binge_series: dict = {}
    dropped_genres: Counter = Counter()   # genres of items abandoned early
    plex_genre_counter: Counter = Counter()  # SAME source as dropped_genres —
    # the denominator for drop RATES (raw drop counts just mirror the top
    # genres: you drop most of what you start most)
    dropped_titles: list = []  # series → list of viewed_at

    for idx, e in enumerate(entries):
        # Yield to event loop every 500 items so SSE updates can go out
        if idx % 500 == 0:
            await asyncio.sleep(0)
            if idx > 0:
                task_monitor.update(_tv_task, processed=idx, total=len(entries))
        c = completion_weight(e)
        completions.append(c)

        # Genre base rate from the SAME Plex-genre source the drop tracker
        # uses — enables drop RATE (dropped/started) instead of raw counts.
        if e.get('genres'):
            for g in e.get('genres').split(","):
                g = g.strip()
                if g:
                    plex_genre_counter[g] += 1

        # Track dropped items (< 40% completion) as negative signals
        if _is_drop(e):
            if e.get('genres'):
                for g in e.get('genres').split(","):
                    g = g.strip()
                    if g:
                        dropped_genres[g] += 1
            dropped_titles.append(e.get('series_title') or e.get('title'))

        # Title tracking
        label = e.get('series_title') or e.get('title')
        if label:
            title_counter[label] += 1

        # Binge tracking
        if e.get('media_type') in ("show", "anime") and e.get('series_title') and e.get('viewed_at'):
            binge_series.setdefault(e.get('series_title'), []).append(e.get('viewed_at'))

        # Genres/themes from enrichment or Plex
        cache_key = f"enriched:{e.get('media_type') or 'movie'}:{e.get('plex_item_id')}"
        enriched_data = cache.get_cache(cache_key)
        if not enriched_data and e.get('media_type') == 'movie':
            # Log first miss to help debug cache key mismatch
            _debug_logged = getattr(cache, '_debug_miss_logged', False)
            if not _debug_logged:
                logger.debug("Cache miss example: key=%s", cache_key)
                cache._debug_miss_logged = True
        profile = enriched_data["response"] if enriched_data else None

        if profile:
            for g in profile.get("genres", []):
                genre_counter[g] += 1
            for t in profile.get("themes", []):
                theme_counter[t] += 1
            for m in profile.get("mood", []):
                mood_counter[m] += 1
        elif e.get('genres'):
            for g in e.get('genres').split(","):
                g = g.strip()
                if g:
                    genre_counter[g] += 1

    # ── EMBEDDING (capped, most recent + highest weight) ──────────────────────
    # Pass 82d: pre-load Plex user music ratings — only music entries pick
    # this up because Kometa overwrites userRating on movies/shows with
    # aggregated platform ratings (NOT personal opinion). The factor is
    # multiplied into ``total_w`` below so highly-rated tracks pull the
    # taste embedding toward their profile; low-rated tracks contribute
    # less. Rating factor anchored at 3 stars (Plex 6.0) = 1.0:
    #   1★ → 0.33   2★ → 0.67   3★ → 1.00   4★ → 1.33   5★ → 1.67
    # Half-stars interpolate. Unrated entries → 1.0 (no change vs old
    # behaviour). Non-music entries → 1.0 unconditionally.
    user_rating_by_item: dict[str, float] = {}
    try:
        from src.database.models import PlexRating as _PR
        with get_db_session() as _rdb:
            for plex_id, rating in (
                _rdb.query(_PR.plex_item_id, _PR.rating)
                .filter(_PR.user_id == user_id)
                .all()
            ):
                if plex_id and rating is not None:
                    user_rating_by_item[str(plex_id)] = float(rating)
        if user_rating_by_item:
            logger.info(
                "[taste-vector] loaded %d Plex user ratings as weight factors",
                len(user_rating_by_item),
            )
    except Exception as e:
        logger.debug("[taste-vector] user-rating preload failed: %s", e)

    # Select top items by combined weight for embedding
    scored = []
    for e in entries:
        r_weight = recency_weight(e.get('viewed_at'), now)
        c_weight = completion_weight(e)
        replay = min(2.0, 1.0 + 0.1 * (title_counter.get(e.get('series_title') or e.get('title'), 1) - 1))

        # Pass 82d: rating factor — see preload comment above for rationale.
        rating_factor = 1.0
        if e.get('media_type') == 'music' and user_rating_by_item:
            ur = user_rating_by_item.get(str(e.get('plex_item_id') or ''))
            if ur is not None:
                rating_factor = ur / 6.0

        if _is_drop(e):
            # The design's promised negative signal, finally real: a drop
            # contributes a NEGATIVE weight, pushing the taste vector away
            # from the abandoned item's profile (see weighted_average_embedding).
            total_w = -DROP_PUSH_WEIGHT * r_weight
        else:
            total_w = r_weight * c_weight * replay * rating_factor
        scored.append((total_w, e))

    # Sort by |weight| — a plain descending sort would push every negative
    # (drop) entry to the tail, where the EMBED_CAP slice silently discards
    # exactly the repulsion signals we just added.
    scored.sort(key=lambda x: abs(x[0]), reverse=True)
    # max_items=0 means no cap — use all entries
    # For embedding we still cap at 5000 to avoid memory issues with huge libraries
    EMBED_CAP = 5000
    top_entries = scored[:EMBED_CAP] if len(scored) > EMBED_CAP else scored

    # Load enrichment timestamps to know which items have new data
    with get_db_session() as db:
        from src.database.models import EnrichmentStatus
        enrichment_ts = {}
        enrichment_ts_by_title = {}
        music_key_by_artist = {}   # artist title → enriched-doc cache key
        for e in db.query(EnrichmentStatus).filter(
            EnrichmentStatus.enriched == True,
        ).all():
            ts_val = e.enriched_at.isoformat() if e.enriched_at else "enriched"
            enrichment_ts[e.plex_rating_key] = ts_val
            enrichment_ts_by_title[e.title] = ts_val
            if e.media_category == "music" and e.plex_rating_key:
                music_key_by_artist[e.title] = e.plex_rating_key

    # ── MUSIC: aggregate to ARTIST level before embedding ────────────────────
    # Row-level selection made the music vector a "last few weeks" vector:
    # 371k play rows, top-5000 by weight (= the most recent), of which only
    # the warmed top tracks had enrichment docs — 0.55% of plays defined the
    # whole taste. One embedding per ARTIST (the artist doc carries bio,
    # genres, similar-to — a better taste representant than track titles),
    # weighted by recency × log-plays × best user rating, keeps every listened
    # artist in the average and makes the vector stable against binges.
    if category == "music":
        by_artist: dict = {}
        for e in entries:
            a = e.get("series_title") or e.get("title")
            if not a:
                continue
            d = by_artist.setdefault(a, {"plays": 0, "last": None, "ur": None})
            d["plays"] += 1
            va = e.get("viewed_at")
            if va and (d["last"] is None or va > d["last"]):
                d["last"] = va
            if user_rating_by_item:
                ur = user_rating_by_item.get(str(e.get("plex_item_id") or ""))
                if ur is not None and (d["ur"] is None or ur > d["ur"]):
                    d["ur"] = ur
        artist_entries = []
        for a, d in by_artist.items():
            key = music_key_by_artist.get(a)
            if not key:
                continue   # not enriched — genre counters already cover it
            w = (recency_weight(d["last"], now)
                 * (1.0 + math.log10(max(d["plays"], 1)))
                 * ((d["ur"] / 6.0) if d["ur"] is not None else 1.0))
            artist_entries.append((w, {
                "plex_item_id": key, "media_type": "music",
                "title": a, "series_title": None,
            }))
        artist_entries.sort(key=lambda x: abs(x[0]), reverse=True)
        logger.info("Music: %d listened artists → %d with enrichment docs "
                    "(was %d play rows)", len(by_artist), len(artist_entries),
                    len(entries))
        top_entries = artist_entries[:EMBED_CAP]

    # Count how many need fresh embeddings
    # Only items WITH enrichment data get embedded — unenriched items use genre counters
    def get_emb_ts(entry):
        pid = entry.get("plex_item_id")
        if pid in enrichment_ts:
            return enrichment_ts[pid]
        mt = entry.get("media_type", "")
        t = (entry.get("series_title") if mt in ("show", "anime", "music") else None) or entry.get("title")
        return enrichment_ts_by_title.get(t)

    enriched_entries = [(w, e) for w, e in top_entries
                        if cache.get_cache(f"enriched:{e.get('media_type') or 'movie'}:{e.get('plex_item_id')}")]

    # We only count needs_embed uniquely per plex_item_id
    needs_embed = 0
    seen_pids = set()
    for _, e in enriched_entries:
        pid = e.get("plex_item_id")
        if pid not in seen_pids:
            seen_pids.add(pid)
            if cache.get_cache(f"emb:{pid}") is None or get_emb_ts(e) != (cache.get_cache(f"emb_ts:{pid}") or {}).get("response"):
                needs_embed += 1

    logger.info("Items: %d total, %d enriched, %d need fresh embedding, %d use cache",
                len(top_entries), len(seen_pids),
                needs_embed, len(seen_pids) - needs_embed)

    # Transition to embedding phase — reset task counters so UI shows embedding progress
    embed_total = len(enriched_entries)
    task_monitor.update(
        _tv_task,
        processed=0,
        total=max(embed_total, 1),
        message=f"Stats done — embedding {embed_total} enriched items ({needs_embed} need fresh vectors)",
    )
    _tv_task.name = f"Taste vector: {category or 'all'} — embedding"
    task_monitor._broadcast()

    embeddings_weights = []
    sem = asyncio.Semaphore(10)
    _embed_done = 0

    async def embed_entry(weight: float, entry: dict):
        if _tv_task.status.value == "skipped":
            return None, weight

        pid = entry.get("plex_item_id", "")
        enriched_data = cache.get_cache(f"enriched:{entry.get('media_type') or 'movie'}:{pid}")
        profile = enriched_data["response"] if enriched_data else None

        # Skip embedding entirely if no enrichment data — genre counters handle these
        if not profile:
            return None, weight

        emb_ts = get_emb_ts(entry)
        cached_emb = cache.get_cache(f"emb:{pid}")
        cached_ts = (cache.get_cache(f"emb_ts:{pid}") or {}).get("response")

        # Use cached embedding if enrichment hasn't changed since last embed
        if cached_emb and cached_ts == emb_ts:
            return cached_emb["response"], weight

        text = build_item_text_dict(entry, profile)
        if not text:
            return None, weight

        async with sem:
            if _tv_task.status.value == "skipped":
                return None, weight
            emb = await embed_text(text)

        if emb:
            cache.set_cache(f"emb:{pid}", emb, days=90)
            cache.set_cache(f"emb_ts:{pid}", emb_ts if emb_ts else "none", days=90)

        return emb, weight

    tasks = [embed_entry(w, e) for w, e in enriched_entries]

    # Process in chunks so we can check for cancellation and send progress updates
    CHUNK = 10
    for i in range(0, len(tasks), CHUNK):
        if _tv_task.status.value == "skipped":
            logger.info("Taste vector computation cancelled at %d/%d", i, len(tasks))
            cache.close()
            task_monitor.skip(_tv_task, "Cancelled by user")
            return {}

        chunk_results = await asyncio.gather(*tasks[i:i+CHUNK], return_exceptions=True)
        chunk_hits = 0
        for result in chunk_results:
            if isinstance(result, Exception):
                continue
            emb, w = result
            if emb:
                embeddings_weights.append((emb, w))
                chunk_hits += 1

        _embed_done = min(i + CHUNK, embed_total)
        task_monitor.update(
            _tv_task,
            processed=_embed_done,
            total=embed_total,
            message=f"Chunk {i // CHUNK + 1}: +{chunk_hits} vectors",
        )

    # Collect canonical titles that had enrichment data — needed for vector_ready tracking.
    # Must happen before cache.close(). EnrichmentStatus has one row per title, so
    # matching by plex_rating_key (episode/track level) only hits the one canonical key;
    # matching by title covers the whole series/artist correctly.
    enriched_titles_for_vr: set = set()
    for _, e in top_entries:
        enriched_data = cache.get_cache(
            f"enriched:{e.get('media_type') or 'movie'}:{e.get('plex_item_id')}"
        )
        if enriched_data:
            mt = e.get("media_type", "")
            t = (e.get("series_title") if mt in ("show", "anime", "music") else None) or e.get("title")
            if t:
                enriched_titles_for_vr.add(t)

    cache.close()

    logger.info("Got %d/%d embeddings successfully", len(embeddings_weights), len(top_entries))

    # Mark enriched titles as vector_ready — match by (title, media_category) so the
    # single EnrichmentStatus row per series/artist is found regardless of which
    # episode's plex_rating_key was stored there.
    if embeddings_weights and enriched_titles_for_vr:
        try:
            with get_db_session() as db:
                from src.database.models import EnrichmentStatus
                db.query(EnrichmentStatus).filter(
                    EnrichmentStatus.title.in_(enriched_titles_for_vr),
                    EnrichmentStatus.media_category == category,
                    EnrichmentStatus.enriched == True,
                ).update({"vector_ready": True}, synchronize_session=False)
                db.commit()
                logger.debug("Marked %d titles as vector_ready for %s",
                             len(enriched_titles_for_vr), category)
        except Exception as e:
            logger.debug("vector_ready update failed: %s", e)
    task_monitor.update(_tv_task, processed=embed_total,
        message=f"Embeddings complete: {len(embeddings_weights)}/{embed_total} vectors")

    # ── WEIGHTED AVERAGE ──────────────────────────────────────────────────────
    taste_embedding = weighted_average_embedding(embeddings_weights)

    # ── NORMALIZE STATS ───────────────────────────────────────────────────────
    def normalize(counter: Counter, top_n: int) -> dict:
        if not counter:
            return {}
        top = counter.most_common(top_n)
        max_v = top[0][1]
        return {k: round(v / max_v, 3) for k, v in top}

    avg_completion = round(sum(completions) / len(completions), 3) if completions else 0.0

    # Binge detection
    binges = []
    for series, times in binge_series.items():
        times.sort()
        i = 0
        while i < len(times):
            window = [times[i]]
            j = i + 1
            while j < len(times):
                if (times[j] - times[i]).total_seconds() / 3600 <= settings.BINGE_SESSION_HOURS:
                    window.append(times[j])
                    j += 1
                else:
                    break
            if len(window) >= settings.BINGE_EPISODE_THRESHOLD:
                binges.append({
                    "series": series,
                    "episodes": len(window),
                    "hours": round((window[-1] - window[0]).total_seconds() / 3600, 1),
                    "date": window[0].isoformat(),
                })
                i = j
            else:
                i += 1

    # Genre aversion as drop RATE (dropped/started), not raw counts — raw
    # counts just mirrored the affinity list ("you abandon the most of what
    # you start the most": Drama was top-affinity AND top-aversion). Noise
    # guards: >=2 drops and >=4 starts per genre, rate >= 0.2.
    genre_aversion = {}
    for g, n_drop in dropped_genres.items():
        base = plex_genre_counter.get(g, 0)
        if n_drop >= 2 and base >= 4:
            rate = n_drop / base
            if rate >= 0.2:
                genre_aversion[g] = round(rate, 3)
    genre_aversion = dict(sorted(genre_aversion.items(), key=lambda x: -x[1])[:10])

    task_monitor.done(_tv_task, f"Complete: {len(embeddings_weights)} embeddings, {len(entries)} entries")
    from src.services.taste_vectors import compute_temporal_patterns
    return {
        "embedding": taste_embedding,
        "embedding_items": len(embeddings_weights),
        "total_entries": len(entries),
        "genre_affinity": normalize(genre_counter, 20),
        "theme_affinity": normalize(theme_counter, 15),
        "mood_affinity": normalize(mood_counter, 10),
        "genre_aversion": genre_aversion,   # drop-rate per genre
        "dropped_titles": dropped_titles[:20],
        "top_titles": [k for k, _ in title_counter.most_common(30)],
        "watch_count": len(entries),
        "avg_completion": avg_completion,
        "binges": binges[-10:],
        # time-of-day / day-of-week distributions — read by the rec prompts
        # as a one-line "viewing rhythm" ingredient
        "temporal": compute_temporal_patterns(entries),
    }


async def compute_all_taste_vectors(user_id: int, categories: list = None):
    """
    Compute taste vectors for the given categories (default: all).
    Stores in TasteVectorEntry (plain text for chat context)
    and EncryptedTasteVector (AES-256 encrypted embeddings).
    """
    if not categories:
        categories = ["music", "movie", "show", "anime"]
    all_results = {}

    for cat in categories:
        logger.info("Computing taste vector for %s...", cat)
        result = await compute_taste_vector_for_user(user_id, category=cat)
        if result.get("watch_count", 0) >= 5:
            all_results[cat] = result

    if not all_results:
        logger.warning("No taste data for user %d", user_id)
        return

    # Generate LLM summaries per category
    from src.services.plex_sync import _generate_taste_summary, TYPE_LABELS
    summary_parts = []
    for cat, res in all_results.items():
        summary = await _generate_taste_summary(
            user_id=user_id,
            media_type=cat,
            genre_affinity=res["genre_affinity"],
            themes=list(res["theme_affinity"].keys())[:8],
            moods=list(res["mood_affinity"].keys())[:5],
            top_titles=res["top_titles"][:15],
            watch_count=res["watch_count"],
            avg_completion=res["avg_completion"],
            genre_aversion=res.get("genre_aversion", {}),
            dropped_titles=res.get("dropped_titles", [])[:5],
            binges=res.get("binges", [])[-3:],
        )
        summary_parts.append(f"[{cat.upper()}] {summary}")

    # Store plain stats in TasteVectorEntry
    type_summaries = {}
    top_titles_by_type = {}
    themes_by_type = {}
    moods_by_type = {}
    for cat, res in all_results.items():
        type_summaries[cat] = {
            "genre_affinity": res["genre_affinity"],
            "themes": res["theme_affinity"],
            "moods": res["mood_affinity"],
            "top_titles": res["top_titles"][:20],
            "watch_count": res["watch_count"],
            "avg_completion": res["avg_completion"],
            "embedding_items": res.get("embedding_items", 0),
            "temporal": res.get("temporal", {}),
        }
        top_titles_by_type[cat] = res["top_titles"][:20]
        themes_by_type[cat] = list(res["theme_affinity"].keys())[:10]
        moods_by_type[cat] = list(res["mood_affinity"].keys())[:6]

    with get_db_session() as db:
        existing = db.query(TasteVectorEntry).filter(
            TasteVectorEntry.user_id == user_id
        ).first()

        # MERGE with the stored per-category data instead of replacing it —
        # a partial recompute (categories=["anime"]) used to wipe the other
        # categories' summaries, titles and stats from TasteVectorEntry.
        summary_by_cat = {}
        if existing:
            def _merged(old_json: str, new: dict) -> dict:
                try:
                    old = json.loads(old_json or "{}")
                except Exception:
                    old = {}
                if not isinstance(old, dict):
                    old = {}
                old.update(new)
                return old
            type_summaries = _merged(existing.genre_affinity, type_summaries)
            top_titles_by_type = _merged(existing.top_titles, top_titles_by_type)
            themes_by_type = _merged(existing.actor_affinity, themes_by_type)
            moods_by_type = _merged(existing.director_affinity, moods_by_type)
            for p in (existing.summary_text or "").split("\n\n"):
                if p.startswith("[") and "]" in p:
                    summary_by_cat[p[1:p.index("]")].lower()] = p
        for p in summary_parts:
            summary_by_cat[p[1:p.index("]")].lower()] = p
        summary_text = "\n\n".join(
            summary_by_cat[c] for c in ("music", "movie", "show", "anime", "other")
            if c in summary_by_cat)

        # totals over the MERGED per-category stats, not just this run's slice
        total_count = sum((ts.get("watch_count") or 0)
                          for ts in type_summaries.values() if isinstance(ts, dict))
        overall_completion = round(
            sum((ts.get("avg_completion") or 0) * (ts.get("watch_count") or 0)
                for ts in type_summaries.values() if isinstance(ts, dict))
            / max(total_count, 1), 3)

        data = dict(
            genre_affinity=json.dumps(type_summaries),
            actor_affinity=json.dumps(themes_by_type),
            director_affinity=json.dumps(moods_by_type),
            top_titles=json.dumps(top_titles_by_type),
            watch_count=total_count,
            avg_completion=overall_completion,
            computed_at=datetime.utcnow(),
            summary_text=summary_text,
        )
        if existing:
            for k, v in data.items():
                setattr(existing, k, v)
        else:
            db.add(TasteVectorEntry(user_id=user_id, **data))
        db.commit()

    # Store encrypted embeddings in EncryptedTasteVector
    # (requires user PIN — stored as unencrypted for now, PIN flow in v1.1)
    try:
        from src.database.models import EncryptedTasteVector
        with get_db_session() as db:
            for cat, res in all_results.items():
                if not res.get("embedding"):
                    continue
                existing_etv = db.query(EncryptedTasteVector).filter(
                    EncryptedTasteVector.user_id == user_id,
                    EncryptedTasteVector.media_category == cat,
                ).first()

                # --- MEMORY CARRY-OVER ---
                # Chat-sourced fields (feedback, dislikes, theme aversion)
                # carry over across recomputes. genre_aversion does NOT any
                # more: it is the freshly computed drop-rate — the old max()
                # merge kept stale chat strings alive in it forever.
                # theme_aversion now DECAYS on carry-over (60d half-life,
                # same as the watch signal): before this, a casual complaint
                # was the only immortal signal in the system while actual
                # viewing behaviour faded — the reliability order was
                # upside down. mood_aversion (never written, never read)
                # is dropped from the blob entirely.
                old_feedback = {}
                if existing_etv and existing_etv.encrypted_blob:
                    try:
                        old_data = json.loads(existing_etv.encrypted_blob)
                        if old_data.get("version", 0) == 0:  # Phase A: unencrypted
                            old_feedback["explicit_feedback"] = old_data.get("explicit_feedback", [])
                            old_feedback["disliked_titles"] = old_data.get("disliked_titles", [])
                            aversion = old_data.get("theme_aversion", {}) or {}
                            if aversion and existing_etv.computed_at:
                                days = max(0.0, (datetime.utcnow()
                                                 - existing_etv.computed_at
                                                 ).total_seconds() / 86400)
                                factor = 0.5 ** (days / 60.0)
                                aversion = {k: round(v * factor, 4)
                                            for k, v in aversion.items()
                                            if v * factor >= 0.05}
                            old_feedback["theme_aversion"] = dict(
                                sorted(aversion.items(), key=lambda x: -x[1])[:20])
                    except Exception as e:
                        logger.warning("Could not read old taste vector for carry-over: %s", e)

                # Store vector stats (embedding stored separately when PIN available)
                blob = json.dumps({
                    "embedding": res["embedding"] if res.get("embedding") else None,
                    "embedding_items": res.get("embedding_items", 0),
                    "binges": res.get("binges", []),
                    "genre_aversion": res.get("genre_aversion", {}),
                    "explicit_feedback": old_feedback.get("explicit_feedback", []),
                    "disliked_titles": old_feedback.get("disliked_titles", []),
                    "theme_aversion": old_feedback.get("theme_aversion", {}),
                    "version": 0,  # 0 = unencrypted, 1 = AES-256
                })
                # --- ENDE NEU ---

                if existing_etv:
                    existing_etv.encrypted_blob = blob
                    existing_etv.computed_at = datetime.utcnow()
                    existing_etv.watch_count = res["watch_count"]
                    existing_etv.summary_text = next(
                        (p for p in summary_parts if f"[{cat.upper()}]" in p), ""
                    )
                else:
                    db.add(EncryptedTasteVector(
                        user_id=user_id,
                        media_category=cat,
                        salt="unencrypted",
                        encrypted_blob=blob,
                        computed_at=datetime.utcnow(),
                        watch_count=res["watch_count"],
                        summary_text=next(
                            (p for p in summary_parts if f"[{cat.upper()}]" in p), ""
                        ),
                    ))
            db.commit()
        logger.info("Taste vectors computed and stored for user %d", user_id)
        logger.info("Summary preview: %s", summary_text[:200] if summary_text else "(empty)")
    except Exception as e:
        logger.warning("Could not store EncryptedTasteVector: %s", e)