"""
Curatarr - Taste Engine

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
    watched, not completed).

    Never fires for imported music: those rows carry ms_played but no
    duration_ms, so there is nothing to take a percentage of. (The older
    claim here — that music rows are always completed — is not true: a
    tenth of them are tracks that were skipped. They are down-weighted by
    completion_weight rather than pushed away as drops.)
    """
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
    """Embed one taste-input text via the central embed service.

    DOCUMENT side: the taste centroid is averaged from item texts, so it
    must live in the same space as the indexed item documents (the Nomic
    prefix schema's asymmetry belongs to QUERIES only)."""
    from src.services.embed_service import embed_documents
    vecs = await embed_documents([text])
    return vecs[0] if vecs else None


def _unit(vec) -> Optional[list]:
    """L2-normalize. Every vector entering the centroid average MUST be
    unit-length: the legacy corpus stored RAW vectors (norm ~13) — mixing
    one of those with unit vectors would weight it 13x in the mean."""
    if vec is None or len(vec) == 0:
        return None
    norm = math.sqrt(sum(float(x) * float(x) for x in vec))
    return [float(x) / norm for x in vec] if norm > 0 else None


def _chroma_item_vector(entry: dict, profile: Optional[dict]) -> Optional[list]:
    """The item's STORED index vector, if it is in ChromaDB (eval 1.3).

    The index and the taste side used to embed two DIFFERENT texts for the
    same item (prose embedding_text vs the pipe template) — every cosine in
    the system compared mismatched representations. Preferring the stored
    vector unifies the representation for free and makes the post-migration
    taste rebuild a lookup instead of a re-embed. Same id cascade as the
    index writer: plex key → tmdb → anilist → title."""
    try:
        from src.vector_store.chromadb_wrapper import get_chroma_db
        chroma = get_chroma_db()
        candidates = [entry.get("plex_item_id")]
        if profile:
            candidates += [profile.get("tmdb_id"), profile.get("anilist_id")]
        candidates += [entry.get("series_title"), entry.get("title")]
        for cid in candidates:
            if not cid:
                continue
            res = chroma.get_by_id(str(cid))
            emb = res.get("embedding") if res else None
            if emb is not None and len(emb):
                return _unit(list(emb))
    except Exception as e:
        logger.debug("chroma item lookup failed: %s", e)
    return None


def weighted_mean_embedding(embeddings_weights: list) -> tuple:
    """Weighted mean of unit embeddings → (unit centroid | None, pre_norm).

    Non-negative weights only: the old in-centroid drop subtraction was
    mathematically void (eval 1.8 — the |w| denominator cancelled under L2
    normalisation, so repulsion strength was just the neg/pos mass ratio,
    near zero for typical users). Drops now form their OWN centroid and
    act as a scoring-side penalty instead.

    pre_norm is the length of the mean BEFORE normalisation — the free
    multimodality diagnostic from the eval (PinnerSage rationale): a low
    pre-norm means the items point in conflicting directions and the
    single centroid represents this (user, category) poorly. Logged and
    stored; multi-centroid clustering only gets built if the numbers say
    so ("erst messen, dann bauen").
    """
    valid = [(emb, w) for emb, w in embeddings_weights if emb and w > 0]
    if not valid:
        return None, 0.0

    dim = len(valid[0][0])
    total_weight = sum(w for _, w in valid)
    if total_weight <= 0:
        return None, 0.0

    avg = [0.0] * dim
    for emb, w in valid:
        factor = w / total_weight
        for i in range(dim):
            avg[i] += emb[i] * factor

    pre_norm = math.sqrt(sum(x * x for x in avg))
    if pre_norm <= 0:
        return None, 0.0
    return [x / pre_norm for x in avg], pre_norm


def _domain_corpus_mean(category: str):
    """Mean of the category's normalized item cloud — the anisotropy
    reference for the cluster-separation gate below. None on a tiny or
    unavailable corpus (callers then fall back to the raw-cosine gate)."""
    if not category:
        return None
    try:
        import numpy as np
        from src.vector_store.chromadb_wrapper import get_chroma_db
        embs = get_chroma_db().embeddings_for_domain(category)
        if len(embs) < 50:
            return None
        m = np.asarray(embs, dtype=float)
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (m / norms).mean(axis=0)
    except Exception as e:
        logger.debug("corpus mean for %s failed: %s", category, e)
        return None


