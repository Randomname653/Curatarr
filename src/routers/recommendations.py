"""
Curatarr 1.0 - Recommendations & Deletions Router

All endpoints are category-aware and use the LLM for pitches.
"""

import logging
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.database import get_db
from src.database.connection import get_db_session
from src.database.models import DeletionProposal, User
from src.routers.auth import get_current_user
from src.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

CATEGORIES = ["movie", "show", "anime", "music"]


@router.get("/")
async def get_recommendations(
    category: Optional[str] = Query(None),
    limit: int = Query(8),
    refresh: bool = Query(False),
    source: str = Query("cache"),  # cache / library / external
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from src.database.models import CachedRecommendation
    from src.services.app_state import get_state
    CAT_LABEL = {"movie":"🎬 Movies","show":"📺 TV Shows","anime":"⛩️ Anime","music":"🎵 Music"}

    # Library-based: from ARR items not yet watched
    if source == "library":
        from src.services.recommendations_engine import generate_recommendations, score_arr_items
        unwatched = await _fetch_arr_unwatched(user.id, category)
        if not unwatched:
            return {"recommendations": [], "category": category, "source": "library",
                    "message": "No unwatched items in your ARR libraries, or ARR not configured."}
        cats = [category] if category and category in CATEGORIES else CATEGORIES
        all_recs = []
        for cat in cats:
            cat_items = [i for i in unwatched if i.get("category") == cat]
            if not cat_items: continue
            # Pre-filter to top 50 most relevant items to avoid overwhelming the LLM context
            cat_items = await score_arr_items(user.id, cat, cat_items, top_n=50)
            recs = await generate_recommendations(user_id=user.id, category=cat,
                                                   limit=limit, arr_library=cat_items)
            for rec in recs:
                match = next((i for i in cat_items if i["title"] == rec.get("title")), {})
                rec["arr_url"] = match.get("arr_url", "")
                rec["size_gb"] = round(match.get("size_mb", 0) / 1024, 1)
                rec["poster_url"] = await _fetch_poster(rec.get("title", ""), cat)
                rec["category_label"] = CAT_LABEL.get(cat, cat)
            all_recs.extend(recs)
        return {"recommendations": all_recs, "category": category, "source": "library", "from_cache": False}

    # Serve from cache unless refresh/external requested
    if not refresh and source != "external":
        q = db.query(CachedRecommendation).filter(CachedRecommendation.user_id == user.id)
        if category and category in CATEGORIES:
            q = q.filter(CachedRecommendation.category == category)
        cached = q.order_by(CachedRecommendation.confidence.desc()).limit(
            limit * (1 if category else 4)).all()
        if cached:
            recs = [{"title": r.title, "reason": r.reason, "confidence": r.confidence,
                     "genres": r.genres, "category": r.category,
                     "category_label": CAT_LABEL.get(r.category, r.category),
                     "poster_url": r.poster_url,
                     "cached_at": r.cached_at.isoformat() if r.cached_at else None}
                    for r in cached]
            return {"recommendations": recs, "category": category,
                    "from_cache": True, "source": "cache",
                    "cached_at": get_state("recs_cached_at")}

    # Generate on the fly
    from src.services.recommendations_engine import generate_recommendations
    cats = [category] if category and category in CATEGORIES else CATEGORIES
    all_recs = []
    for cat in cats:
        recs = await generate_recommendations(user_id=user.id, category=cat, limit=limit)
        for rec in recs:
            rec["poster_url"] = await _fetch_poster(rec.get("title", ""), cat)
            rec["category_label"] = CAT_LABEL.get(cat, cat)
        all_recs.extend(recs)
    return {"recommendations": all_recs, "category": category, "from_cache": False, "source": "external"}


@router.post("/refresh-cache")
async def refresh_recommendation_cache(
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
):
    """Manually trigger recommendation cache refresh."""
    background_tasks.add_task(_run_cache_refresh, user.id)
    return {"status": "started", "message": "Generating recommendations in background…"}


async def _run_cache_refresh(user_id: int):
    from src.services.scheduler import _cache_recommendations
    await _cache_recommendations(user_id)


# ── POSTER FETCHING ───────────────────────────────────────────────────────────

async def _fetch_poster(title: str, category: str) -> Optional[str]:
    """Fetch poster URL from TMDB. Returns full URL or None."""
    tmdb_key = settings.TMDB_API_KEY
    if not tmdb_key or not title:
        return None

    try:
        media_type = "movie" if category == "movie" else "tv"
        if category == "music":
            return None  # no poster for music

        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"https://api.themoviedb.org/3/search/{media_type}",
                params={"api_key": tmdb_key, "query": title, "page": 1},
            )
        if r.status_code != 200:
            return None

        results = r.json().get("results", [])
        if not results:
            return None

        poster_path = results[0].get("poster_path")
        if poster_path:
            return f"https://image.tmdb.org/t/p/w300{poster_path}"
    except Exception:
        pass
    return None


