"""
Curatarr 1.0 - Enrichment Router

Category-specific metadata enrichment with progress tracking.
"""

import asyncio
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.config import settings
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
    source: str = "watch_history"  # watch_history / arr / both / (legacy: all)
    limit: Optional[int] = None    # None = no limit
    force: bool = False            # True = re-enrich even if already done (clears cache first)


@router.get("/status")
async def enrichment_status(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    quick: bool = False,
):
    """Return enrichment progress per category. quick=true skips live ARR API calls."""
    from sqlalchemy import func as _func

    result = {}
    for cat in CATEGORIES:
        # canonical_col: the WatchHistoryEntry column that maps to EnrichmentStatus.title
        if cat in ("show", "anime", "music"):
            # series/artist-level: canonical = series_title (artist name for music)
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
            # Subquery: canonical titles this user has in their history
            user_titles_sq = db.query(
                _func.distinct(WatchHistoryEntry.series_title)
            ).filter(
                WatchHistoryEntry.user_id == user.id,
                WatchHistoryEntry.media_type == cat,
                WatchHistoryEntry.series_title != None,
            ).scalar_subquery()
        else:
            # movie: canonical = title
            #
            # Pass 91a: ``total_unique`` was ``count(*)`` here (= row count)
            # while the show/anime/music branch above uses
            # ``count(distinct series_title)``. For a user who re-watched
            # movies, the row count overcounted unique titles and the
            # downstream ``not_enriched = total_unique - enriched`` math
            # became nonsense (37 plays - 31 enriched distinct titles =
            # "6 pending" even though only 1 distinct title was actually
            # unenriched). Aligned with the other branches: distinct title
            # count for ``total_unique``, raw row count kept separately
            # in ``total_raw`` for the "X plays tracked" display.
            total_unique = db.query(
                _func.count(_func.distinct(WatchHistoryEntry.title))
            ).filter(
                WatchHistoryEntry.user_id == user.id,
                WatchHistoryEntry.media_type == cat,
            ).scalar() or 0
            total_raw = db.query(WatchHistoryEntry).filter(
                WatchHistoryEntry.user_id == user.id,
                WatchHistoryEntry.media_type == cat,
            ).count()
            user_titles_sq = db.query(
                _func.distinct(WatchHistoryEntry.title)
            ).filter(
                WatchHistoryEntry.user_id == user.id,
                WatchHistoryEntry.media_type == cat,
            ).scalar_subquery()

        # Count only titles this user actually has in their watch history
        enriched = db.query(
            _func.count(_func.distinct(EnrichmentStatus.title))
        ).filter(
            EnrichmentStatus.media_category == cat,
            EnrichmentStatus.enriched == True,
            EnrichmentStatus.title.in_(user_titles_sq),
        ).scalar() or 0

        vector_ready = db.query(
            _func.count(_func.distinct(EnrichmentStatus.title))
        ).filter(
            EnrichmentStatus.media_category == cat,
            EnrichmentStatus.vector_ready == True,
            EnrichmentStatus.title.in_(user_titles_sq),
        ).scalar() or 0

        result[cat] = {
            "total_in_history": total_raw,
            "total_unique": total_unique,
            "enriched": enriched,
            "not_enriched": max(0, total_unique - enriched),
            "vector_ready": vector_ready,
            "pct": round(100 * enriched / max(total_unique, 1)),
            "unit": "series" if cat in ("show","anime") else "artists" if cat=="music" else "items",
        }

    if quick:
        import json as _json
        from src.services.app_state import get_state as _gs
        cached_raw = _gs("arr_counts_cache")
        arr_counts = _json.loads(cached_raw) if cached_raw else {}
    else:
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
    Results are cached in AppState (DB) -- refreshed every 5 minutes or on demand.
    """
    import json as _json
    import httpx as _httpx
    from src.config import settings
    from src.services.app_state import get_state, set_state
    from datetime import datetime, timedelta

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

    prev_cached = {}
    if cached_raw:
        try:
            prev_cached = _json.loads(cached_raw)
        except Exception:
            pass

    from src.database.connection import get_db_session
    from src.database.models import ArrEnrichmentStatus

    # Pass 65: report real per-service ChromaDB embedding coverage.
    #
    # History of this metric:
    #  - Originally: ``cache_key LIKE 'enriched:%:{svc}:%'`` against
    #    api_cache — doubly broken (no ``v2:`` prefix → matched zero
    #    current keys, AND counted profiles not vectors). Pure noise.
    #  - Pass 63: switched to ``EnrichmentStatus.vector_ready`` — correct
    #    data, but the WRONG metric for THIS column. vector_ready is the
    #    watch-history taste-vector coverage (show/artist granularity:
    #    "21 of the 21 series you've watched"). Sitting next to "Enriched"
    #    (a whole-library number) it looked broken — comparing a
    #    watched-things count to a library count.
    #  - Pass 65 (now): the column should answer "is my enriched library
    #    actually vectorised?" — that's the ChromaDB embedding count, one
    #    doc per library item, keyed "{service}:{arr_id}". It tracks
    #    "Enriched" closely (e.g. radarr ~5488 vs ~5403) because both are
    #    per-library-item.
    def _count_vectors(svc: str) -> int:
        try:
            from src.vector_store.chromadb_wrapper import get_chroma_db
            return get_chroma_db().count_by_id_prefix(f"{svc}:")
        except Exception:
            return 0

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
                    "vector_count": _count_vectors("radarr"),
                    "pct": round(100 * enriched / max(downloaded, 1)),
                    "stale": False,
                }
            else:
                raise Exception(f"HTTP {r.status_code}")
        except Exception as e:
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
                    "vector_count": _count_vectors("sonarr"),
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
                total_albums = sum(
                    a.get("statistics", {}).get("albumCount", 0) for a in artists
                )
                # Pass 91c: Lidarr always showed 0% enriched because
                # ``ArrEnrichmentStatus`` never gets music rows — the
                # music pipeline (in-app job_music_pipeline + standalone
                # music_enricher.py) writes to the general
                # ``EnrichmentStatus`` table instead. To get a meaningful
                # coverage number, intersect Lidarr's artistName set with
                # the music titles that DO have enrichment data.
                # Case-insensitive match because Lidarr stores artistName
                # exactly as MB/the user typed it, but the music enricher
                # normalises through different paths (e.g. the
                # ``ext-script`` seed lowercases).
                from src.database.models import EnrichmentStatus as _ES
                with get_db_session() as db:
                    enriched_music_titles = {
                        (t or "").lower()
                        for (t,) in db.query(_ES.title)
                        .filter(
                            _ES.media_category == "music",
                            _ES.enriched == True,
                        ).all()
                    }
                    enriched_albums = db.query(ArrEnrichmentStatus).filter(
                        ArrEnrichmentStatus.service == "lidarr",
                        ArrEnrichmentStatus.enriched == True,
                        ArrEnrichmentStatus.category == "album",
                    ).count()
                lidarr_names = {
                    (a.get("artistName") or "").lower() for a in artists
                }
                enriched_artists = len(lidarr_names & enriched_music_titles)
                counts["lidarr"] = {
                    "total": len(artists),
                    "total_albums": total_albums,
                    "downloaded": downloaded,
                    "monitored": sum(1 for a in artists if a.get("monitored")),
                    "enriched": enriched_artists,
                    "enriched_albums": enriched_albums,
                    "vector_count": _count_vectors("lidarr"),
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

    import json as _json2
    from src.services.app_state import set_state as _set
    _set("arr_counts_cache", _json2.dumps(counts))
    _set("arr_counts_ts", datetime.utcnow().isoformat())
    return counts



@router.post("/arr-refresh")
async def refresh_arr_counts(user: User = Depends(get_current_user)):
    """Force-refresh ARR library counts (bypass 5-min cache)."""
    from src.services.app_state import set_state
    set_state("arr_counts_ts", "")
    counts = await _get_arr_counts()
    return {"arr": counts}


@router.post("/arr-pre-enrich")
async def start_arr_pre_enrich(
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
):
    """
    Manually trigger the nightly ARR pre-enrichment batch.

    Enriches up to ARR_PRE_ENRICH_BATCH (default 80) unenriched ARR items so
    the deletion-proposal engine has rating and genre data before the next sync.
    Returns immediately — enrichment runs in the background.
    """
    # Atomic compare-and-set so two concurrent POSTs can't both pass the
    # check before either flips the flag. _run_enrichment claims the lock;
    # if it's already held we 409.
    from src.services.app_state import acquire_state_lock
    if not acquire_state_lock("enrichment_running"):
        raise HTTPException(status_code=409, detail="Enrichment already running")

    from src.config import settings as _s
    batch = int(getattr(_s, "ARR_PRE_ENRICH_BATCH", 80))
    background_tasks.add_task(
        _run_enrichment, user.id,
        ["movie", "show", "anime", "music"],
        "arr", batch, False,
    )
    return {"status": "started", "batch": batch, "source": "arr"}


@router.post("/start")
async def start_enrichment(
    req: EnrichRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
):
    """Start enrichment for specified categories and sources."""
    # Atomic compare-and-set: two concurrent /start posts can't race past
    # the check. The flag is released in _run_enrichment's finally-block.
    from src.services.app_state import acquire_state_lock
    if not acquire_state_lock("enrichment_running"):
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
    background_tasks: BackgroundTasks,
    categories: Optional[List[str]] = Query(None),
    user: User = Depends(get_current_user),
):
    """Recompute taste vectors from enriched data.

    Defaulting ``background_tasks`` to ``None`` would prevent FastAPI from
    injecting the real BackgroundTasks instance (the dep is only injected
    when the param has no default), causing ``add_task`` to AttributeError
    on every call.
    """
    cats = list(categories) if categories else None
    if cats:
        valid = {"music", "movie", "show", "anime"}
        cats = [c for c in cats if c in valid]
    background_tasks.add_task(_run_taste_computation, user.id, cats)
    return {"status": "started"}


# ── Pass 55: audit & requeue incomplete enrichments ─────────────────────────

def _enrichment_incomplete_reason(profile: dict) -> Optional[str]:
    """Return a short reason code if a cached enrichment profile is missing
    key metadata, else None.

    Drives the audit-requeue endpoint: lets the user fix bad profiles
    (the Pass-54 "zero rating" class, not_found sentinels) without a
    full-library force-clear. Rule-based fallbacks are deliberately NOT
    flagged here — the normal _run_enrichment(force=False) already keeps
    retrying those for an LLM upgrade (see the Step-5 pre-filter).
    """
    if not isinstance(profile, dict):
        return "malformed"
    if profile.get("source") == "not_found":
        # Wasn't matched in any API. Worth retrying especially after the
        # Pass 51/52 ID-resolution work — previously-unfindable titles
        # may resolve now.
        return "not_found"
    rating = profile.get("rating")
    try:
        if rating is None or float(rating) <= 0:
            # Pass 54 context: 0 is the "no rating data" sentinel, not a
            # 0/10 verdict. Re-enrich to try for a real score.
            return "zero_rating"
    except (TypeError, ValueError):
        return "zero_rating"
    return None


async def _audit_enrichments(dry_run: bool) -> dict:
    """Scan the enrichment cache for incomplete profiles and (unless
    dry_run) requeue them.

    Requeue = delete the stale ``api_cache`` row AND flip the matching
    ``EnrichmentStatus`` / ``ArrEnrichmentStatus`` rows back to
    ``enriched=False``. Both are needed: the Step-5 pre-filter skips by
    EnrichmentStatus, the producer skips by cache presence. The next
    ``_run_enrichment(force=False)`` then picks the items up — no
    full-library force-clear.
    """
    from src.cache.metadata_cache import MetadataCache, _CACHE_VERSION
    from src.database.models import ArrEnrichmentStatus
    import json as _json

    prefix = f"{_CACHE_VERSION}:enriched:"
    scanned = 0
    by_reason: dict[str, int] = {}
    # each hit: (cache_key, plex_rating_key, title, category, reason)
    hits: list[tuple] = []

    mc = MetadataCache()
    try:
        rows = mc.conn.execute(
            "SELECT cache_key, response FROM api_cache WHERE cache_key LIKE ?",
            (prefix + "%",),
        ).fetchall()
        for row in rows:
            scanned += 1
            cache_key = row["cache_key"]
            try:
                profile = _json.loads(row["response"])
            except Exception:
                profile = None
            reason = _enrichment_incomplete_reason(profile or {})
            if not reason:
                continue
            by_reason[reason] = by_reason.get(reason, 0) + 1
            # Cache key shape: "{_CACHE_VERSION}:enriched:{category}:{id_key}"
            # — category is the 3rd colon-segment. id_key may itself
            # contain colons, hence the maxsplit=3.
            parts = cache_key.split(":", 3)
            category = parts[2] if len(parts) > 2 else (
                (profile or {}).get("media_type")
            )
            hits.append((
                cache_key,
                (profile or {}).get("plex_rating_key"),
                (profile or {}).get("title"),
                category,
                reason,
            ))

        if not dry_run and hits:
            for cache_key, *_ in hits:
                mc.conn.execute("DELETE FROM api_cache WHERE cache_key = ?", (cache_key,))
            mc.conn.commit()
    finally:
        mc.close()

    requeued = 0
    if not dry_run and hits:
        with get_db_session() as db:
            for _ck, prk, title, category, _reason in hits:
                if prk:
                    # Precise path — plex_rating_key is EnrichmentStatus's
                    # UNIQUE column, immune to AniList title normalisation.
                    es = db.query(EnrichmentStatus).filter(
                        EnrichmentStatus.plex_rating_key == prk,
                    ).first()
                    if es:
                        es.enriched = False
                        es.enriched_at = None
                        es.error = None
                        requeued += 1
                    # plex_rating_key shape from _collect_arr_items is
                    # "{service}:{arr_id}" — flip the ArrEnrichmentStatus row too.
                    svc, _, aid = (prk or "").partition(":")
                    if aid.isdigit():
                        arr_s = db.query(ArrEnrichmentStatus).filter(
                            ArrEnrichmentStatus.service == svc,
                            ArrEnrichmentStatus.arr_id == int(aid),
                        ).first()
                        if arr_s:
                            arr_s.enriched = False
                            arr_s.enriched_at = None
                elif title and category:
                    # Legacy / chat-cascade cache entry with no
                    # plex_rating_key — best-effort title+category match.
                    matched = db.query(EnrichmentStatus).filter(
                        EnrichmentStatus.title == title,
                        EnrichmentStatus.media_category == category,
                    ).all()
                    for es in matched:
                        es.enriched = False
                        es.enriched_at = None
                        es.error = None
                        requeued += 1
            db.commit()

    logger.info(
        "[audit-requeue] scanned=%d incomplete=%d requeued=%d dry_run=%s by_reason=%s",
        scanned, len(hits), requeued, dry_run, by_reason,
    )
    return {
        "scanned":    scanned,
        "incomplete": len(hits),
        "requeued":   requeued,
        "dry_run":    dry_run,
        "by_reason":  by_reason,
    }


@router.post("/audit-requeue")
async def audit_requeue_enrichments(
    dry_run: bool = True,
    user: User = Depends(get_current_user),
):
    """Scan the enrichment cache for profiles with missing metadata
    (zero/absent rating, not_found sentinel) and requeue just those —
    no full-library force-clear.

    ``dry_run=true`` (default): scan + count only, change nothing — the
    frontend uses this to show a breakdown before the user commits.
    ``dry_run=false``: delete the stale cache rows + flip
    EnrichmentStatus / ArrEnrichmentStatus back to ``enriched=False``.
    The next enrichment run (scheduled or manual ``/start``) re-fetches them.
    """
    return await _audit_enrichments(dry_run=dry_run)


# ---- BACKGROUND TASKS -------------------------------------------------------


async def _collect_arr_items(categories: list) -> list:
    """Fetch downloaded items from configured ARR services."""
    import httpx as _httpx
    # Pass 99-fu7 (#29): reuse the diagnostic helper from library.py so an
    # aiohttp ClientConnectorError (empty str(), non-empty repr()) doesn't
    # surface as ``"ARR collect (radarr) failed: "`` with nothing after
    # the colon. The pre-fu7 logs gave zero diagnostic value when all 3
    # services failed simultaneously — could've been DNS, timeout, 401,
    # 500, the API key not being loaded, anything. Now we get the class
    # name at minimum.
    from src.routers.library import _format_arr_error
    items = []

    # Pass 99-fu7 (#29 follow-on): 60s timeout, not 10s. The live ARR ping
    # that surfaced this showed radarr (6,996 movies) + lidarr (5,206
    # artists) both ReadTimeout at 10s — the /api/v3/movie + /api/v1/artist
    # endpoints return the FULL library with per-item metadata (15-30 MB
    # JSON) which radarr/lidarr can't serialise inside 10s when under load.
    # Sonarr (3,604 series) squeaked under the old limit, which is why it
    # sometimes worked and sometimes didn't. These are background bulk
    # fetches, so a generous timeout is correct.
    _ARR_TIMEOUT = 60
    if "movie" in categories and settings.RADARR_URL and settings.RADARR_API_KEY:
        try:
            async with _httpx.AsyncClient(timeout=_ARR_TIMEOUT) as client:
                r = await client.get(
                    f"{settings.effective_radarr_url}/api/v3/movie",
                    headers={"X-Api-Key": settings.RADARR_API_KEY},
                )
            if r.status_code == 200:
                for m in r.json():
                    if not m.get("hasFile"):
                        continue
                    items.append({
                        "plex_rating_key": f"radarr:{m['id']}",
                        "title": m.get("title", ""),
                        "series_title": None,
                        "media_type": "movie",
                        "tmdb_id": m.get("tmdbId"),
                        "tvdb_id": None,
                        "imdb_id": m.get("imdbId"),
                        "arr_id": m["id"],
                        "service": "radarr",
                    })
        except Exception as e:
            logger.warning("ARR collect (radarr) failed: %s", _format_arr_error("radarr", e))

    if any(c in categories for c in ("show", "anime")) and settings.SONARR_URL and settings.SONARR_API_KEY:
        try:
            async with _httpx.AsyncClient(timeout=_ARR_TIMEOUT) as client:
                r = await client.get(
                    f"{settings.effective_sonarr_url}/api/v3/series",
                    headers={"X-Api-Key": settings.SONARR_API_KEY},
                )
            if r.status_code == 200:
                from src.services.arr_client import classify_sonarr_category
                for s in r.json():
                    # Pass 59: ``or {}`` — Sonarr returns "statistics": null
                    # for file-less series; the .get default doesn't cover
                    # a present-null value (same class as the Pass-58 fix).
                    if not (s.get("statistics") or {}).get("episodeFileCount", 0):
                        continue
                    series_type = s.get("seriesType", "standard")
                    # Pass 59: shared classifier — was ``"anime" if
                    # seriesType == "anime" else "show"`` here, but the
                    # deletion-proposal pipeline ALSO checked the genre
                    # tag. The drift cached the same series under two
                    # different category keys. One source of truth now.
                    cat = classify_sonarr_category(s)
                    if cat not in categories:
                        continue
                    items.append({
                        "plex_rating_key": f"sonarr:{s['id']}",
                        "title": s.get("title", ""),
                        "series_title": s.get("title", ""),
                        "media_type": cat,
                        "tmdb_id": s.get("tmdbId"),
                        "tvdb_id": s.get("tvdbId"),
                        "imdb_id": None,
                        "arr_id": s["id"],
                        "service": "sonarr",
                        "sonarr_series_type": series_type,
                    })
        except Exception as e:
            logger.warning("ARR collect (sonarr) failed: %s", _format_arr_error("sonarr", e))

    if "music" in categories and settings.LIDARR_URL and settings.LIDARR_API_KEY:
        try:
            async with _httpx.AsyncClient(timeout=_ARR_TIMEOUT) as client:
                r = await client.get(
                    f"{settings.effective_lidarr_url}/api/v1/artist",
                    headers={"X-Api-Key": settings.LIDARR_API_KEY},
                )
            if r.status_code == 200:
                for a in r.json():
                    items.append({
                        "plex_rating_key": f"lidarr:{a['id']}",
                        "title": a.get("artistName", ""),
                        "series_title": a.get("artistName", ""),
                        "media_type": "music",
                        "tmdb_id": None,
                        "tvdb_id": None,
                        "imdb_id": None,
                        "arr_id": a["id"],
                        "service": "lidarr",
                    })
        except Exception as e:
            logger.warning("ARR collect (lidarr) failed: %s", _format_arr_error("lidarr", e))

    return items


_BATCH_SIZE = 10       # items per category per rotation slice
# Pass 99-fu3: queue depth raised from 30 → 500 so the parallel producer
# can stay well ahead of the LLM consumer. The consumer at ~5 s/item runs
# at ~12 items/min; with 8 producer workers fetching in parallel the
# producer can fill the queue inside a few seconds and then idle on
# queue.put backpressure. A 500-deep buffer means a brief LLM stall
# (model reload, etc.) doesn't make the producer waste time waiting.
_PREFETCH_DEPTH = 500
# Number of parallel producer workers. Each one independently fetches an
# item via fetch_and_prepare_raw, which internally respects per-service
# concurrency caps (TMDB 16, OMDb 4, AniList 1 via lock, Jikan 2, MB 1
# via existing semaphore). 8 workers × per-service caps means no service
# gets hammered while the slowest path (AniList sequential, MB 1 req/sec)
# keeps the queue fed for the LLM.
_PRODUCER_WORKERS = 8


def _interleave_by_cat(by_cat: dict, batch_size: int = _BATCH_SIZE) -> list:
    """Round-robin items across categories in batches so no category starves."""
    cats = [c for c in by_cat if by_cat[c]]
    offsets = {c: 0 for c in cats}
    result = []
    while True:
        added = 0
        for c in cats:
            chunk = by_cat[c][offsets[c]:offsets[c] + batch_size]
            result.extend(chunk)
            offsets[c] += len(chunk)
            added += len(chunk)
        if added == 0:
            break
    return result


def _is_rule_based(profile: dict) -> bool:
    """Return True when the profile was produced by the rule-based fallback or not-found sentinel (no LLM)."""
    src = profile.get("source") or ""
    return "rule_based" in src or src == "not_found"


async def _write_enrichment_db(item: dict, profile, cat: str):
    """Write EnrichmentStatus + ArrEnrichmentStatus + propagate series cache."""
    from src.database.models import ArrEnrichmentStatus
    canonical_title = item.get("series_title") or item["title"]

    for _attempt in range(5):
        try:
            with get_db_session() as db:
                # Pass 29: look up by plex_rating_key — that's the UNIQUE
                # column on the table. The previous (title, media_category)
                # lookup missed rows whose plex_rating_key was already taken
                # but whose stored title had drifted (arr renamed the series,
                # or category changed) → db.add() then crashed on
                #   UNIQUE constraint failed: enrichment_status.plex_rating_key
                # Looking up by the actual unique key fixes that AND lets us
                # update the title/category in place when they've shifted.
                status = db.query(EnrichmentStatus).filter(
                    EnrichmentStatus.plex_rating_key == item["plex_rating_key"],
                ).first()
                if not status:
                    status = EnrichmentStatus(
                        plex_rating_key=item["plex_rating_key"],
                        title=canonical_title,
                        media_category=cat,
                    )
                    db.add(status)
                else:
                    # Existing row — refresh descriptive fields in case the
                    # arr renamed the series or the category was re-derived.
                    status.title          = canonical_title
                    status.media_category = cat
                is_not_found = profile is not None and profile.get("source") == "not_found"
                llm_ok       = profile is not None and not _is_rule_based(profile)
                has_data     = profile is not None
                # enriched=True  + error=None           → LLM done; never retried
                # enriched=True  + error="rule_based…"  → has data; retried for upgrade
                # enriched=True  + error="Not found…"   → no external data; retried after cache TTL
                # enriched=False                         → processing failed; always retried
                status.enriched    = has_data
                status.enriched_at = datetime.utcnow() if has_data else None
                status.error       = (None              if llm_ok
                                      else "Not found in metadata APIs" if is_not_found
                                      else "rule_based — LLM upgrade pending"
                                      if _is_rule_based(profile) else "Processing failed")
                db.commit()
            break
        except Exception as _e:
            if "database is locked" in str(_e) and _attempt < 4:
                await asyncio.sleep(1.0 * (_attempt + 1))
            else:
                raise

    if profile and item.get("arr_id") and item.get("service"):
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
                    title=canonical_title,
                    tmdb_id=item.get("tmdb_id"),
                    tvdb_id=item.get("tvdb_id"),
                )
                db.add(arr_s)
            arr_llm_ok = not _is_rule_based(profile)
            arr_s.enriched    = arr_llm_ok
            arr_s.enriched_at = datetime.utcnow() if arr_llm_ok else None
            db.commit()
    elif profile and canonical_title:
        try:
            with get_db_session() as db:
                arr_match = db.query(ArrEnrichmentStatus).filter(
                    ArrEnrichmentStatus.title == canonical_title,
                    ArrEnrichmentStatus.category == cat,
                    ArrEnrichmentStatus.enriched == False,
                ).first()
                if arr_match:
                    arr_llm_ok = not _is_rule_based(profile)
                    arr_match.enriched    = arr_llm_ok
                    arr_match.enriched_at = datetime.utcnow() if arr_llm_ok else None
                    db.commit()
        except Exception:
            pass

    if not profile:
        return

    if profile:
        from src.cache.metadata_cache import MetadataCache as _MC
        is_not_found = profile.get("source") == "not_found"
        _c = _MC()
        # not_found sentinel: 3-day TTL — enough to block hammering, short enough
        #   for a title that may appear on the API after a new release.
        # Rule-based fallbacks: 1-day TTL so they are retried next run.
        # LLM-enriched profiles: 90 days (stable data).
        cache_days = 3 if is_not_found else (1 if _is_rule_based(profile) else 90)
        _c.set_cache(f"enriched:{cat}:{item['plex_rating_key']}", profile, days=cache_days)
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
                    _c.set_cache(f"enriched:{cat}:{rpid}", profile, days=cache_days)
            # Only mark related episodes as enriched when LLM succeeded
            if not _is_rule_based(profile):
                with get_db_session() as _db:
                    _db.query(EnrichmentStatus).filter(
                        EnrichmentStatus.media_category == cat,
                        EnrichmentStatus.plex_rating_key.in_(related_pids),
                    ).update({"enriched": True, "enriched_at": datetime.utcnow()},
                             synchronize_session=False)
                    _db.commit()
        elif cat == "music" and item.get("series_title"):
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
                    _c.set_cache(f"enriched:music:{rpid}", profile, days=cache_days)
        _c.close()


async def _run_enrichment(user_id: int, categories: list, source: str, limit: Optional[int], force: bool = False):
    """Single-worker producer-consumer enrichment pipeline.

    Producer: fetches API metadata (TMDB/AniList/MusicBrainz) concurrently.
    Consumer: single LLM worker processes items sequentially; yields to curator calls.
    Items rotate across categories in batches so no category starves.
    Crash-safe: EnrichmentStatus.enriched is only set True after the LLM succeeds.
    Rule-based fallbacks leave enriched=False and cache for 1 day — retried next run.
    """
    # Normalize legacy source value
    if source == "all":
        source = "both"

    if force:
        logger.info("Force re-enrichment requested -- clearing cache for %s (source=%s)", categories, source)
        try:
            from src.cache.metadata_cache import MetadataCache
            from src.database.models import ArrEnrichmentStatus
            # One sqlite3 handle (MetadataCache.conn) instead of two pointed
            # at the same file — concurrent writers used to deadlock here.
            _mc = MetadataCache()
            try:
                for cat in categories:
                    if source in ("watch_history", "both"):
                        deleted = _mc.conn.execute(
                            "DELETE FROM api_cache WHERE cache_key LIKE ?",
                            (f"enriched:{cat}:%",)
                        ).rowcount
                        logger.info("Cleared %d cache entries for %s", deleted, cat)
                    elif source == "arr":
                        # Clear only ARR-sourced cache keys (radarr/sonarr/lidarr prefixes).
                        deleted = 0
                        for svc in ("radarr", "sonarr", "lidarr"):
                            deleted += _mc.conn.execute(
                                "DELETE FROM api_cache WHERE cache_key LIKE ?",
                                (f"enriched:{cat}:{svc}:%",)
                            ).rowcount
                        logger.info("Cleared %d ARR cache entries for %s", deleted, cat)

                # Pass 46 (Bug 4 / B7): also wipe per-item embedding caches.
                # ``emb:{pid}`` and ``emb_ts:{pid}`` live with a 90-day TTL
                # and gate re-embedding by ts comparison; force-re-enrich
                # MUST drop them or the next taste-vector recompute keeps
                # reusing vectors built from the now-deleted old profiles.
                # ``LIKE 'emb:%'`` matches both emb: and emb_ts: in one go.
                # Only run on watch_history / both — emb caches are keyed
                # by plex_rating_key, which arr-only items don't have.
                if source in ("watch_history", "both"):
                    emb_deleted = _mc.conn.execute(
                        "DELETE FROM api_cache WHERE cache_key LIKE ?",
                        (f"%:emb:%",),
                    ).rowcount
                    emb_ts_deleted = _mc.conn.execute(
                        "DELETE FROM api_cache WHERE cache_key LIKE ?",
                        (f"%:emb_ts:%",),
                    ).rowcount
                    logger.info(
                        "Cleared %d emb + %d emb_ts cache rows", emb_deleted, emb_ts_deleted,
                    )

                _mc.conn.commit()
            finally:
                _mc.close()
            with get_db_session() as db:
                if source in ("watch_history", "both"):
                    db.query(EnrichmentStatus).filter(
                        EnrichmentStatus.media_category.in_(categories)
                    ).update({"enriched": False, "error": None, "enriched_at": None},
                             synchronize_session=False)
                if source in ("arr", "both"):
                    db.query(ArrEnrichmentStatus).filter(
                        ArrEnrichmentStatus.category.in_(categories)
                    ).update({"enriched": False, "enriched_at": None},
                             synchronize_session=False)
                db.commit()
        except Exception as e:
            logger.warning("Force-clear failed: %s", e)

    import json as _json
    # The route already acquired the enrichment_running lock atomically via
    # acquire_state_lock(); we don't need to set it again here. The flag
    # is released in the finally-block at the end of this function.
    set_state("enrichment_progress", "0")
    # Persist settings so a startup-resume uses the exact same parameters
    set_state("enrichment_last_settings", _json.dumps({
        "categories": categories,
        "source": source,
        "limit": limit,
    }))
    set_state("enrichment_interrupted", "1")
    setup_task = task_monitor.create(
        name=f"Enrichment setup: {', '.join(categories)}",
        category="enrichment",
    )
    task_monitor.start(setup_task)

    try:
        # Step 1: collect from watch history (skip when arr-only)
        items = []
        if source in ("watch_history", "both"):
            with get_db_session() as db:
                for cat in categories:
                    for e in db.query(WatchHistoryEntry).filter(
                        WatchHistoryEntry.user_id == user_id,
                        WatchHistoryEntry.media_type == cat,
                    ).all():
                        items.append({
                            "plex_rating_key": e.plex_item_id,
                            "title": e.title,
                            "series_title": e.series_title,
                            "media_type": cat,
                            "tmdb_id": e.tmdb_id,
                            "tvdb_id": None,
                            "imdb_id": None,
                            "source": "watch_history",
                        })

        # Step 2: merge ARR items when source includes arr
        if source in ("arr", "both"):
            task_monitor.update(setup_task, message="Fetching ARR libraries...")
            arr_items = await _collect_arr_items(categories)
            watch_keys = {
                (i.get("series_title") or i["title"]).lower().strip()
                for i in items
            }
            for ai in arr_items:
                key = (ai.get("series_title") or ai["title"]).lower().strip()
                if key and key not in watch_keys:
                    items.append(ai)
                    watch_keys.add(key)
            logger.info("After ARR merge: %d total items", len(items))

        # Step 3: deduplicate
        deduped: list = []
        seen: set = set()
        for item in items:
            cat = item["media_type"]
            if cat in ("show", "anime"):
                st = item.get("series_title")
                if not st:
                    continue
                key = f"{cat}:{st.lower().strip()}"
                item = dict(item)
                # Note: previously we nullified ``tmdb_id > 200000`` here
                # as a defence against episode-level TMDB IDs leaking out
                # of older sync paths. Current plex_sync writes series-level
                # IDs (via grandparent_key) — and TMDB series IDs legitimately
                # exceed 1 M, so the magnitude heuristic now corrupts good
                # data. Removed; MediaIdentity remains the canonical override.
            elif cat == "music":
                st = item.get("series_title")
                if not st:
                    continue
                key = f"music:{st.lower().strip()}"
            else:
                key = f"movie:{item['title'].lower().strip()}"

            if key not in seen:
                seen.add(key)
                deduped.append(item)

        logger.info("Deduped %d -> %d unique items across %s", len(items), len(deduped), categories)
        items = deduped

        # Step 4: enrich IDs from MediaIdentity (watch history items only)
        wh_keys = [i["plex_rating_key"] for i in items if not str(i["plex_rating_key"]).startswith(("radarr:", "sonarr:", "lidarr:"))]
        if wh_keys:
            from src.database.models import MediaIdentity
            with get_db_session() as db:
                id_map = {
                    mi.plex_rating_key: {
                        "tvdb_id": mi.tvdb_id, "imdb_id": mi.imdb_id,
                        "anilist_id": mi.anilist_id, "tmdb_id": mi.tmdb_id,
                    }
                    for mi in db.query(MediaIdentity).filter(
                        MediaIdentity.plex_rating_key.in_(wh_keys)
                    ).all()
                }
            for item in items:
                ids = id_map.get(item["plex_rating_key"], {})
                if ids.get("tvdb_id"):    item["tvdb_id"]    = ids["tvdb_id"]
                if ids.get("imdb_id"):    item["imdb_id"]    = ids["imdb_id"]
                if ids.get("anilist_id"): item["anilist_id"] = ids["anilist_id"]
                # MediaIdentity is the canonical resolved-IDs table — when it
                # has a tmdb_id, prefer it over whatever the upstream
                # (ARR / Plex Guid) gave us. (Previously gated on a magnitude
                # heuristic ``> 200000`` that no longer holds for real TMDB IDs.)
                if ids.get("tmdb_id"):
                    item["tmdb_id"] = ids["tmdb_id"]

        # Step 5: pre-filter already-enriched (single bulk DB query)
        # Only skip items that are FULLY LLM-enriched (enriched=True AND error IS NULL).
        # Rule-based items (enriched=True, error set) are kept for LLM upgrade retries.
        # Items with no data (enriched=False) are always retried.
        if not force:
            with get_db_session() as db:
                enriched_set = {
                    (r.title, r.media_category)
                    for r in db.query(EnrichmentStatus.title, EnrichmentStatus.media_category)
                    .filter(
                        EnrichmentStatus.enriched == True,
                        EnrichmentStatus.error.is_(None),
                    ).all()
                }
            before = len(items)
            items = [
                i for i in items
                if ((i.get("series_title") or i["title"]), i["media_type"]) not in enriched_set
            ]
            logger.info("Pre-filter: %d -> %d items (skipped %d LLM-done, kept rule-based/failed)",
                        before, len(items), before - len(items))

        # Pass 86: priority sort — fresh imports (never enriched) go to the
        # front of the queue, then TTL-refresh re-queues by oldest-first.
        # The scheduler's TTL refresh PRESERVES the old enriched_at on a
        # re-queue (scheduler.py::job_enrichment_ttl_refresh, Pass 86), so
        # ``enriched_at IS NULL`` is now a reliable "never been enriched"
        # marker — fresh items get LLM-polished within the same batch as
        # they arrive instead of waiting behind the background refresh
        # cycle that would otherwise consume the daily budget first.
        if items:
            with get_db_session() as db:
                ts_map = {
                    (r.title, r.media_category): r.enriched_at
                    for r in db.query(
                        EnrichmentStatus.title,
                        EnrichmentStatus.media_category,
                        EnrichmentStatus.enriched_at,
                    ).all()
                }

            def _priority_key(it):
                key = ((it.get("series_title") or it["title"]), it["media_type"])
                ts = ts_map.get(key)
                # First component: 0 = never enriched (top priority),
                # 1 = has a previous enriched_at (TTL-refresh path).
                # Second component: timestamp for ascending sort within
                # group 1; ignored for group 0 (all equal).
                if ts is None:
                    return (0, 0.0)
                return (1, ts.timestamp())

            items.sort(key=_priority_key)
            fresh_count = sum(1 for it in items
                              if ts_map.get(((it.get("series_title") or it["title"]),
                                             it["media_type"])) is None)
            logger.info("Priority sort: %d fresh-import items moved to front of %d total",
                        fresh_count, len(items))

        if limit:
            items = items[:limit]

        total = len(items)
        logger.info("Enrichment start: %d items across %s (source=%s)", total, categories, source)

        if total == 0:
            task_monitor.done(setup_task, "Nothing to enrich -- all items already have profiles")
            set_state("last_enrichment_at", datetime.utcnow().isoformat())
            await _run_taste_computation(user_id, categories)
            return

        # Step 6: build interleaved order and start pipeline
        by_cat = {cat: [i for i in items if i["media_type"] == cat] for cat in categories}
        active_cats = [cat for cat in categories if by_cat.get(cat)]
        interleaved = _interleave_by_cat(by_cat)

        task_monitor.done(setup_task,
            f"Starting pipeline: {total} items across {active_cats} "
            f"({_BATCH_SIZE}-item category rotation)")

        main_task = task_monitor.create(
            name=f"Enrichment: {', '.join(active_cats)}",
            category="enrichment",
            total=total,
        )
        task_monitor.start(main_task)

        # Pass 99-fu12: per-category output queues + round-robin consumer.
        # A single FIFO queue let the music lane (instant raw-cache hits)
        # monopolise it — movie/show/anime queued behind a huge music backlog
        # and starved for hours, and the fast lanes' producers blocked on
        # queue.put (so OMDb/TMDB went idle). Now each category has its own
        # queue; the consumer pulls up to _CONSUME_BATCH per category in turn,
        # skipping empties → every category gets a fair turn the moment it has
        # items, and the fast producers keep fetching at full rate.
        _CONSUME_BATCH = 10
        cat_queues: dict[str, asyncio.Queue] = {
            c: asyncio.Queue(maxsize=_PREFETCH_DEPTH) for c in active_cats
        }
        lanes_done: set = set()

        from src.services.media_enricher import fetch_and_prepare_raw, process_and_save
        # Pass 60: the bulk consumer now waits on the combined gate — it
        # yields to BOTH the curator (chat) and a user-triggered priority
        # re-enrich, instead of only the curator.
        from src.services.llm_priority import wait_for_enrichment_slot

        # Pass 93: shared progress counter — producer counts not_found
        # sentinels (which bypass the consumer), consumer counts everything
        # that reaches the LLM-polish path. asyncio is single-threaded so
        # the shared int is race-free. Without this, the UI sat at 0% while
        # the producer chewed through hundreds of items writing not_found
        # rows directly — invisible to the consumer-only progress update.
        processed_total = 0
        producer_not_found = 0

        # Pass 99: sliding-window not_found ratio per category. If the
        # producer writes >50% not_found sentinels in the last
        # ``_RATIO_WINDOW`` items of a given category, abort the run —
        # that's a strong signal that an upstream API is degraded (not
        # that the user genuinely has thousands of un-findable items).
        # The pre-99 silent-fail mode poisoned 5,837 movies in a single
        # hour during a TMDB outage on 2026-05-16; this guard catches
        # the next one early.
        from collections import deque
        from src.services.media_enricher import TMDBTransientError
        _RATIO_WINDOW = 50
        _RATIO_ABORT  = 0.5
        _rolling: dict[str, deque] = {c: deque(maxlen=_RATIO_WINDOW)
                                       for c in CATEGORIES}
        # Per-category transient-error tally — used to back off harder
        # when one upstream is degraded but the others are fine.
        _transient_streak: dict[str, int] = {c: 0 for c in CATEGORIES}
        _producer_abort_reason: dict[str, str] = {}

        async def _process_one(pitem: dict) -> None:
            """Per-item logic — same shape as the pre-99-fu3 producer body.

            Pass 99-fu3: factored out so a worker pool can run N copies
            in parallel. Per-service concurrency caps inside
            ``fetch_and_prepare_raw`` (TMDB 16, OMDb 4, AniList 1-lock,
            Jikan 2, MB 1-semaphore) keep us within each API's rate
            limits even with 8 workers in flight. The 50%-abort + 429-
            respect logic is unchanged — rolling-window ratio and per-
            category streak counters work correctly regardless of
            processing order.
            """
            nonlocal processed_total, producer_not_found
            if main_task.status.value == "skipped":
                return
            pcat = pitem["media_type"]
            canonical = pitem.get("series_title") or pitem["title"]
            if pcat in _producer_abort_reason:
                return
            try:
                raw = await fetch_and_prepare_raw(
                    title=canonical,
                    media_type=pcat,
                    tmdb_id=pitem.get("tmdb_id"),
                    anilist_id=pitem.get("anilist_id"),
                    anidb_id=pitem.get("anidb_id"),
                    tvdb_id=pitem.get("tvdb_id"),
                    imdb_id=pitem.get("imdb_id"),
                    plex_rating_key=pitem["plex_rating_key"],
                    sonarr_series_type=pitem.get("sonarr_series_type"),
                )
                if raw is not None and raw.get("_already_enriched"):
                    # Pass 99-fu: cache hit shows the item is already
                    # fully enriched (LLM-polished, or fresh cache).
                    # Reconcile the EnrichmentStatus row to reflect
                    # that — no re-fetch, no LLM polish needed.
                    cached_profile = raw["_cached_profile"]
                    await _write_enrichment_db(pitem, cached_profile, pcat)
                    processed_total += 1
                    task_monitor.update(main_task, processed=processed_total, total=total)
                    _rolling[pcat].append("ok")
                    _transient_streak[pcat] = 0
                elif raw is not None:
                    await cat_queues[pcat].put((pitem, raw))
                    _rolling[pcat].append("ok")
                    _transient_streak[pcat] = 0   # reset on any success
                else:
                    # No API data found for this title (title mismatch, not on TMDB/AniList, etc.)
                    sentinel = {
                        "source": "not_found",
                        "title": canonical,
                        "genres": [], "themes": [], "mood": [],
                        "media_type": pcat,
                    }
                    await _write_enrichment_db(pitem, sentinel, pcat)
                    producer_not_found += 1
                    processed_total += 1
                    task_monitor.update(main_task, processed=processed_total, total=total)
                    _rolling[pcat].append("not_found")

                    if len(_rolling[pcat]) >= _RATIO_WINDOW:
                        nf = sum(1 for x in _rolling[pcat] if x == "not_found")
                        ratio = nf / len(_rolling[pcat])
                        if ratio >= _RATIO_ABORT and pcat not in _producer_abort_reason:
                            reason = (
                                f"{int(ratio*100)}% not_found in last "
                                f"{_RATIO_WINDOW} {pcat} items — upstream "
                                f"API likely degraded; aborting category"
                            )
                            _producer_abort_reason[pcat] = reason
                            logger.warning(
                                "[enrichment][%s] PRODUCER ABORT: %s. "
                                "Remaining items stay at enriched=False; "
                                "next run will retry them.", pcat, reason,
                            )
                            task_monitor.log(
                                main_task, f"⚠ {pcat}: {reason}", level="warn",
                            )
            except TMDBTransientError as te:
                _transient_streak[pcat] += 1
                logger.warning(
                    "[enrichment][%s] TMDB transient (HTTP %d) on '%s' — "
                    "sleeping %.1fs then skipping (streak=%d). Body: %s",
                    pcat, te.status_code, canonical, te.retry_after_s,
                    _transient_streak[pcat], te.body_snippet[:80],
                )
                await asyncio.sleep(te.retry_after_s)
                if _transient_streak[pcat] >= 5 and pcat not in _producer_abort_reason:
                    reason = (
                        f"5 consecutive TMDB transient errors — backing "
                        f"off this category for the rest of the run"
                    )
                    _producer_abort_reason[pcat] = reason
                    logger.warning("[enrichment][%s] PRODUCER ABORT: %s", pcat, reason)
                    task_monitor.log(main_task, f"⚠ {pcat}: {reason}", level="warn")
            except Exception as e:
                logger.warning("Producer error [%s] '%s': %s", pcat, canonical, e)

        async def _producer():
            """Pass 99-fu10: per-category fetch lanes (replaces the single
            mixed worker pool).

            The pre-fu10 pool put ALL items in one inbox and let 8 generic
            workers pull from it. With a 66%-music library that was fatal:
            ~6 of 8 workers grabbed music items and blocked on the
            MusicBrainz ``Semaphore(1)`` (MB enforces ~1 req/sec) while the
            TMDB lanes (movie/show — 16 concurrent allowed) sat idle, so
            throughput collapsed to roughly the music rate (~4/min).

            Now each category gets its OWN worker count, matched to its
            upstream's concurrency cap. A slow lane (music) can no longer
            starve a fast lane (movie/show): the lanes fetch independently
            and all feed the single output ``queue``; the consumer drains
            whatever is ready. ``_process_one``'s per-category abort /
            rolling-window / transient-streak state is order-independent,
            so it works across lanes unchanged.

            NOTE: the consumer is still a single LLM coroutine (~16/min) —
            after this change THAT, not the producer, is the throughput
            ceiling. (We deliberately do NOT parallelise the consumer:
            that needs OLLAMA_NUM_PARALLEL + concurrent client requests,
            which is a separate, opt-in change.)
            """
            # Worker count per lane, sized to each API's concurrency cap.
            # Pass 99-fu11: GENTLED. The pre-fu11 movie=8/show=4 overran TMDB
            # (real limit ~5/s) → 429 → the 5-transient guard aborted those
            # categories (only music survived). With OMDb-primary (fu11) the
            # movie/show fetch goes through OMDb (supporter key), so TMDB only
            # supplements (429-safe). anime stays AniList-primary → keep it to
            # ONE worker so AniList's Lock(1) + rate limit can't trip the abort.
            # The single LLM consumer (~12/min) is the ceiling anyway, so a few
            # fetch workers per lane is plenty.
            lane_workers = {
                "movie": 4,   # OMDb-primary (supporter key); TMDB supplement only
                "show":  2,   # OMDb-primary
                "anime": 1,   # AniList Lock(1) — gentle, avoids the 429-abort
                "music": 1,   # MusicBrainz Semaphore(1) — hard 1/sec cap
            }
            by_cat: dict[str, list] = {}
            for p in interleaved:
                by_cat.setdefault(p["media_type"], []).append(p)

            async def _lane(cat: str, lane_items: list, n_workers: int) -> None:
                inbox: asyncio.Queue = asyncio.Queue()
                for p in lane_items:
                    inbox.put_nowait(p)

                async def _worker() -> None:
                    while True:
                        if main_task.status.value == "skipped":
                            return
                        try:
                            pitem = inbox.get_nowait()
                        except asyncio.QueueEmpty:
                            return
                        await _process_one(pitem)

                lane_tasks = [asyncio.create_task(_worker()) for _ in range(n_workers)]
                await asyncio.gather(*lane_tasks, return_exceptions=True)
                lanes_done.add(cat)

            lanes = []
            for cat, lane_items in by_cat.items():
                if not lane_items:
                    continue
                n = lane_workers.get(cat, 4)
                logger.info(
                    "[enrichment] lane '%s': %d items, %d workers", cat, len(lane_items), n,
                )
                lanes.append(asyncio.create_task(_lane(cat, lane_items, n)))

            logger.info(
                "[enrichment] Producer lanes started (%d categories, %d items total, "
                "output queue depth %d). Consumer is the ceiling at ~16/min.",
                len(lanes), len(interleaved), _PREFETCH_DEPTH,
            )
            await asyncio.gather(*lanes, return_exceptions=True)

            if main_task.status.value == "skipped":
                logger.warning(
                    "[enrichment] Producer lanes aborted (status=skipped). "
                    "Consumer will drain the remaining queues and exit.",
                )
            # No sentinel needed: the round-robin consumer stops when lanes_done
            # covers all active categories AND every per-category queue is empty.

        from src.services.process_monitor import is_game_running

        async def _write_game_mode_db(item: dict, cat: str, raw: dict):
            """Persist raw API data to SQLite and mark item api_cached in EnrichmentStatus.
            Next normal run hits the raw_prefetch cache — no repeated API calls needed."""
            canonical = item.get("series_title") or item["title"]

            # Persist raw dict so fetch_and_prepare_raw can return it on the next run
            try:
                from src.cache.metadata_cache import MetadataCache as _MC
                _c = _MC()
                _c.set_cache(
                    f"raw_prefetch:{item['plex_rating_key']}",
                    raw,
                    days=7,
                )
                _c.close()
            except Exception as _ce:
                logger.debug("Game-mode raw cache write failed: %s", _ce)

            for _attempt in range(5):
                try:
                    with get_db_session() as db:
                        # Pass 29: same lookup-by-unique-key fix as the
                        # main producer — plex_rating_key, not title.
                        status = db.query(EnrichmentStatus).filter(
                            EnrichmentStatus.plex_rating_key == item["plex_rating_key"],
                        ).first()
                        if not status:
                            status = EnrichmentStatus(
                                plex_rating_key=item["plex_rating_key"],
                                title=canonical,
                                media_category=cat,
                            )
                            db.add(status)
                        else:
                            status.title          = canonical
                            status.media_category = cat
                        status.enriched    = True
                        status.enriched_at = datetime.utcnow()
                        status.error       = "api_cached — LLM pending"
                        db.commit()
                    break
                except Exception as _e:
                    if "database is locked" in str(_e) and _attempt < 4:
                        await asyncio.sleep(1.0 * (_attempt + 1))
                    else:
                        raise

        async def _consumer() -> int:
            nonlocal processed_total
            consumed = 0
            game_skipped = 0
            async def _consume_entry(citem, raw, ccat):
                nonlocal consumed, game_skipped, processed_total
                canonical = citem.get("series_title") or citem["title"]
                task_monitor.update(main_task, message=f"[{ccat}] '{canonical[:50]}'")
                try:
                    if is_game_running():
                        # Persist raw API data to SQLite, skip LLM, mark for later
                        await _write_game_mode_db(citem, ccat, raw)
                        game_skipped += 1
                        if game_skipped == 1:
                            logger.info("Game detected — LLM paused, unloading models, API pre-fetching only")
                            from src.services.process_monitor import unload_llm_models
                            await unload_llm_models()
                    else:
                        await wait_for_enrichment_slot()
                        profile = await process_and_save(raw)
                        await _write_enrichment_db(citem, profile, ccat)
                except Exception as e:
                    import traceback
                    logger.warning("Consumer error [%s] '%s': %s\n%s",
                                   ccat, canonical, e, traceback.format_exc()[-400:])
                finally:
                    # Pass 93: increment the SHARED counter — the producer
                    # also bumps it for not_found sentinels, so reporting
                    # ``processed_total`` reflects the real combined work
                    # done by both sides of the pipeline. ``consumed`` is
                    # still tracked locally for the "X of Y queue items"
                    # diagnostic in the done message.
                    consumed += 1
                    processed_total += 1
                    task_monitor.update(main_task, processed=processed_total, total=total)

            # Round-robin (Pass 99-fu12): up to _CONSUME_BATCH items per
            # category per cycle, skipping empty queues — every category gets a
            # fair turn the moment it has items, instead of the music lane
            # monopolising a single FIFO queue. Stop when all lanes finished AND
            # all per-category queues drained (or the task was cancelled).
            while True:
                if main_task.status.value == "skipped":
                    break
                progressed = False
                for ccat in active_cats:
                    q = cat_queues[ccat]
                    for _ in range(_CONSUME_BATCH):
                        try:
                            citem, raw = q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        progressed = True
                        await _consume_entry(citem, raw, ccat)
                if not progressed:
                    if len(lanes_done) >= len(active_cats):
                        break
                    await asyncio.sleep(0.25)

            # Pass 93: surface the producer-side not_found count in the
            # done message so the user can tell at a glance how much of
            # the total was "couldn't find this in any API" vs actual
            # LLM-polished work.
            msg = f"Complete: {processed_total}/{total} processed"
            if producer_not_found:
                msg += f" ({producer_not_found} not_found by producer)"
            if game_skipped:
                msg += f" ({game_skipped} API-cached, LLM paused for game)"
            task_monitor.done(main_task, msg)
            # Pass 93: return the SHARED total (producer + consumer) so
            # the downstream log line reflects the real work. Previously
            # returned ``consumed`` alone, which silently dropped the
            # producer-side not_found writes from the headline count.
            return processed_total

        _, processed = await asyncio.gather(_producer(), _consumer())

        set_state("last_enrichment_at", datetime.utcnow().isoformat())
        logger.info(
            "Enrichment complete: %d items processed across %s "
            "(of which %d not_found by producer)",
            processed, active_cats, producer_not_found,
        )

        # New enriched data → invalidate downstream recommendation caches.
        if processed and processed > 0:
            from src.services.app_state import set_datetime as _set_dt
            _set_dt("recs_invalidate_at", datetime.utcnow())

        await _run_taste_computation(user_id, categories)

        try:
            from src.services.verification_session import start_verification_session
            await start_verification_session(user_id)
        except Exception as e:
            logger.debug("Post-enrichment verification failed: %s", e)

        # Normal completion — clear interrupted flag
        set_state("enrichment_interrupted", "0")

    except asyncio.CancelledError:
        logger.info("Enrichment cancelled")
        task_monitor.skip(setup_task, "Cancelled")
        set_state("enrichment_interrupted", "0")
    finally:
        set_state("enrichment_running", "0")


async def _run_taste_computation(user_id: int, categories: list = None):
    """Background: compute taste vectors for the enriched categories only."""
    from src.services.taste_engine import compute_all_taste_vectors
    logger.info("Computing taste vectors for user %d categories=%s...", user_id, categories)
    await compute_all_taste_vectors(user_id, categories=categories)
    logger.info("Taste vectors updated for user %d", user_id)


# ---- PROFILE BROWSER + MAPPING STATS ----------------------------------------

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
    source: str = "all",   # "all" | "watch_history" | "arr"
    user: User = Depends(get_current_user),
):
    """Browse enriched profiles from watch history and/or ARR libraries."""
    from src.cache.metadata_cache import MetadataCache
    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry, EnrichmentStatus, ArrEnrichmentStatus
    from sqlalchemy import func as _func

    cache = MetadataCache()
    # Each entry: (cache_key, title, enriched_at, profile_source_label)
    raw: list[tuple] = []

    with get_db_session() as db:

        # ── Watch-history profiles ────────────────────────────────────────────
        if source in ("all", "watch_history"):
            if category in ("show", "anime"):
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
                for r in q.all():
                    raw.append((f"enriched:{category}:{r.plex_rating_key}",
                                r.series_title, r.enriched_at, "watch_history"))
            else:
                q = db.query(
                    EnrichmentStatus.title,
                    _func.max(EnrichmentStatus.plex_rating_key).label("plex_rating_key"),
                    _func.max(EnrichmentStatus.enriched_at).label("enriched_at"),
                ).filter(
                    EnrichmentStatus.media_category == category,
                    EnrichmentStatus.enriched == True,
                ).group_by(EnrichmentStatus.title)
                if search:
                    q = q.filter(EnrichmentStatus.title.ilike(f"%{search}%"))
                for r in q.all():
                    raw.append((f"enriched:{category}:{r.plex_rating_key}",
                                r.title, r.enriched_at, "watch_history"))

        # ── ARR library profiles ──────────────────────────────────────────────
        if source in ("all", "arr"):
            q = db.query(ArrEnrichmentStatus).filter(
                ArrEnrichmentStatus.category == category,
                ArrEnrichmentStatus.enriched == True,
            )
            if search:
                q = q.filter(ArrEnrichmentStatus.title.ilike(f"%{search}%"))
            for row in q.all():
                raw.append((f"enriched:{category}:{row.service}:{row.arr_id}",
                            row.title, row.enriched_at, f"arr:{row.service}"))

    # Deduplicate by normalised title — keep the entry with the most recent enriched_at
    seen: dict[str, tuple] = {}
    for entry in raw:
        key = entry[1].lower().strip()
        existing = seen.get(key)
        if not existing:
            seen[key] = entry
        else:
            ts_new = entry[2]
            ts_old = existing[2]
            if ts_new and (not ts_old or ts_new > ts_old):
                seen[key] = entry

    merged = sorted(seen.values(),
                    key=lambda e: e[2] or datetime.min,
                    reverse=True)
    total = len(merged)
    page  = merged[offset: offset + limit]

    profiles = []
    for cache_key, title, enriched_at, profile_source in page:
        cached  = cache.get_cache(cache_key)
        profile = cached["response"] if cached else None
        profiles.append({
            "plex_rating_key":  cache_key,
            "title":            title,
            "enriched_at":      enriched_at.isoformat() if enriched_at else None,
            "profile_source":   profile_source,   # "watch_history" | "arr:radarr" etc.
            "has_profile":      profile is not None,
            "genres":           profile.get("genres", []) if profile else [],
            "themes":           profile.get("themes", []) if profile else [],
            "mood":             profile.get("mood", []) if profile else [],
            "plot_summary":     (profile.get("plot_summary") or
                                 profile.get("overview", "")[:300]) if profile else None,
            "embedding_text":   profile.get("embedding_text", "") if profile else None,
            "similar_to":       profile.get("similar_to",
                                            profile.get("similar_titles", []))[:5] if profile else [],
            "rating":           profile.get("rating") if profile else None,
            "source":           profile.get("source", "unknown") if profile else None,
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