def _cluster_centroids(pos_titled: list, max_k: int = 5,
                       corpus_mean=None) -> Optional[list]:
    """Spherical k-means over the positive watch embeddings — the
    multi-centroid upgrade for MULTIMODAL (user, category) pairs only
    (eval 2.2 / PinnerSage: a single mean of conflicting interests lands
    in a region matching none of them).

    Deterministic on purpose (no random init): farthest-point seeding
    from the weight-strongest item, k ≤ 5 via a log2 ramp (MIND-style).
    Returns [{embedding, weight, share, top_titles}] sorted by weight,
    clusters under 8% mass dropped — or None when clustering degenerates.

    ``corpus_mean`` (external eval catch): the near-parallel gate used to
    compare RAW centroid cosines — but text embeddings share a common
    cone, so genuinely different clusters still read ~0.94 raw (music:
    0.942 raw vs 0.015 centered — five real listening clusters, among
    them the owner's Kabarett lane, were rejected as 'one cloud' and the
    pair fell back to a single centroid). Centroids are centered against
    the CORPUS mean for the gate test only (stored raw — readers center
    downstream via the blob's corpus_mean). Deliberately NOT self-centered:
    at k=2, centroids centered by their own mean are always antiparallel
    and the gate would never fire, breaking unimodal detection."""
    try:
        import numpy as np
        n = len(pos_titled)
        k = min(max_k, max(2, int(math.log2(max(4, n)) / 2)))
        m = np.asarray([e for e, _, _ in pos_titled], dtype=float)
        w = np.asarray([wt for _, wt, _ in pos_titled], dtype=float)
        titles = [t for _, _, t in pos_titled]

        # farthest-point init, seeded at the strongest item
        seeds = [int(np.argmax(w))]
        while len(seeds) < k:
            sims = np.max(m @ m[seeds].T, axis=1)
            sims[seeds] = np.inf
            seeds.append(int(np.argmin(sims)))
        cents = m[seeds].copy()

        assign = None
        for _ in range(15):
            new_assign = np.argmax(m @ cents.T, axis=1)
            if assign is not None and np.array_equal(new_assign, assign):
                break
            assign = new_assign
            for c in range(k):
                mask = assign == c
                if not mask.any():
                    continue
                v = (m[mask] * w[mask, None]).sum(axis=0)
                norm = np.linalg.norm(v)
                if norm > 0:
                    cents[c] = v / norm

        total_w = float(w.sum())
        out = []
        for c in range(k):
            mask = assign == c
            cw = float(w[mask].sum())
            if cw < total_w * 0.08 or int(mask.sum()) < 3:
                continue   # noise cluster — not a real interest
            idx = np.argsort(-(w * mask))[:12]
            # dedupe: several rows of one series share a title
            uniq = list(dict.fromkeys(
                titles[int(i)] for i in idx if mask[int(i)] and titles[int(i)]))
            out.append({
                "embedding": [float(x) for x in cents[c]],
                "weight": round(cw, 3),
                "share": round(cw / total_w, 3),
                "top_titles": uniq[:3],
            })
        out.sort(key=lambda c: -c["weight"])
        if len(out) < 2:
            return None
        # k-means happily splits ONE tight cloud into k halves — reject the
        # artificial split when any two centroids are near-parallel (that
        # was a unimodal taste after all; the single centroid serves it).
        # The parallelism test runs CENTERED when a corpus mean is at hand
        # (raw cosines sit in the anisotropy cone and reject real clusters);
        # a centroid sitting exactly at the corpus mean is directionless —
        # fall back to the raw-cone test then.
        cmat = np.asarray([c["embedding"] for c in out])
        if corpus_mean is not None:
            cen = cmat - np.asarray(corpus_mean, dtype=float)[None, :]
            n = np.linalg.norm(cen, axis=1, keepdims=True)
            if float(n.min()) > 1e-6:
                cmat = cen / n
        sims = cmat @ cmat.T
        np.fill_diagonal(sims, 0.0)
        if float(sims.max()) > 0.9:
            return None
        return out
    except Exception as e:
        logger.warning("taste clustering failed: %s", e)
        return None


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


