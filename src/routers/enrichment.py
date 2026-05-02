"""
Curatarr 1.0 - Enrichment Router

Category-specific metadata enrichment with progress tracking.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.database import get_db
from src.database.connection import get_db_session
from src.database.models import User, WatchHistoryEntry, EnrichmentStatus
from src.routers.auth import get_current_user
from src.services.app_state import get_state, set_state

logger = logging.getLogger(__name__)
from src.services.task_monitor import task_monitor
router = APIRouter()

CATEGORIES = ["music", "movie", "show", "anime"]


class EnrichRequest(BaseModel):
    categories: list = []       # empty = all
    source: str = "watch_history"  # watch_history / radarr / sonarr / lidarr / all
    limit: Optional[int] = None    # None = no limit
    force: bool = False            # True = re-enrich even if already done (clears cache first)


@router.get("/status")
async def enrichment_status(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return enrichment progress per category including ARR library counts."""
    from sqlalchemy import func as _func

    result = {}
    for cat in CATEGORIES:
        # Total: count unique series/artists, not individual episodes/tracks
        if cat in ("show", "anime"):
            # Count unique series
            total_unique = db.query(
                _func.count(_func.distinct(WatchHistoryEntry.series_title))
            ).filter(
                WatchHistoryEntry.user_id == user.id,
                WatchHistoryEntry.media_type == cat,
                WatchHistoryEntry.series_title != None,
            ).scalar() or 0
            total_raw = db.query(WatchHistoryEntry).filter(
                WatchHistoryEntry.user_id == user.id,
                WatchHistoryEntry.media_type == cat,
            ).count()
        elif cat == "music":
            # Count unique artists
            total_unique = db.query(
                _func.count(_func.distinct(WatchHistoryEntry.series_title))
            ).filter(
                WatchHistoryEntry.user_id == user.id,
                WatchHistoryEntry.media_type == cat,
                WatchHistoryEntry.series_title != None,
            ).scalar() or 0
            total_raw = db.query(WatchHistoryEntry).filter(
                WatchHistoryEntry.user_id == user.id,
                WatchHistoryEntry.media_type == cat,
            ).count()
        else:
            total_unique = db.query(WatchHistoryEntry).filter(
                WatchHistoryEntry.user_id == user.id,
                WatchHistoryEntry.media_type == cat,
            ).count()
            total_raw = total_unique

        # Enriched: count unique titles in EnrichmentStatus
        enriched = db.query(
            _func.count(_func.distinct(EnrichmentStatus.title))
        ).filter(
            EnrichmentStatus.media_category == cat,
            EnrichmentStatus.enriched == True,
        ).scalar() or 0

        vector_ready = db.query(
            _func.count(_func.distinct(EnrichmentStatus.title))
        ).filter(
            EnrichmentStatus.media_category == cat,
            EnrichmentStatus.vector_ready == True,
        ).scalar() or 0

        result[cat] = {
            "total_in_history": total_raw,
            "total_unique": total_unique,  # unique series/artists
            "enriched": enriched,
            "not_enriched": max(0, total_unique - enriched),
            "vector_ready": vector_ready,
            "pct": round(100 * enriched / max(total_unique, 1)),
            "unit": "series" if cat in ("show","anime") else "artists" if cat=="music" else "items",
        }

    # Fetch ARR library sizes (quick, no enrichment check needed)
    arr_counts = await _get_arr_counts()

    last_run = get_state("last_enrichment_at")
    return {
        "categories": result,
        "arr": arr_counts,
        "last_run": last_run,
        "running": get_state("enrichment_running") == "1",
    }


