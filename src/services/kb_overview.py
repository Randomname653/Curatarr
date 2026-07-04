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
from pathlib import Path

logger = logging.getLogger(__name__)

# 60s payload cache — the endpoint may be polled by the KB view.
_CACHE: dict = {"at": 0.0, "payload": None}
_CACHE_TTL = 60.0

_STATES = ("enriched_live", "enriched_dead", "awaiting_polish",
           "retry_queued", "not_findable", "never_processed")


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
        live_raw = mc.live_key_set("raw:")
        wiki_counts = {c: mc.count_raw_with_marker(c, '"significance"')
                       for c in ("movie", "show", "anime", "music")}
        omdb_counts = {c: mc.count_raw_with_marker(c, '"omdb_checked"')
                       for c in ("movie", "show", "anime", "music")}
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

            flag = arr_flags.get((svc, raw.get("id")))
            has_live = any(f"enriched:{m}:{k}" in live_enriched
                           for m in mts for k in cands)
            has_raw = any(f"raw:{m}:{k}" in live_raw
                          for m in mts for k in cands)
            err = (flag[1] or "") if flag else ""

            if has_live:
                state = "enriched_live"
            elif flag and flag[0] and not err:
                state = "enriched_dead"          # flag says done, cache is gone
            elif err.lower().startswith("not found"):
                state = "not_findable"
            elif has_raw:
                state = "awaiting_polish"        # api data cached, no profile yet
            elif flag:
                state = "retry_queued"           # tracked, errored, will retry
            else:
                state = "never_processed"
            c["states"][state] += 1

    for cat, c in categories.items():
        c["vectors"]["of"] = c["denominator"]["downloaded"]
        c["wikipedia"] = {"significance_cached": wiki_counts.get(cat, 0)}
        c["omdb"] = {"covered": omdb_counts.get(cat, 0)}

    # ── watch-history-only section (its OWN set, never mixed into the above) ──
    with get_db_session() as db:
        wh_rows = {}
        for r in db.query(EnrichmentStatus.media_category, EnrichmentStatus.enriched).all():
            b = wh_rows.setdefault(r.media_category or "?", {"rows": 0, "enriched": 0})
            b["rows"] += 1
            if r.enriched:
                b["enriched"] += 1

        # ── music pipeline (Spotify cascade) ──────────────────────────────────
        from sqlalchemy import func, distinct
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
        "main_db_mb": _file_size_mb(Path("data") / "curatarr.db"),
        "chromadb_mb": _dir_size_mb(str(settings.CHROMADB_PATH)),
    }
    storage["total_mb"] = round(sum(storage.values()), 1)

    payload = {
        "generated_at": int(now),
        "categories": categories,
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