def _corpus_calibration(embedding, drop_embedding, category: str):
    """Eval 1.6 + 2.4: one pass over the category corpus produces
    everything the scoring side needs to compare cosines across runs:

      corpus_mean — the anisotropy fix: text embeddings share a common
        cone; subtracting the corpus mean before comparing widens the
        discriminative range and makes the drop repulsion real.
      cal (p10, p90) — the taste centroid's percentile anchors against
        the CENTERED corpus (global, so stored del_scores stay stationary
        across batches).
      drop_cal — same anchors for the drop centroid (scoring penalty).

    Mean and anchors are written together in one rebuild: a reader that
    finds corpus_mean in the blob knows the anchors are centered ones.
    Returns dict or None (tiny corpus / no embedding)."""
    if not embedding:
        return None
    try:
        import numpy as np
        from src.vector_store.chromadb_wrapper import get_chroma_db
        embs = get_chroma_db().embeddings_for_domain(category)
        if len(embs) < 50:
            return None
        m = np.asarray(embs, dtype=float)
        norms = np.linalg.norm(m, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        m = m / norms
        mean = m.mean(axis=0)

        def _center(vec):
            v = np.asarray(vec, dtype=float)
            n = np.linalg.norm(v)
            if not n:
                return None
            v = v / n - mean
            n = np.linalg.norm(v)
            return v / n if n else None

        mc = m - mean
        mc_norms = np.linalg.norm(mc, axis=1, keepdims=True)
        mc_norms[mc_norms == 0] = 1.0
        mc = mc / mc_norms

        def _anchors(vec_or_list):
            """Anchors for a single centroid OR max-over-clusters (the
            reader scores multimodal pairs as MAX over cluster centroids,
            so the calibration distribution must be built the same way)."""
            if isinstance(vec_or_list, list) and vec_or_list \
                    and isinstance(vec_or_list[0], dict):
                centered = [_center(c["embedding"]) for c in vec_or_list]
                centered = [c for c in centered if c is not None]
                if not centered:
                    return None
                cos = np.max(np.stack([mc @ c for c in centered]), axis=0)
            else:
                c = _center(vec_or_list)
                if c is None:
                    return None
                cos = mc @ c
            return (round(float(np.percentile(cos, 10)), 4),
                    round(float(np.percentile(cos, 90)), 4))

        cal = _anchors(embedding)
        if not cal:
            return None
        return {
            "corpus_mean": [round(float(x), 6) for x in mean],
            "cal": cal,
            "drop_cal": _anchors(drop_embedding) if drop_embedding else None,
        }
    except Exception as e:
        logger.warning("corpus calibration failed for %s: %s", category, e)
        return None


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
        # Column tuples, not full ORM objects: instantiating tens of
        # thousands of EnrichmentStatus rows just to read four fields is
        # ~17x slower and holds every object in memory (Jules measured
        # 2.7s vs 0.16s at 50k rows).
        for plex_rating_key, title, media_category, enriched_at in db.query(
            EnrichmentStatus.plex_rating_key,
            EnrichmentStatus.title,
            EnrichmentStatus.media_category,
            EnrichmentStatus.enriched_at,
        ).filter(EnrichmentStatus.enriched == True).all():
            ts_val = enriched_at.isoformat() if enriched_at else "enriched"
            enrichment_ts[plex_rating_key] = ts_val
            enrichment_ts_by_title[title] = ts_val
            if media_category == "music" and plex_rating_key:
                music_key_by_artist[title] = plex_rating_key

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

    # We only count needs_embed uniquely per plex_item_id. Model-scoped key —
    # the legacy un-scoped `emb:{pid}` rows were purged in the 2026-08-18
    # ballast cleanup (this counter was their last reader, and only for the
    # progress estimate; the real embed path at embed_entry() below prefers
    # the stored chroma vector anyway).
    needs_embed = 0
    seen_pids = set()
    from src.services.embed_service import get_profile as _cnt_profile
    _cnt_model = _cnt_profile().get("model") or "?"
    for _, e in enriched_entries:
        pid = e.get("plex_item_id")
        if pid not in seen_pids:
            seen_pids.add(pid)
            if (cache.get_cache(f"emb:{_cnt_model}:{pid}") is None
                    or get_emb_ts(e) != (cache.get_cache(f"emb_ts:{_cnt_model}:{pid}") or {}).get("response")):
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
    from src.services.embed_service import get_profile as _emb_profile
    _emb_model = _emb_profile().get("model") or "?"

    async def embed_entry(weight: float, entry: dict):
        _t = entry.get("series_title") or entry.get("title") or ""
        if _tv_task.status.value == "skipped":
            return None, weight, _t

        pid = entry.get("plex_item_id", "")
        enriched_data = cache.get_cache(f"enriched:{entry.get('media_type') or 'movie'}:{pid}")
        profile = enriched_data["response"] if enriched_data else None

        # Skip embedding entirely if no enrichment data — genre counters handle these
        if not profile:
            return None, weight, _t

        # Preferred source: the item's STORED index vector (eval 1.3 —
        # one representation for index and taste; also makes the
        # post-migration rebuild a cheap lookup instead of a re-embed).
        doc_vec = _chroma_item_vector(entry, profile)
        if doc_vec is not None:
            return doc_vec, weight, _t

        emb_ts = get_emb_ts(entry)
        # Model-scoped cache key: a model flip must never serve vectors
        # from the previous embedding space.
        cached_emb = cache.get_cache(f"emb:{_emb_model}:{pid}")
        cached_ts = (cache.get_cache(f"emb_ts:{_emb_model}:{pid}") or {}).get("response")

        # Use cached embedding if enrichment hasn't changed since last embed
        if cached_emb and cached_ts == emb_ts:
            return _unit(cached_emb["response"]), weight, _t

        text = build_item_text_dict(entry, profile)
        if not text:
            return None, weight, _t

        async with sem:
            if _tv_task.status.value == "skipped":
                return None, weight, _t
            emb = await embed_text(text)

        if emb:
            cache.set_cache(f"emb:{_emb_model}:{pid}", emb, days=90)
            cache.set_cache(f"emb_ts:{_emb_model}:{pid}", emb_ts if emb_ts else "none", days=90)

        return _unit(emb) if emb else None, weight, _t

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
            emb, w, t = result
            if emb:
                embeddings_weights.append((emb, w, t))
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
    # Two centroids (eval 2.3): positives form the taste centroid, drops
    # form their own — kept OUT of the positive representation and applied
    # as a scoring-side penalty by the readers.
    _pos = [(emb, w) for emb, w, _t in embeddings_weights if w > 0]
    _pos_titled = [(emb, w, _t) for emb, w, _t in embeddings_weights if w > 0]
    _neg = [(emb, -w) for emb, w, _t in embeddings_weights if w < 0]
    taste_embedding, pre_norm = weighted_mean_embedding(_pos)
    drop_embedding, _ = weighted_mean_embedding(_neg)
    multimodal = bool(_pos) and pre_norm < 0.75
    logger.info("[taste] %s/%s: pre-norm %.3f (%d pos / %d drop vectors)%s",
                user_id, category or "all", pre_norm, len(_pos), len(_neg),
                " — MULTIMODAL (single centroid represents this pair poorly)"
                if multimodal else "")

    # Multi-centroid upgrade (eval 2.2, built only where the measurement
    # says so): for multimodal pairs, cluster the watch embeddings and let
    # the readers score as MAX over cluster centroids (ComiRec serving).
    cluster_centroids = (
        _cluster_centroids(_pos_titled,
                           corpus_mean=_domain_corpus_mean(category))
        if multimodal and len(_pos) >= 40 else None)
    if cluster_centroids:
        logger.info("[taste] %s/%s: %d taste clusters: %s",
                    user_id, category or "all", len(cluster_centroids),
                    " | ".join(f"{int(c['share'] * 100)}% {', '.join(c['top_titles'][:2])}"
                               for c in cluster_centroids))

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
        "drop_embedding": drop_embedding,   # eval 2.3: scoring-side repulsion
        "cluster_centroids": cluster_centroids,   # multimodal pairs only
        "pre_norm": round(pre_norm, 4),     # multimodality diagnostic
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


def _update_plain_taste_stats(user_id: int, all_results: dict, summary_parts: list) -> str:
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
    return summary_text


def _store_taste_blobs(user_id: int, all_results: dict, summary_parts: list, summary_text: str):
    # Store plain-text embeddings in EncryptedTasteVector
    # (the model is called EncryptedTasteVector but it currently stores plain JSON)
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
                old_rev = 0
                if existing_etv and existing_etv.encrypted_blob:
                    try:
                        old_data = json.loads(existing_etv.encrypted_blob)
                        if old_data.get("version", 0) == 0:  # Phase A: unencrypted
                            old_rev = int(old_data.get("rev") or 0)
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

                # Corpus calibration: mean-centering + global anchors for
                # BOTH centroids, one corpus pass (eval 1.6 + 2.3 + 2.4).
                # Multimodal pairs calibrate against max-over-clusters —
                # the same statistic the reader scores with.
                calib = _corpus_calibration(
                    res.get("cluster_centroids") or res.get("embedding"),
                    res.get("drop_embedding"), cat)

                # Store vector stats (embedding stored separately when PIN available)
                blob_dict = {
                    "embedding": res["embedding"] if res.get("embedding") else None,
                    "drop_embedding": res.get("drop_embedding"),
                    "cluster_centroids": res.get("cluster_centroids"),
                    "pre_norm": res.get("pre_norm"),
                    "embedding_items": res.get("embedding_items", 0),
                    "binges": res.get("binges", []),
                    "genre_aversion": res.get("genre_aversion", {}),
                    "explicit_feedback": old_feedback.get("explicit_feedback", []),
                    "disliked_titles": old_feedback.get("disliked_titles", []),
                    "theme_aversion": old_feedback.get("theme_aversion", {}),
                    "corpus_mean": (calib or {}).get("corpus_mean"),
                    "cal_p10": (calib or {}).get("cal", (None, None))[0],
                    "cal_p90": (calib or {}).get("cal", (None, None))[1],
                    "drop_cal_p10": ((calib or {}).get("drop_cal") or (None, None))[0],
                    "drop_cal_p90": ((calib or {}).get("drop_cal") or (None, None))[1],
                    "version": 0,  # 0 = unencrypted, 1 = AES-256
                }
                # --- ENDE NEU ---

                if existing_etv:
                    # Optimistic write (eval 1.4): a chat-feedback merge that
                    # landed between our carry-over read and this commit must
                    # not be clobbered — on a rev miss, take the merger's
                    # fresh feedback fields and try once more.
                    from src.services.taste_vectors import cas_update_blob
                    if not cas_update_blob(db, existing_etv.id, old_rev, blob_dict):
                        db.rollback()
                        db.expire_all()
                        fresh = db.query(EncryptedTasteVector).filter(
                            EncryptedTasteVector.id == existing_etv.id).first()
                        try:
                            fd = json.loads(fresh.encrypted_blob) if fresh else {}
                        except Exception:
                            fd = {}
                        for k in ("explicit_feedback", "disliked_titles",
                                  "theme_aversion"):
                            if fd.get(k) is not None:
                                blob_dict[k] = fd[k]
                        new_rev = int(fd.get("rev") or 0)
                        if not cas_update_blob(db, existing_etv.id, new_rev, blob_dict):
                            logger.warning("[taste] %s blob CAS lost twice — "
                                           "forcing recompute result", cat)
                            blob_dict["rev"] = new_rev + 1
                            fresh.encrypted_blob = json.dumps(blob_dict)
                        existing_etv = fresh or existing_etv
                    existing_etv.computed_at = datetime.utcnow()
                    existing_etv.watch_count = res["watch_count"]
                    # skip_summaries rebuilds produce no parts — keep the
                    # existing curator-written summary in that case.
                    existing_etv.summary_text = next(
                        (p for p in summary_parts if f"[{cat.upper()}]" in p),
                        existing_etv.summary_text or "",
                    )
                else:
                    blob_dict["rev"] = 1
                    db.add(EncryptedTasteVector(
                        user_id=user_id,
                        media_category=cat,
                        salt="unencrypted",
                        encrypted_blob=json.dumps(blob_dict),
                        computed_at=datetime.utcnow(),
                        watch_count=res["watch_count"],
                        summary_text=next(
                            (p for p in summary_parts if f"[{cat.upper()}]" in p), ""
                        ),
                    ))
                # Commit PER category: the CAS-miss path rolls back, which
                # must never discard the categories already written in this
                # loop.
                db.commit()
        logger.info("Taste vectors computed and stored for user %d", user_id)
        logger.info("Summary preview: %s", summary_text[:200] if summary_text else "(empty)")
    except Exception as e:
        logger.warning("Could not store EncryptedTasteVector: %s", e)


async def _generate_summaries(user_id: int, all_results: dict, skip_summaries: bool) -> list:
    # Generate LLM summaries per category. skip_summaries=True (the
    # embedding-migration rebuild) recomputes only vectors/stats — the
    # persist block below merges per-category summaries from the EXISTING
    # row, so the curator-written texts survive untouched and no GPU/LLM
    # call happens.
    from src.services.plex_sync import _generate_taste_summary
    summary_parts = []
    for cat, res in ({} if skip_summaries else all_results).items():
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
    return summary_parts


async def compute_all_taste_vectors(user_id: int, categories: list = None,
                                    skip_summaries: bool = False):
    """
    Compute taste vectors for the given categories (default: all).
    Stores in TasteVectorEntry (plain text for chat context)
    and EncryptedTasteVector (JSON plain text embeddings).
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

    summary_parts = await _generate_summaries(user_id, all_results, skip_summaries)
    summary_text = _update_plain_taste_stats(user_id, all_results, summary_parts)
    _store_taste_blobs(user_id, all_results, summary_parts, summary_text)