async def _get_arr_counts() -> dict:
    """
    Fetch item counts from ARR services.
    Results are cached in AppState (DB) — refreshed every 5 minutes or on demand.
    """
    import json as _json
    import httpx as _httpx
    from src.config import settings
    from src.services.app_state import get_state, set_state
    from datetime import datetime, timedelta

    # Check DB cache — return if fresh (< 5 min old)
    cached_raw = get_state("arr_counts_cache")
    cached_ts  = get_state("arr_counts_ts")
    if cached_raw and cached_ts:
        try:
            ts = datetime.fromisoformat(cached_ts)
            if datetime.utcnow() - ts < timedelta(minutes=5):
                return _json.loads(cached_raw)
        except Exception:
            pass

    counts = {}

    # Load previous cached data as fallback for unreachable services
    prev_cached = {}
    if cached_raw:
        try:
            prev_cached = _json.loads(cached_raw)
        except Exception:
            pass

    from src.database.connection import get_db_session
    from src.database.models import ArrEnrichmentStatus

    if settings.RADARR_URL and settings.RADARR_API_KEY:
        try:
            async with _httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    f"{settings.RADARR_URL.rstrip('/')}/api/v3/movie",
                    headers={"X-Api-Key": settings.RADARR_API_KEY},
                )
            if r.status_code == 200:
                movies = r.json()
                total = len(movies)
                downloaded = sum(1 for m in movies if m.get("hasFile"))
                with get_db_session() as db:
                    enriched = db.query(ArrEnrichmentStatus).filter(
                        ArrEnrichmentStatus.service == "radarr",
                        ArrEnrichmentStatus.enriched == True,
                    ).count()
                counts["radarr"] = {
                    "total": total,
                    "downloaded": downloaded,
                    "monitored": sum(1 for m in movies if m.get("monitored")),
                    "enriched": enriched,
                    "pct": round(100 * enriched / max(downloaded, 1)),
                    "stale": False,
                }
            else:
                raise Exception(f"HTTP {r.status_code}")
        except Exception as e:
            # Use cached data if available, mark as stale
            if "radarr" in prev_cached and not prev_cached["radarr"].get("error"):
                counts["radarr"] = {**prev_cached["radarr"], "stale": True,
                                    "stale_reason": str(e)}
            else:
                counts["radarr"] = {"error": "unreachable", "stale": True}

    if settings.SONARR_URL and settings.SONARR_API_KEY:
        try:
            async with _httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    f"{settings.SONARR_URL.rstrip('/')}/api/v3/series",
                    headers={"X-Api-Key": settings.SONARR_API_KEY},
                )
            if r.status_code == 200:
                series = r.json()
                downloaded = sum(1 for s in series
                                 if s.get("statistics", {}).get("episodeFileCount", 0) > 0)
                with get_db_session() as db:
                    enriched = db.query(ArrEnrichmentStatus).filter(
                        ArrEnrichmentStatus.service == "sonarr",
                        ArrEnrichmentStatus.enriched == True,
                    ).count()
                counts["sonarr"] = {
                    "total": len(series),
                    "downloaded": downloaded,
                    "monitored": sum(1 for s in series if s.get("monitored")),
                    "enriched": enriched,
                    "pct": round(100 * enriched / max(downloaded, 1)),
                    "stale": False,
                }
            else:
                raise Exception(f"HTTP {r.status_code}")
        except Exception as e:
            if "sonarr" in prev_cached and not prev_cached["sonarr"].get("error"):
                counts["sonarr"] = {**prev_cached["sonarr"], "stale": True,
                                    "stale_reason": str(e)}
            else:
                counts["sonarr"] = {"error": "unreachable", "stale": True}

    if settings.LIDARR_URL and settings.LIDARR_API_KEY:
        try:
            async with _httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    f"{settings.LIDARR_URL.rstrip('/')}/api/v1/artist",
                    headers={"X-Api-Key": settings.LIDARR_API_KEY},
                )
            if r.status_code == 200:
                artists = r.json()
                downloaded = sum(1 for a in artists
                                 if a.get("statistics", {}).get("trackFileCount", 0) > 0)
                # Also count albums
                total_albums = sum(
                    a.get("statistics", {}).get("albumCount", 0) for a in artists
                )
                with get_db_session() as db:
                    enriched_artists = db.query(ArrEnrichmentStatus).filter(
                        ArrEnrichmentStatus.service == "lidarr",
                        ArrEnrichmentStatus.enriched == True,
                        ArrEnrichmentStatus.category == "artist",
                    ).count()
                    enriched_albums = db.query(ArrEnrichmentStatus).filter(
                        ArrEnrichmentStatus.service == "lidarr",
                        ArrEnrichmentStatus.enriched == True,
                        ArrEnrichmentStatus.category == "album",
                    ).count()
                counts["lidarr"] = {
                    "total": len(artists),
                    "total_albums": total_albums,
                    "downloaded": downloaded,
                    "monitored": sum(1 for a in artists if a.get("monitored")),
                    "enriched": enriched_artists,
                    "enriched_albums": enriched_albums,
                    "pct": round(100 * enriched_artists / max(len(artists), 1)),
                    "stale": False,
                }
            else:
                raise Exception(f"HTTP {r.status_code}")
        except Exception as e:
            if "lidarr" in prev_cached and not prev_cached["lidarr"].get("error"):
                counts["lidarr"] = {**prev_cached["lidarr"], "stale": True,
                                    "stale_reason": str(e)}
            else:
                counts["lidarr"] = {"error": "unreachable", "stale": True}

    # Persist to AppState so it survives restarts and is shared across workers
    import json as _json2
    from src.services.app_state import set_state as _set
    _set("arr_counts_cache", _json2.dumps(counts))
    _set("arr_counts_ts", datetime.utcnow().isoformat())
    return counts