async def _fetch_poster_batch(titles_cats: list) -> dict:
    """Fetch posters for multiple titles concurrently. Returns {title: url}."""
    import asyncio
    sem = asyncio.Semaphore(5)

    async def fetch_one(title, cat):
        async with sem:
            return title, await _fetch_poster(title, cat)

    results = await asyncio.gather(*[fetch_one(t, c) for t, c in titles_cats])
    return {t: u for t, u in results if u}


@router.get("/by-category")
async def get_recommendations_by_category(
    limit: int = Query(5),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from src.database.models import CachedRecommendation
    cats = CATEGORIES
    result = {}
    for cat in cats:
        cached = db.query(CachedRecommendation).filter(
            CachedRecommendation.user_id == user.id,
            CachedRecommendation.category == cat,
        ).order_by(CachedRecommendation.confidence.desc()).limit(limit).all()

        if cached:
            result[cat] = [
                {"title": r.title, "reason": r.reason, "confidence": r.confidence,
                 "genres": r.genres, "poster_url": r.poster_url,
                 "category": cat}
                for r in cached
            ]
    return {"by_category": result}


@router.get("/deletions")
async def get_deletion_proposals(
    category: Optional[str] = Query(None),
    refresh: bool = Query(False),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from src.services.recommendations_engine import generate_deletion_proposals

    if not refresh:
        q = db.query(DeletionProposal).filter(
            DeletionProposal.user_id == user.id,
            DeletionProposal.status == "pending",
        )
        cached = q.order_by(DeletionProposal.confidence.desc()).limit(30).all()
        if cached:
            return {
                "proposals": [
                    {"id": p.id, "title": p.title, "pitch": p.reason,
                     "confidence": p.confidence, "service": p.service,
                     "arr_url": p.arr_url, "size_gb": round(p.storage_mb / 1024, 2),
                     "status": p.status, "user_comment": p.user_comment}
                    for p in cached
                ],
                "total_gb": round(sum(p.storage_mb for p in cached) / 1024, 1),
            }

    arr_items = await _fetch_arr_candidates(category)
    if not arr_items:
        return {"proposals": [], "total_gb": 0,
                "message": "No ARR services configured or no candidates found."}

    proposals = await generate_deletion_proposals(user.id, arr_items, category)

    with get_db_session() as dbs:
        saved = []
        for p in proposals:
            row = DeletionProposal(
                user_id=user.id, media_id=str(p.get("arr_id", "")),
                title=p["title"], service=p.get("service", ""),
                arr_url=p.get("arr_url", ""), reason=p["pitch"],
                confidence=p["confidence"], storage_mb=p.get("size_mb", 0),
                status="pending",
            )
            dbs.add(row)
            saved.append((p, row))
        dbs.flush()  # assign DB IDs before commit
        proposals_with_ids = [
            {**p, "id": row.id, "size_gb": round((p.get("size_mb") or 0) / 1024, 1)}
            for p, row in saved
        ]
        dbs.commit()

    return {"proposals": proposals_with_ids,
            "total_gb": round(sum(p.get("size_mb", 0) for p in proposals) / 1024, 1)}


@router.post("/deletions/{proposal_id}/comment")
async def update_comment(
    proposal_id: int, comment: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    p = db.query(DeletionProposal).filter(
        DeletionProposal.id == proposal_id,
        DeletionProposal.user_id == user.id
    ).first()
    if not p:
        raise HTTPException(404, "Not found")
    p.user_comment = comment
    db.commit()
    if comment:
        from src.services.episodic_memory import write_memory
        await write_memory(user_id=user.id, memory_type="feedback",
            content=f"About deleting '{p.title}': {comment}",
            metadata={"title": p.title, "source": "deletion_feedback"})
    return {"ok": True}


@router.post("/deletions/{proposal_id}/approve")
async def approve_deletion(
    proposal_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    p = db.query(DeletionProposal).filter(
        DeletionProposal.id == proposal_id,
        DeletionProposal.user_id == user.id,
        DeletionProposal.status == "pending",
    ).first()
    if not p:
        raise HTTPException(404, "Not found")
    success = await _execute_arr_delete(p)
    p.status = "deleted" if success else "error"
    p.resolved_at = datetime.utcnow()
    db.commit()
    return {"ok": success, "status": p.status}


@router.post("/deletions/{proposal_id}/reject")
async def reject_deletion(
    proposal_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    p = db.query(DeletionProposal).filter(
        DeletionProposal.id == proposal_id,
        DeletionProposal.user_id == user.id,
    ).first()
    if not p:
        raise HTTPException(404, "Not found")
    p.status = "rejected"
    db.commit()
    return {"ok": True}


# ── ARR HELPERS ───────────────────────────────────────────────────────────────

async def _fetch_arr_candidates(category: str = None) -> list:
    """Fetch all items from ARR services. Includes unwatched items for recommendations."""
    import httpx
    candidates = []

    if (not category or category == "movie") and settings.RADARR_URL and settings.RADARR_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"{settings.RADARR_URL.rstrip('/')}/api/v3/movie",
                    headers={"X-Api-Key": settings.RADARR_API_KEY})
            if r.status_code == 200:
                for m in r.json():
                    if not m.get("hasFile"): continue
                    candidates.append({
                        "title": m.get("title", ""), "year": m.get("year"),
                        "genres": ", ".join(m.get("genres", [])[:4]),
                        "size_mb": m.get("sizeOnDisk", 0) / (1024*1024),
                        "service": "radarr", "arr_id": m.get("id"),
                        "arr_url": f"{settings.RADARR_URL}/movie/{m.get('titleSlug','')}",
                        "category": "movie",
                        "tmdb_id": m.get("tmdbId"),  # Radarr correctly provides TMDB IDs
                        "imdb_id": m.get("imdbId"),
                        "monitored": m.get("monitored", True),
                    })
        except Exception as e:
            logger.warning("Radarr: %s", e)

    if (not category or category in ("show", "anime")) and settings.SONARR_URL and settings.SONARR_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"{settings.SONARR_URL.rstrip('/')}/api/v3/series",
                    headers={"X-Api-Key": settings.SONARR_API_KEY})
            if r.status_code == 200:
                for s in r.json():
                    size = s.get("statistics", {}).get("sizeOnDisk", 0)
                    if not size: continue
                    genres = s.get("genres", [])
                    is_anime = "Anime" in genres or s.get("seriesType") == "anime"
                    # Sonarr uses tvdbId — NOT tmdbId. Store as tvdb_id so
                    # enrich_media_item routes to title search instead of direct TMDB ID call
                    tvdb_id = s.get("tvdbId")
                    candidates.append({
                        "title": s.get("title", ""), "year": s.get("year"),
                        "genres": ", ".join(genres[:4]),
                        "size_mb": size / (1024*1024),
                        "service": "sonarr", "arr_id": s.get("id"),
                        "arr_url": f"{settings.SONARR_URL}/series/{s.get('titleSlug','')}",
                        "category": "anime" if is_anime else "show",
                        "tvdb_id": tvdb_id,
                        "tmdb_id": None,  # Sonarr doesn't provide TMDB IDs
                        "imdb_id": s.get("imdbId"),
                        "sonarr_series_type": s.get("seriesType", "standard"),
                        "monitored": s.get("monitored", True),
                    })
        except Exception as e:
            logger.warning("Sonarr: %s", e)

    if (not category or category == "music") and settings.LIDARR_URL and settings.LIDARR_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(f"{settings.LIDARR_URL.rstrip('/')}/api/v1/artist",
                    headers={"X-Api-Key": settings.LIDARR_API_KEY})
            if r.status_code == 200:
                for a in r.json():
                    stats = a.get("statistics", {})
                    if not stats.get("sizeOnDisk", 0): continue
                    candidates.append({
                        "title": a.get("artistName", ""), "year": None,
                        "genres": ", ".join(a.get("genres", [])[:4]),
                        "size_mb": stats.get("sizeOnDisk", 0) / (1024*1024),
                        "service": "lidarr", "arr_id": a.get("id"),
                        "arr_url": f"{settings.LIDARR_URL}/artist/{a.get('foreignArtistId','')}",
                        "category": "music",
                        "musicbrainz_id": a.get("foreignArtistId"),
                        "monitored": a.get("monitored", True),
                    })
        except Exception as e:
            if e.args:
                logger.warning("Lidarr: %s", e)
            else:
                logger.debug("Lidarr: not configured or unreachable")

    return candidates


async def _fetch_arr_unwatched(user_id: int, category: str = None) -> list:
    """
    Fetch ARR items that are downloaded but not yet watched.
    Used for library-based recommendations.
    """
    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry

    all_items = await _fetch_arr_candidates(category)
    if not all_items:
        return []

    # Get watched titles
    with get_db_session() as db:
        watched = {
            r.series_title or r.title
            for r in db.query(
                WatchHistoryEntry.series_title,
                WatchHistoryEntry.title,
            ).filter(WatchHistoryEntry.user_id == user_id).all()
        }

    # Return only unwatched
    unwatched = [
        item for item in all_items
        if item["title"] not in watched
    ]
    logger.info("ARR unwatched: %d/%d items", len(unwatched), len(all_items))
    return unwatched


async def _execute_arr_delete(p: DeletionProposal) -> bool:
    import httpx
    try:
        if p.service == "radarr" and settings.RADARR_URL and settings.RADARR_API_KEY:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.delete(
                    f"{settings.RADARR_URL.rstrip('/')}/api/v3/movie/{p.media_id}",
                    headers={"X-Api-Key": settings.RADARR_API_KEY},
                    params={"deleteFiles": "true"})
            return r.status_code in (200, 204)
        if p.service == "sonarr" and settings.SONARR_URL and settings.SONARR_API_KEY:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.delete(
                    f"{settings.SONARR_URL.rstrip('/')}/api/v3/series/{p.media_id}",
                    headers={"X-Api-Key": settings.SONARR_API_KEY},
                    params={"deleteFiles": "true"})
            return r.status_code in (200, 204)
    except Exception as e:
        logger.error("Delete failed: %s", e)
    return False
