"""
Curatarr - Watch History Router
Sync Plex history, view history per user, trigger taste recompute.
"""

import json
import logging
from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.database import get_db
from src.database.models import User, WatchHistoryEntry, TasteVectorEntry
from src.routers.auth import get_current_user, require_admin
from src.services.plex_sync import (
    sync_plex_history, get_user_taste_context,
    reattribute_watch_history, cleanup_orphan_watch_history,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/sync")
async def trigger_sync(
    background_tasks: BackgroundTasks,
    force: bool = False,
    user: User = Depends(get_current_user),
):
    """Kick off a Plex watch history sync. Set force=true to bypass rate limit."""
    background_tasks.add_task(_run_sync, force)
    return {"status": "sync_started", "message": "Fetching Plex history and computing taste vectors…"}


async def _run_sync(force: bool = False):
    result = await sync_plex_history(force=force)
    logger.info("Sync result: %s", result)


@router.get("/status")
async def sync_status(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return history count + taste vector summary for the current user."""
    count = db.query(WatchHistoryEntry).filter(WatchHistoryEntry.user_id == user.id).count()
    tv = db.query(TasteVectorEntry).filter(TasteVectorEntry.user_id == user.id).first()

    # Aggregate completion stats per type via SQL instead of loading all rows
    completion_stats = {}
    for mtype in ["music", "movie", "show", "anime"]:
        base = db.query(WatchHistoryEntry).filter(
            WatchHistoryEntry.user_id == user.id,
            WatchHistoryEntry.media_type == mtype,
        )
        total = base.count()
        if not total:
            continue
        completed = base.filter(WatchHistoryEntry.completed == True).count()
        # in_progress: not completed, has both offset and duration
        in_progress_q = base.filter(
            WatchHistoryEntry.completed == False,
            WatchHistoryEntry.view_offset_ms.isnot(None),
            WatchHistoryEntry.duration_ms.isnot(None),
            WatchHistoryEntry.duration_ms > 0,
        ).with_entities(
            WatchHistoryEntry.view_offset_ms,
            WatchHistoryEntry.duration_ms,
        ).all()
        dropped = sum(1 for o, d in in_progress_q if (o / d) < 0.4)
        nearly_done = sum(1 for o, d in in_progress_q if (o / d) >= 0.8)
        completion_stats[mtype] = {
            "total": total,
            "completed": completed,
            "in_progress": len(in_progress_q),
            "dropped": dropped,
            "nearly_done": nearly_done,
        }

    per_type = {}
    if tv and tv.genre_affinity:
        type_data = json.loads(tv.genre_affinity)
        top_titles_by_type = json.loads(tv.top_titles or "{}")
        for mtype, ts in type_data.items():
            if isinstance(ts, dict) and ts.get("watch_count", 0) > 0:
                per_type[mtype] = {
                    "watch_count": ts["watch_count"],
                    "avg_completion": ts["avg_completion"],
                    "top_genres": list((ts.get("genre_affinity") or {}).keys())[:5],
                    "top_titles": (top_titles_by_type.get(mtype) or ts.get("top_titles") or [])[:5],
                    "completion_breakdown": completion_stats.get(mtype, {}),
                }

    return {
        "watch_history_entries": count,
        "taste_vector": {
            "computed": tv is not None,
            "watch_count": tv.watch_count if tv else 0,
            "covers_all": (tv.watch_count == count) if tv else False,
            "avg_completion": tv.avg_completion if tv else 0,
            "avg_completion_note": (
                "100% means Plex only tracks completion for fully-watched items. "
                "In-progress items are synced separately and show real completion %."
            ) if tv and tv.avg_completion >= 0.99 else None,
            "summary": tv.summary_text if tv else None,
            "computed_at": tv.computed_at.isoformat() if tv else None,
            "by_type": per_type,
            "note": (
                f"Taste profile covers {tv.watch_count} of {count} entries. "
                f"Run sync again to recompute with all entries."
                if tv and tv.watch_count != count else None
            ),
        },
    }


@router.get("/taste")
async def get_taste(
    user: User = Depends(get_current_user),
):
    """Return the full taste context string (same as injected into chat)."""
    ctx = await get_user_taste_context(user.id)
    if not ctx:
        raise HTTPException(
            status_code=404,
            detail="No taste vector yet. Run /api/history/sync first.",
        )
    return {"taste_context": ctx}


@router.get("/recent")
async def recent_history(
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    category: str = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return recent watch history, optionally filtered by category."""
    q = db.query(WatchHistoryEntry).filter(WatchHistoryEntry.user_id == user.id)
    if category and category != "all":
        q = q.filter(WatchHistoryEntry.media_type == category)
    entries = q.order_by(WatchHistoryEntry.viewed_at.desc()).offset(offset).limit(limit).all()
    return {
        "offset": offset,
        "limit": limit,
        "entries": [
            {
                "title": e.title,
                "series_title": e.series_title,
                "media_type": e.media_type,
                "viewed_at": e.viewed_at.isoformat(),
                "completed": e.completed,
                "genres": e.genres,
                "season": e.season,
                "episode": e.episode,
            }
            for e in entries
        ],
    }


@router.post("/recompute-taste")
async def recompute_taste(
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
):
    """Recompute taste vectors from current enrichment data."""
    background_tasks.add_task(_run_taste_bg, user.id)
    return {"status": "started"}


async def _run_taste_bg(user_id: int):
    from src.services.taste_engine import compute_all_taste_vectors
    await compute_all_taste_vectors(user_id)


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN-ONLY MAINTENANCE ACTIONS
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/admin/re-attribute")
async def admin_re_attribute(_admin: User = Depends(require_admin)):
    """Re-pull Plex history and fix per-user attribution on existing rows.

    Idempotent. Run once after a new user has logged in via Plex OAuth so
    their pre-existing plays (which were parked on the admin during sync)
    get moved over to their account. Spotify entries are skipped.
    """
    result = await reattribute_watch_history()
    return result


@router.post("/admin/cleanup-orphans")
async def admin_cleanup_orphans(_admin: User = Depends(require_admin)):
    """Drop watch_history rows whose Plex item no longer exists in any library.

    Walks every configured library section, collects all live ratingKeys,
    deletes WatchHistoryEntry rows whose plex_item_id is a numeric ratingKey
    not present anymore. Spotify entries (``plex_item_id`` starts with
    ``spotify:``) are kept untouched. Aborts cleanly if any section fetch
    fails (so a transient network blip can't wipe history).
    """
    result = await cleanup_orphan_watch_history()
    return result