@router.post("/arr-refresh")
async def refresh_arr_counts(user: User = Depends(get_current_user)):
    """Force-refresh ARR library counts (bypass 5-min cache)."""
    from src.services.app_state import set_state
    set_state("arr_counts_ts", "")  # invalidate cache
    counts = await _get_arr_counts()
    return {"arr": counts}


@router.post("/start")
async def start_enrichment(
    req: EnrichRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
):
    """Start enrichment for specified categories and sources."""
    if get_state("enrichment_running") == "1":
        raise HTTPException(status_code=409, detail="Enrichment already running")

    cats = req.categories if req.categories else CATEGORIES
    background_tasks.add_task(
        _run_enrichment, user.id, cats, req.source, req.limit, req.force
    )
    return {
        "status": "started",
        "categories": cats,
        "source": req.source,
    }


@router.post("/compute-taste")
async def compute_taste(
    categories: list = None,
    background_tasks: BackgroundTasks = None,
    user: User = Depends(get_current_user),
):
    """Recompute taste vectors from enriched data."""
    background_tasks.add_task(_run_taste_computation, user.id, categories)
    return {"status": "started"}


# ── BACKGROUND TASKS ──────────────────────────────────────────────────────────

async def _db_write_with_retry(fn, max_retries=5):
    """Execute a DB write function with exponential backoff on lock errors."""
    import time
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                wait = 0.5 * (2 ** attempt)
                logger.debug("DB locked, retry %d/%d in %.1fs", attempt+1, max_retries, wait)
                await asyncio.sleep(wait)
            else:
                raise


