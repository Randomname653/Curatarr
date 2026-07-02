"""Per-user watch status from Plex playback history (`watch_history`).

Shared by the chat layer (discussion + RAG neighbours) and the deletion pitch so
the curator never confuses "the user is curious about this unseen title" with
"proven dead weight they watched and moved on from". NOT Tautulli — the owner
doesn't run it; this is the Plex-synced history (completed + viewed_at per play).
"""
from __future__ import annotations

import logging

logger = logging.getLogger("curatarr")


def watched_lookup(user_id: int, titles: list, category: str = None) -> dict:
    """Map each title → {count, completed, last} from watch_history, in ONE query.
    Absent from the result = the user never played it.

    ``category`` adds the MEDIA-FAMILY guard (same rule as pillars._watch_filters):
    a video title must match a non-music row, a music title a music row. Without
    it, a title-only match against the owner's ≈350k Spotify rows turned Don
    McLean's song into "you watched American Pie 17×" and a STRLGHT track into
    "you sampled Blindspot" — poisoning the discussion watch-status, the RAG
    watch-tags and the legacy pitch. ``None`` skips the guard (unknown context)."""
    titles = [t for t in titles if t]
    if not user_id or not titles:
        return {}
    tset = set(titles)
    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry
    from sqlalchemy import or_
    agg: dict = {}
    try:
        with get_db_session() as db:
            q = db.query(
                WatchHistoryEntry.title, WatchHistoryEntry.series_title,
                WatchHistoryEntry.completed, WatchHistoryEntry.viewed_at,
            ).filter(
                WatchHistoryEntry.user_id == user_id,
                or_(WatchHistoryEntry.title.in_(titles),
                    WatchHistoryEntry.series_title.in_(titles)),
            )
            if category:
                q = q.filter(WatchHistoryEntry.media_type == "music"
                             if category == "music"
                             else WatchHistoryEntry.media_type != "music")
            rows = q.all()
    except Exception as e:
        logger.debug("[watch] lookup failed: %s", e)
        return {}
    for r in rows:
        key = r.title if r.title in tset else (
            r.series_title if r.series_title in tset else None)
        if not key:
            continue
        a = agg.setdefault(key, {"count": 0, "completed": False, "last": None})
        a["count"] += 1
        a["completed"] = a["completed"] or bool(r.completed)
        if r.viewed_at and (a["last"] is None or r.viewed_at > a["last"]):
            a["last"] = r.viewed_at
    return agg


def watch_tag(status: dict) -> str:
    """One-line watch tag for a status dict (or None → never watched)."""
    if not status:
        return "NOT watched"
    n = status["count"]
    base = f"watched{f' {n}×' if n > 1 else ''}" if status["completed"] else "started, not finished"
    if status.get("last"):
        base += f", last {status['last'].strftime('%b %Y')}"
    return base
