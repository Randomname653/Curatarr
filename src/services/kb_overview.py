"""
Curatarr — Knowledge-Base Overview: the SINGLE SOURCE OF TRUTH for "how far is
the data collection".

Replaces three display sources that each counted DIFFERENT sets against
DIFFERENT denominators (/enrichment/status watch-history rows, the ARR-status
rows, /library/breakdown pipeline states) — which produced "1.089 of 628
tracked" and "Indexed 792/628 (100%)" style nonsense.

The three rules every number here obeys:
  1. ONE identity per item — an item counts as enriched/vectorized if ANY of
     its historic cache/vector keys matches ("{svc}:{arr_id}", tmdb, tvdb,
     title[:40]; both show+anime for sonarr items — classification drift).
  2. ONE denominator per row — arr DOWNLOADED items. Watch-history-only
     entities (Spotify artists…) are their OWN section, never mixed in.
  3. States are derived from the JOIN of the three truths (status flags,
     live cache entries, vector ids) — "enriched" with a dead cache entry is
     reported as exactly that, not as 100%.

Invariants (enforced by tests/test_kb_overview.py):
  sum(states) == downloaded, every state >= 0, vectors.indexed <= downloaded.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path

from src.paths import DATA_DIR
from src.services.enrichment_state import (STATES, STATE_DEFINITIONS, DONE_STATES,
                                            classify_enrichment_row)

logger = logging.getLogger(__name__)

# 60s payload cache — the endpoint may be polled by the KB view.
_CACHE: dict = {"at": 0.0, "payload": None}
_CACHE_TTL = 60.0

# The state vocabulary lives in services/enrichment_state.py (one classifier
# for the KB tile, /library/breakdown and the producer). Re-exported here for
# the invariant test and older callers.
_STATES = STATES


def invalidate() -> None:
    """Drop the payload cache — every write endpoint (retry / ignore / pin)
    calls this so the tile reflects the action on the next poll."""
    _CACHE["payload"] = None


class _PseudoRow:
    """Row shape for ``classify_enrichment_row`` when only the arr-keyed
    status exists (title-match writes never created an EnrichmentStatus)."""
    __slots__ = ("enriched", "error", "provisional", "next_retry_at", "attempt_count")

    def __init__(self, enriched=False, error=None):
        self.enriched, self.error = bool(enriched), error
        self.provisional, self.next_retry_at, self.attempt_count = False, None, 0


def _dir_size_mb(path: str) -> float:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except Exception:
        return 0.0
    return round(total / (1024 * 1024), 1)


def _file_size_mb(path) -> float:
    try:
        return round(os.path.getsize(str(path)) / (1024 * 1024), 1)
    except OSError:
        return 0.0


def _candidates(mt: str, svc: str, arr_id, tmdb_id, tvdb_id, title: str,
                mbid: str = None) -> list:
    """Every cache/vector key shape this item may have been stored under."""
    out = [f"{svc}:{arr_id}" if arr_id is not None else None,
           str(tmdb_id) if tmdb_id else None,
           str(tvdb_id) if tvdb_id else None,
           (title or "")[:40] or None,
           mbid or None]
    return [k for k in out if k]


def _is_downloaded(svc: str, raw: dict) -> bool:
    if svc == "radarr":
        return bool(raw.get("hasFile"))
    stats = raw.get("statistics") or {}
    if svc == "sonarr":
        return (stats.get("episodeFileCount") or 0) > 0 or (stats.get("sizeOnDisk") or 0) > 0
    return (stats.get("sizeOnDisk") or 0) > 0   # lidarr


async def build_overview() -> dict:
    """Assemble the full consolidated payload. Read-only; ~1-2s cold."""
    now = time.time()
    if _CACHE["payload"] is not None and now - _CACHE["at"] < _CACHE_TTL:
        return _CACHE["payload"]

    from src.config import settings
    from src.cache.metadata_cache import MetadataCache
    from src.database.connection import get_db_session
    from src.database.models import (ArrEnrichmentStatus, EnrichmentStatus,
                                     WatchHistoryEntry)
    from src.routers.library import _fetch_arr_library

    # ── shared truth sets (one query each) ────────────────────────────────────
    mc = MetadataCache()
    try:
        live_enriched = mc.live_key_set("enriched:")
        wiki_counts = {c: mc.count_raw_with_marker(c, '"significance"')
                       for c in ("movie", "show", "anime", "music")}
        omdb_counts = {c: mc.count_raw_with_marker(c, '"omdb_checked"')
                       for c in ("movie", "show", "anime", "music")}
        reception_counts = {c: mc.count_raw_with_marker(c, '"reception"')
                            for c in ("movie", "show", "anime")}
    finally:
        mc.close()

    chroma_ids: set = set()
    try:
        from src.vector_store.chromadb_wrapper import ChromaDBWrapper
        chroma_ids = set(ChromaDBWrapper().all_ids())
    except Exception as e:
        logger.debug("[kb-overview] chroma id fetch failed: %s", e)

    with get_db_session() as db:
        arr_flags = {
            (r.service, r.arr_id): (bool(r.enriched), r.error)
            for r in db.query(ArrEnrichmentStatus.service, ArrEnrichmentStatus.arr_id,
                              ArrEnrichmentStatus.enriched, ArrEnrichmentStatus.error).all()
        }
        # The row truth, keyed the way arr items are keyed ("svc:id"). This
        # is what the old version never read — it looked at ArrEnrichmentStatus
        # .error, which nothing wrote, so 798 not-found rows read as "retry".
        es_rows = {
            r.plex_rating_key: r
            for r in db.query(EnrichmentStatus.plex_rating_key, EnrichmentStatus.enriched,
                              EnrichmentStatus.error, EnrichmentStatus.provisional,
                              EnrichmentStatus.next_retry_at,
                              EnrichmentStatus.attempt_count).all()
        }
    now_dt = datetime.utcnow()

    # ── per-category classification over arr items ────────────────────────────
    categories: dict = {}
    for svc in ("radarr", "sonarr", "lidarr"):
        try:
            items_raw, _tags, _ci = await _fetch_arr_library(svc)
        except Exception as e:
            logger.debug("[kb-overview] %s not available: %s", svc, e)
            continue

        def _cat_of(raw: dict) -> str:
            if svc == "radarr":
                return "movie"
            if svc == "lidarr":
                return "music"
            root = (raw.get("rootFolderPath") or "").lower()
            return "anime" if "anime" in root else "show"

        for raw in items_raw:
            cat = _cat_of(raw)
            c = categories.setdefault(cat, {
                "denominator": {"arr_total": 0, "downloaded": 0},
                "states": {s: 0 for s in _STATES},
                "vectors": {"indexed": 0},
                # Why-not-100%: what the open rows are waiting for.
                "open": {"due_now": 0, "waiting": 0, "next_due_at": None,
                         "needs_attention": 0, "ignored": 0},
            })
            c["denominator"]["arr_total"] += 1
            if not _is_downloaded(svc, raw):
                continue
            c["denominator"]["downloaded"] += 1

            title = raw.get("title") or raw.get("artistName") or ""
            cands = _candidates(
                cat, svc, raw.get("id"), raw.get("tmdbId"), raw.get("tvdbId"),
                title, raw.get("foreignArtistId"))
            mts = ("show", "anime") if cat in ("show", "anime") else (cat,)

            if any(f"{m}:{k}" in chroma_ids or k in chroma_ids
                   for m in mts for k in cands):
                c["vectors"]["indexed"] += 1

            has_live = any(f"enriched:{m}:{k}" in live_enriched
                           for m in mts for k in cands)
            row = es_rows.get(f"{svc}:{raw.get('id')}")
            if row is None:
                flag = arr_flags.get((svc, raw.get("id")))
                if flag is not None:
                    row = _PseudoRow(enriched=flag[0], error=flag[1])
            if row is None:
                # Rule 1: a live profile under ANY historic key counts.
                state = "enriched" if has_live else "never_processed"
            else:
                # Status-derived state first; cache liveness only decides
                # enriched vs enriched_dead. A live not-found sentinel can no
                # longer masquerade as an enriched item.
                state = classify_enrichment_row(row, has_live_cache=has_live, now=now_dt)
            c["states"][state] += 1

            o = c["open"]
            if state in ("not_found", "retry_due"):
                if state == "retry_due":
                    o["due_now"] += 1
                else:
                    o["waiting"] += 1
                    nra = getattr(row, "next_retry_at", None)
                    if nra and (o["next_due_at"] is None or nra < o["next_due_at"]):
                        o["next_due_at"] = nra
                if (getattr(row, "attempt_count", 0) or 0) >= 2:
                    o["needs_attention"] += 1
            elif state in ("queued", "processing_error", "enriched_dead",
                           "rule_based", "awaiting_llm"):
                o["due_now"] += 1
            elif state == "ignored":
                o["ignored"] += 1

    for cat, c in categories.items():
        nda = c["open"]["next_due_at"]
        c["open"]["next_due_at"] = nda.isoformat() + "Z" if isinstance(nda, datetime) else None
        c["vectors"]["of"] = c["denominator"]["downloaded"]
        c["wikipedia"] = {"significance_cached": wiki_counts.get(cat, 0)}
        c["omdb"] = {"covered": omdb_counts.get(cat, 0)}
        c["reception"] = {"covered": reception_counts.get(cat, 0)}

    # ── watch-history-only section (its OWN set, never mixed into the above) ──
    from sqlalchemy import func, distinct, case
    with get_db_session() as db:
        wh_rows = {}
        # "enriched" here means a real profile (error IS NULL) — a not-found
        # sentinel also carries enriched=True and used to be counted as done.
        grouped = db.query(
            EnrichmentStatus.media_category,
            func.count(EnrichmentStatus.id).label("rows"),
            func.sum(case(((EnrichmentStatus.enriched == True) & EnrichmentStatus.error.is_(None), 1),
                          else_=0)).label("enriched"),
            func.sum(case((EnrichmentStatus.error.like("Not found%"), 1), else_=0)).label("not_found"),
        ).group_by(EnrichmentStatus.media_category).all()

        for r in grouped:
            cat = r.media_category or "?"
            b = wh_rows.setdefault(cat, {"rows": 0, "enriched": 0, "not_found": 0})
            b["rows"] += r.rows
            b["enriched"] += (r.enriched or 0)
            b["not_found"] += (r.not_found or 0)

        # ── music pipeline (Spotify cascade) ──────────────────────────────────
        spotify_total = (db.query(func.count(WatchHistoryEntry.id))
                         .filter(WatchHistoryEntry.source == "spotify").scalar()) or 0
        spotify_matched = (db.query(func.count(WatchHistoryEntry.id))
                           .filter(WatchHistoryEntry.source == "spotify",
                                   ~WatchHistoryEntry.plex_item_id.like("spotify%"))
                           .scalar()) or 0
        genre_covered = (db.query(func.count(WatchHistoryEntry.id))
                         .filter(WatchHistoryEntry.source == "spotify",
                                 WatchHistoryEntry.genres.isnot(None),
                                 WatchHistoryEntry.genres != "").scalar()) or 0
        artists_total = (db.query(func.count(distinct(WatchHistoryEntry.series_title)))
                         .filter(WatchHistoryEntry.media_type == "music",
                                 WatchHistoryEntry.series_title.isnot(None)).scalar()) or 0
        artists_mbid = (db.query(func.count(distinct(WatchHistoryEntry.series_title)))
                        .filter(WatchHistoryEntry.media_type == "music",
                                WatchHistoryEntry.series_title.isnot(None),
                                WatchHistoryEntry.artist_mbid.isnot(None)).scalar()) or 0

    # ── storage ────────────────────────────────────────────────────────────────
    storage = {
        "enrichment_cache_mb": _file_size_mb(settings.ENRICHMENT_CACHE),
        "main_db_mb": _file_size_mb(DATA_DIR / "curatarr.db"),
        "chromadb_mb": _dir_size_mb(str(settings.CHROMADB_PATH)),
    }
    storage["total_mb"] = round(sum(storage.values()), 1)

    payload = {
        "generated_at": int(now),
        "categories": categories,
        # The vocabulary, served: label / explainer / next_step per state so
        # the tile, the drilldown and the chat never carry their own copy.
        "state_definitions": STATE_DEFINITIONS,
        "done_states": list(DONE_STATES),
        "watch_history_only": wh_rows,
        "music_pipeline": {
            "plex_match": {"done": spotify_matched, "of": spotify_total},
            "mbid_resolve": {"done": artists_mbid, "of": artists_total},
            "genre_coverage": {"done": genre_covered, "of": spotify_total},
        },
        "storage": storage,
    }
    _CACHE.update(at=now, payload=payload)
    return payload