async def _run_enrichment(user_id: int, categories: list, source: str, limit: Optional[int], force: bool = False):
    """Background: fetch metadata for all items in watch history. force=True clears existing cache first."""
    if force:
        logger.info("Force re-enrichment requested — clearing cache for %s", categories)
        try:
            from src.cache.metadata_cache import MetadataCache
            import sqlite3
            _mc = MetadataCache()
            conn = sqlite3.connect(_mc.cache_path)
            for cat in categories:
                deleted = conn.execute(
                    "DELETE FROM api_cache WHERE cache_key LIKE ?",
                    (f"enriched:{cat}:%",)
                ).rowcount
                logger.info("Cleared %d cache entries for %s", deleted, cat)
            conn.commit()
            conn.close()
            _mc.close()
            with get_db_session() as db:
                db.query(EnrichmentStatus).filter(
                    EnrichmentStatus.media_category.in_(categories)
                ).update({"enriched": False, "error": None, "enriched_at": None},
                         synchronize_session=False)
                db.commit()
            logger.info("Force-reset EnrichmentStatus for %s", categories)
        except Exception as e:
            logger.warning("Force-clear failed: %s", e)
    from src.services.media_enricher import enrich_media_item

    set_state("enrichment_running", "1")
    set_state("enrichment_progress", "0")
    _enrich_task = task_monitor.create(
        name=f"Enrichment: {', '.join(categories)}",
        category="enrichment",
    )
    task_monitor.start(_enrich_task)

    try:
        # Collect items to enrich
        items = []
        with get_db_session() as db:
            for cat in categories:
                q = db.query(WatchHistoryEntry).filter(
                    WatchHistoryEntry.user_id == user_id,
                    WatchHistoryEntry.media_type == cat,
                )
                if source == "watch_history":
                    entries = q.all()
                elif source == "radarr" and cat == "movie":
                    entries = q.all()
                elif source in ("sonarr",) and cat in ("show", "anime"):
                    entries = q.all()
                elif source == "lidarr" and cat == "music":
                    entries = q.all()
                else:
                    entries = q.all()

                for e in entries:
                    items.append({
                        "plex_rating_key": e.plex_item_id,
                        "title": e.title,
                        "series_title": e.series_title,
                        "album_title": e.parent_title if hasattr(e, 'parent_title') else None,
                        "media_type": cat,
                        "tmdb_id": e.tmdb_id,
                        "tvdb_id": None,
                        "imdb_id": None,
                    })

        # For shows/anime: deduplicate on series_title — enrich once per series not per episode
        # For music: deduplicate on artist (series_title) — enrich once per artist not per track
        deduped_items = []
        seen_series = set()
        for item in items:
            cat = item["media_type"]
            if cat in ("show", "anime") and item.get("series_title"):
                key = f"{cat}:{item['series_title'].lower().strip()}"
                if key in seen_series:
                    continue
                seen_series.add(key)
                item = dict(item)
                if item.get("tmdb_id") and item["tmdb_id"] > 200000:
                    item["tmdb_id"] = None
            elif cat == "music" and item.get("series_title"):
                # Music: deduplicate at artist level
                key = f"music:{item['series_title'].lower().strip()}"
                if key in seen_series:
                    continue
                seen_series.add(key)
            deduped_items.append(item)

        original_count = len(items)
        items = deduped_items
        if original_count != len(items):
            logger.info("Deduped %d items → %d unique series/items", original_count, len(items))

        # Enrich items with additional IDs from MediaIdentity
        if items:
            from src.database.models import MediaIdentity
            with get_db_session() as db:
                plex_keys = [i["plex_rating_key"] for i in items]
                identities = {
                    mi.plex_rating_key: mi
                    for mi in db.query(MediaIdentity).filter(
                        MediaIdentity.plex_rating_key.in_(plex_keys)
                    ).all()
                }
                # Convert to dicts inside session
                id_map = {
                    k: {"tvdb_id": mi.tvdb_id, "imdb_id": mi.imdb_id,
                        "anilist_id": mi.anilist_id, "tmdb_id": mi.tmdb_id}
                    for k, mi in identities.items()
                }
            for item in items:
                ids = id_map.get(item["plex_rating_key"], {})
                if ids.get("tvdb_id"):   item["tvdb_id"]   = ids["tvdb_id"]
                if ids.get("imdb_id"):   item["imdb_id"]   = ids["imdb_id"]
                if ids.get("anilist_id"): item["anilist_id"] = ids["anilist_id"]
                # Only override tmdb_id if currently None or episode-level
                if ids.get("tmdb_id") and (not item.get("tmdb_id") or
                        (item.get("tmdb_id", 0) > 200000)):
                    item["tmdb_id"] = ids["tmdb_id"]

        if limit:
            items = items[:limit]

        logger.info("Enrichment: %d items across %s", len(items), categories)
        total = len(items)
        processed = 0
        sem = asyncio.Semaphore(1)  # 1 at a time — SQLite can't handle concurrent writes reliably

        async def enrich_one(item: dict):
            nonlocal processed
            if _enrich_task.status.value == "skipped":
                processed += 1
                return
            try:
                cat = item["media_type"]
                canonical = item.get("series_title") or item["title"]

                # Check if already enriched by canonical title WITH LLM structuring
                with get_db_session() as db:
                    existing_by_title = db.query(EnrichmentStatus).filter(
                        EnrichmentStatus.title == canonical,
                        EnrichmentStatus.media_category == cat,
                    ).first()
                    if existing_by_title and existing_by_title.enriched:
                        # Check if the cached profile has LLM structuring
                        from src.cache.metadata_cache import MetadataCache as _MC3
                        _c3 = _MC3()
                        _ck3 = f"enriched:{cat}:{existing_by_title.plex_rating_key}"
                        _cached3 = _c3.get_cache(_ck3)
                        _c3.close()
                        cached_source = (_cached3 or {}).get("response", {}).get("source", "")
                        if "+llm" in cached_source or cached_source == "llm":
                            return  # already has LLM profile, skip
                        # Has enriched=True but no LLM — fall through to re-enrich
                        logger.debug("Re-enriching '%s' — missing LLM structuring (source=%s)",
                                     canonical, cached_source)
                    elif existing_by_title and not existing_by_title.enriched:
                        # Has error — clear it and retry
                        existing_by_title.error = None
                        db.commit()

                async with sem:
                    if _enrich_task.status.value == "skipped":
                        processed += 1
                        return
                    if cat == "music":
                        artist = item.get("series_title") or item["title"]
                        if not artist:
                            profile = None
                        else:
                            profile = await enrich_media_item(
                                title=artist,
                                media_type="music",
                                plex_rating_key=item["plex_rating_key"],
                            )
                    else:
                        # For shows/anime: don't pass episode-level or TVDB IDs as TMDB IDs
                        # enrich_media_item will use tvdb_id for title search fallback
                        use_tmdb = item.get("tmdb_id")
                        if cat in ("show", "anime") and use_tmdb and use_tmdb > 200000:
                            use_tmdb = None  # episode-level ID, not usable for series enrichment
                        profile = await enrich_media_item(
                            title=item.get("series_title") or item["title"],
                            media_type=cat,
                            tmdb_id=use_tmdb,
                            tvdb_id=item.get("tvdb_id"),
                            imdb_id=item.get("imdb_id"),
                            sonarr_series_type=item.get("sonarr_series_type"),
                            plex_rating_key=item["plex_rating_key"],
                        )

                # Use canonical key for EnrichmentStatus:
                # - Shows/Anime: series_title (one row per series)
                # - Music: series_title = artist name (one row per artist)
                # - Movies: title (one row per movie)
                canonical_title = item.get("series_title") or item["title"]

                # Write with retry on lock
                for _attempt in range(5):
                    try:
                        with get_db_session() as db:
                            status = db.query(EnrichmentStatus).filter(
                                EnrichmentStatus.title == canonical_title,
                                EnrichmentStatus.media_category == cat,
                            ).first()
                            if not status:
                                status = EnrichmentStatus(
                                    plex_rating_key=item["plex_rating_key"],
                                    title=canonical_title,
                                    media_category=cat,
                                )
                                db.add(status)
                            status.enriched = profile is not None
                            status.enriched_at = datetime.utcnow() if profile else None
                            status.error = None if profile else "No data found"
                            if not profile:
                                logger.warning("Enrichment failed for '%s' (%s)",
                                               canonical_title, cat)
                            db.commit()
                        break  # success
                    except Exception as _e:
                        if "database is locked" in str(_e) and _attempt < 4:
                            await asyncio.sleep(1.0 * (_attempt + 1))
                        else:
                            raise

                # Track in ArrEnrichmentStatus — either directly (ARR source) or via title match
                if profile and item.get("arr_id") and item.get("service"):
                    from src.database.models import ArrEnrichmentStatus
                    with get_db_session() as db:
                        arr_s = db.query(ArrEnrichmentStatus).filter(
                            ArrEnrichmentStatus.service == item["service"],
                            ArrEnrichmentStatus.arr_id == item["arr_id"],
                        ).first()
                        if not arr_s:
                            arr_s = ArrEnrichmentStatus(
                                service=item["service"],
                                arr_id=item["arr_id"],
                                category=cat,
                                title=item.get("series_title") or item["title"],
                                tmdb_id=item.get("tmdb_id"),
                                tvdb_id=item.get("tvdb_id"),
                            )
                            db.add(arr_s)
                        arr_s.enriched = True
                        arr_s.enriched_at = datetime.utcnow()
                        db.commit()
                elif profile and (item.get("series_title") or item.get("title")):
                    # No arr_id — try to match by title to update ArrEnrichmentStatus
                    search_title = item.get("series_title") or item["title"]
                    try:
                        from src.database.models import ArrEnrichmentStatus as _AES
                        with get_db_session() as db:
                            arr_match = db.query(_AES).filter(
                                _AES.title == search_title,
                                _AES.category == cat,
                                _AES.enriched == False,
                            ).first()
                            if arr_match:
                                arr_match.enriched = True
                                arr_match.enriched_at = datetime.utcnow()
                                db.commit()
                    except Exception:
                        pass

                # Also store under plex_rating_key so taste_engine can find it
                # For shows/anime: write under ALL episode plex_item_ids of this series
                if profile:
                    from src.cache.metadata_cache import MetadataCache as _MC
                    _c = _MC()
                    _c.set_cache(
                        f"enriched:{cat}:{item['plex_rating_key']}",
                        profile, days=90,
                    )
                    # Propagate to all episodes of same series
                    if cat in ("show", "anime") and item.get("series_title"):
                        with get_db_session() as _db:
                            related_pids = [
                                r.plex_item_id
                                for r in _db.query(WatchHistoryEntry).filter(
                                    WatchHistoryEntry.series_title == item["series_title"],
                                    WatchHistoryEntry.media_type == cat,
                                ).with_entities(WatchHistoryEntry.plex_item_id).limit(1000).all()
                            ]
                        for rpid in related_pids:
                            if rpid != item["plex_rating_key"]:
                                _c.set_cache(f"enriched:{cat}:{rpid}", profile, days=90)
                        # Also mark all episode EnrichmentStatus rows as enriched
                        with get_db_session() as _db:
                            _db.query(EnrichmentStatus).filter(
                                EnrichmentStatus.media_category == cat,
                                EnrichmentStatus.plex_rating_key.in_(related_pids),
                            ).update({
                                "enriched": True,
                                "enriched_at": datetime.utcnow(),
                            }, synchronize_session=False)
                            _db.commit()
                    elif cat == "music" and item.get("series_title"):
                        # Music: propagate to all tracks by this artist
                        with get_db_session() as _db:
                            related_pids = [
                                r.plex_item_id
                                for r in _db.query(WatchHistoryEntry).filter(
                                    WatchHistoryEntry.series_title == item["series_title"],
                                    WatchHistoryEntry.media_type == "music",
                                ).with_entities(WatchHistoryEntry.plex_item_id).limit(5000).all()
                            ]
                        for rpid in related_pids:
                            if rpid != item["plex_rating_key"]:
                                _c.set_cache(f"enriched:music:{rpid}", profile, days=90)
                    _c.close()

            except Exception as e:
                import traceback
                logger.warning("Enrich error for '%s': %s\n%s",
                               item.get("series_title") or item.get("title"),
                               e, traceback.format_exc()[-800:])
            finally:
                processed += 1
                pct = int(100 * processed / max(total, 1))
                set_state("enrichment_progress", str(pct))
                task_monitor.update(_enrich_task, processed=processed, total=total)
                await asyncio.sleep(0.2)

        try:
            await asyncio.gather(*[enrich_one(item) for item in items], return_exceptions=True)
        except asyncio.CancelledError:
            logger.info("Enrichment cancelled during gather (%d/%d done)", processed, total)
            raise

        set_state("last_enrichment_at", datetime.utcnow().isoformat())
        logger.info("Enrichment complete: %d/%d items", processed, total)
        task_monitor.done(_enrich_task, f"Complete: {processed}/{total} items enriched")

        # Auto-compute taste vectors after enrichment — now with real metadata
        await _run_taste_computation(user_id, categories)

        # Trigger verification questions
        try:
            from src.services.verification_session import start_verification_session
            await start_verification_session(user_id)
        except Exception as e:
            logger.debug("Post-enrichment verification failed: %s", e)

    except asyncio.CancelledError:
        logger.info("Enrichment background task cancelled (server shutdown) — %d/%d processed", processed, total)
        task_monitor.skip(_enrich_task, f"Cancelled: {processed}/{total} items enriched")
    finally:
        set_state("enrichment_running", "0")


