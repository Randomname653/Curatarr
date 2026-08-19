"""Curatarr — LLM-free raw-cache refresher (owner ask 2026-08-18).

WHY: the cache is read-through — a row is only ever re-pulled when some
consumer asks for that exact key again. Items nobody actively touches
(cold artists, gone media, discovery titles never clicked) silently age
out; 103k expired rows had piled up that way. This walker keeps the RAW
layer (pure API data: TMDB/AniList/OMDb/Jikan — no LLM anywhere) warm in
the background, so evidence building, zombie rebuilds and discovery
cards always stand on fresh source data — and the LLM enrichment cycle
can then run on top whenever GPU capacity allows (it already does, via
its own custodian task).

Candidate order per tick (budgeted):
  1. Discovery recommendations without a live raw row (cards the owner
     is actively being shown).
  2. Expired raw_prefetch rows — read STALE on purpose: the stored ids
     (tmdb/anilist/mbid) let us re-fetch even for media long gone from
     the arrs (the zombie-rebuild source stays current).
Cursor in app_state; the custodian re-runs it daily.
"""

import json
import logging

logger = logging.getLogger(__name__)

_CURSOR_KEY = "raw_refresh_cursor"
_BUDGET = 150          # API fetches per tick — polite to rate limits


def _prog(task, message=None, processed=None, total=None):
    if task is None:
        return
    try:
        from src.services.task_monitor import task_monitor
        kw = {}
        if processed is not None:
            kw["processed"] = processed
        if total is not None:
            kw["total"] = total
        task_monitor.update(task, message=message, **kw)
    except Exception:
        pass


async def _refresh_one(raw: dict) -> bool:
    """Re-fetch one item's raw data from the ids stored in its old row and
    write it back under the same keys. Returns True on a fresh write."""
    from src.services.media_enricher import fetch_and_prepare_raw
    from src.cache.metadata_cache import MetadataCache

    title = (raw.get("title") or "").strip()
    media_type = raw.get("media_type") or raw.get("_media_type") or "movie"
    if media_type == "tv":
        media_type = "show"
    if not title:
        return False
    fresh = await fetch_and_prepare_raw(
        title, media_type,
        tmdb_id=raw.get("tmdb_id") or raw.get("_tmdb_id"),
        anilist_id=raw.get("anilist_id") or raw.get("_anilist_id"),
        anidb_id=raw.get("anidb_id"),
        tvdb_id=raw.get("tvdb_id"),
        imdb_id=raw.get("imdb_id"),
        mal_id=raw.get("mal_id"),
        mbid=raw.get("mbid") or raw.get("musicbrainz_id"),
        plex_rating_key=raw.get("_plex_rating_key") or raw.get("plex_rating_key"),
        year=raw.get("year"),
        fast_only=True,
    )
    if not isinstance(fresh, dict):
        return False
    if fresh.get("_already_enriched"):
        return False   # profile fresh — raw refresh unnecessary
    mc = MetadataCache()
    try:
        key = fresh.get("_cache_key") or f"raw:{media_type}:{title[:40]}"
        mc.set_cache(key, fresh, days=30)
        prk = fresh.get("_plex_rating_key") or raw.get("_plex_rating_key")
        if prk:
            mc.set_cache(f"raw_prefetch:{prk}", fresh, days=30)
    finally:
        mc.close()
    return True


async def run_raw_refresh(task=None, deep: bool = False) -> bool:
    """Custodian runner. Returns True when the backlog is drained this pass
    (task stamps its cadence), False to continue next tick. ``deep`` triples
    the per-tick budget for owner-triggered catch-up sprints."""
    from src.cache.metadata_cache import MetadataCache
    from src.services.app_state import get_state, set_state

    budget = _BUDGET * (3 if deep else 1)
    done = 0

    # ── 1. discovery recs without a live raw row ─────────────────────────────
    try:
        from src.database.connection import get_db_session
        from src.database.models import CachedRecommendation
        mc = MetadataCache()
        try:
            with get_db_session() as db:
                recs = (db.query(CachedRecommendation.title,
                                 CachedRecommendation.category,
                                 CachedRecommendation.tmdb_id)
                        .filter(CachedRecommendation.tmdb_id.isnot(None))
                        .limit(200).all())
            for title, cat, tmdb_id in recs:
                if done >= budget // 3:
                    break   # discovery gets at most a third of the budget
                if mc.get_cache(f"raw:{cat}:{tmdb_id}"):
                    continue
                _prog(task, message=f"Discovery raw: {title}", processed=done,
                      total=budget)
                if await _refresh_one({"title": title, "media_type": cat,
                                       "tmdb_id": tmdb_id}):
                    done += 1
        finally:
            mc.close()
    except Exception as e:
        logger.debug("[raw-refresh] discovery pass failed: %s", e)

    # ── 2. expired raw_prefetch rows (stale-read → re-fetch by stored ids) ───
    try:
        cursor = int(get_state(_CURSOR_KEY) or 0)
    except Exception:
        cursor = 0
    mc = MetadataCache()
    try:
        rows = mc.conn.execute(
            "SELECT rowid, cache_key, response FROM api_cache "
            "WHERE (cache_key LIKE '%raw_prefetch:%') "
            "AND expires_at <= datetime('now') AND rowid > ? "
            "ORDER BY rowid LIMIT ?", (cursor, max(budget - done, 0) * 3),
        ).fetchall()
    finally:
        mc.close()
    drained = True
    for rowid, key, resp in rows:
        if done >= budget:
            drained = False
            break
        cursor = rowid
        try:
            raw = json.loads(resp)
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        _prog(task, message=f"Raw refresh: {raw.get('title', key)}",
              processed=done, total=budget)
        if await _refresh_one(raw):
            done += 1
    try:
        set_state(_CURSOR_KEY, "0" if drained and len(rows) < budget * 3
                  else str(cursor))
    except Exception:
        pass
    _prog(task, message=f"{done} raw entries refreshed", processed=done,
          total=max(budget, 1))
    logger.info("[raw-refresh] %d entries refreshed (cursor %s)", done, cursor)
    return drained and len(rows) < budget * 3