async def _run_taste_computation(user_id: int, categories: list = None):
    """Background: compute taste vectors from enriched data."""
    from src.services.taste_engine import compute_all_taste_vectors
    logger.info("Computing taste vectors for user %d...", user_id)
    await compute_all_taste_vectors(user_id)
    logger.info("Taste vectors updated for user %d", user_id)


# ── PROFILE BROWSER + MAPPING STATS ──────────────────────────────────────────

@router.get("/mapping-stats")
async def get_mapping_stats(user: User = Depends(get_current_user)):
    """Return anime ID mapping statistics."""
    from src.services.anime_mapping import get_mapping_stats
    stats = await get_mapping_stats()
    return stats


@router.get("/profiles")
async def browse_profiles(
    category: str = "movie",
    limit: int = 10,
    offset: int = 0,
    search: str = "",
    user: User = Depends(get_current_user),
):
    """Browse enriched profiles — for debugging and quality checking."""
    from src.cache.metadata_cache import MetadataCache
    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry, EnrichmentStatus

    cache = MetadataCache()

    with get_db_session() as db:
        # For shows/anime: deduplicate on series title via WatchHistoryEntry
        # For movies/music: use EnrichmentStatus directly
        if category in ("show", "anime"):
            # Get unique series titles from WatchHistoryEntry, joined with EnrichmentStatus
            from sqlalchemy import func as _func
            q = db.query(
                WatchHistoryEntry.series_title,
                _func.max(EnrichmentStatus.plex_rating_key).label("plex_rating_key"),
                _func.max(EnrichmentStatus.enriched_at).label("enriched_at"),
            ).join(
                EnrichmentStatus,
                EnrichmentStatus.plex_rating_key == WatchHistoryEntry.plex_item_id,
            ).filter(
                WatchHistoryEntry.media_type == category,
                EnrichmentStatus.enriched == True,
                WatchHistoryEntry.series_title != None,
            ).group_by(WatchHistoryEntry.series_title)
            if search:
                q = q.filter(WatchHistoryEntry.series_title.ilike(f"%{search}%"))
            total = q.count()
            rows_raw = q.order_by(_func.max(EnrichmentStatus.enriched_at).desc()).offset(offset).limit(limit).all()
            items = [(r.plex_rating_key, r.series_title, r.enriched_at) for r in rows_raw]
        else:
            q = db.query(
                EnrichmentStatus.plex_rating_key,
                EnrichmentStatus.title,
                EnrichmentStatus.enriched_at,
            ).filter(
                EnrichmentStatus.media_category == category,
                EnrichmentStatus.enriched == True,
            )
            if search:
                q = q.filter(EnrichmentStatus.title.ilike(f"%{search}%"))

            # Deduplicate by title — one profile per series/movie
            from sqlalchemy import func as _func
            dedup_q = q.with_entities(
                EnrichmentStatus.title,
                _func.max(EnrichmentStatus.plex_rating_key).label("plex_rating_key"),
                _func.max(EnrichmentStatus.enriched_at).label("enriched_at"),
            ).group_by(EnrichmentStatus.title)
            total = dedup_q.count()
            rows = dedup_q.order_by(
                _func.max(EnrichmentStatus.enriched_at).desc()
            ).offset(offset).limit(limit).all()
            items = [(r.plex_rating_key, r.title, r.enriched_at) for r in rows]

    profiles = []
    for plex_key, title, enriched_at in items:
        cache_key = f"enriched:{category}:{plex_key}"
        cached = cache.get_cache(cache_key)
        profile = cached["response"] if cached else None

        profiles.append({
            "plex_rating_key": plex_key,
            "title": title,
            "enriched_at": enriched_at.isoformat() if enriched_at else None,
            "has_profile": profile is not None,
            "genres": profile.get("genres", []) if profile else [],
            "themes": profile.get("themes", []) if profile else [],
            "mood": profile.get("mood", []) if profile else [],
            "plot_summary": profile.get("plot_summary") or profile.get("overview", "")[:300] if profile else None,
            "embedding_text": profile.get("embedding_text", "") if profile else None,
            "similar_to": profile.get("similar_to", profile.get("similar_titles", []))[:5] if profile else [],
            "rating": profile.get("rating") if profile else None,
            "source": profile.get("source", "unknown") if profile else None,
        })

    cache.close()
    return {"profiles": profiles, "total": total, "category": category}


@router.get("/mapping-coverage")
async def check_mapping_coverage(
    user: User = Depends(get_current_user),
):
    """Check how many Sonarr anime series are covered by the anime-lists mapping."""
    from src.services.anime_mapping import get_anime_mapping
    from src.config import settings
    import httpx as _httpx

    mapping = await get_anime_mapping()

    if not settings.SONARR_URL or not settings.SONARR_API_KEY:
        return {"error": "Sonarr not configured"}

    try:
        async with _httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{settings.SONARR_URL.rstrip('/')}/api/v3/series",
                headers={"X-Api-Key": settings.SONARR_API_KEY})
        if r.status_code != 200:
            return {"error": f"Sonarr HTTP {r.status_code}"}
        series = r.json()
    except Exception as e:
        return {"error": str(e)}

    anime_series = [s for s in series if
                    s.get("seriesType") == "anime" or "Anime" in s.get("genres", [])]

    covered = []
    missing = []

    for s in anime_series:
        tvdb_id = s.get("tvdbId")
        if tvdb_id and tvdb_id in mapping.tvdb_to_anidb:
            anidb_id = mapping.tvdb_to_anidb[tvdb_id]
            anilist_id = mapping.anidb_to_anilist.get(anidb_id)
            covered.append({
                "title": s["title"],
                "tvdb_id": tvdb_id,
                "anidb_id": anidb_id,
                "anilist_id": anilist_id,
                "anilist_resolved": anilist_id is not None,
            })
        else:
            missing.append({
                "title": s["title"],
                "tvdb_id": tvdb_id,
                "reason": "not in anime-lists" if tvdb_id else "no tvdbId",
            })

    return {
        "total_anime": len(anime_series),
        "covered": len(covered),
        "missing": len(missing),
        "coverage_pct": round(100 * len(covered) / max(len(anime_series), 1)),
        "anilist_resolved": sum(1 for c in covered if c["anilist_resolved"]),
        "covered_sample": covered[:10],
        "missing_sample": missing[:10],
    }
