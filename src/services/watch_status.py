"""Per-user watch status from Plex playback history (`watch_history`).

Shared by the chat layer (discussion + RAG neighbours) and the deletion pitch so
the curator never confuses "the user is curious about this unseen title" with
"proven dead weight they watched and moved on from". NOT Tautulli — the owner
doesn't run it; this is the Plex-synced history (completed + viewed_at per play).
"""
from __future__ import annotations

import logging

logger = logging.getLogger("curatarr")


# MusicBrainz names use typographic dashes (U+2010 "Mike WiLL Made‐It") while
# the Plex/Spotify history carries ASCII "-" — an exact match finds NOTHING
# for such artists. Fold every dash variant before comparing.
_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—−"})


def _artist_variants(name: str) -> list[str]:
    base = (name or "").strip().lower()
    folded = base.translate(_DASHES)
    return list({base, folded})


def music_listening_stats(user_id: int, artist: str,
                          artist_mbid: str = None) -> dict | None:
    """The owner's REAL listening record for one artist, from watch_history:
    {plays, tracks, last, top:[(track, plays)…]} or None when never played.
    Matches by dash-folded artist name OR artist_mbid (the robust key)."""
    if not user_id or not (artist or artist_mbid):
        return None
    from sqlalchemy import func, or_
    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry as W
    conds = []
    variants = _artist_variants(artist) if artist else []
    if variants:
        conds.append(func.lower(W.series_title).in_(variants))
    if artist_mbid:
        conds.append(W.artist_mbid == artist_mbid)
    try:
        with get_db_session() as db:
            base = db.query(W).filter(W.user_id == user_id,
                                      W.media_type == "music", or_(*conds))
            plays = base.count()
            if not plays:
                return None
            tracks = base.with_entities(func.count(func.distinct(W.title))).scalar()
            last = base.with_entities(func.max(W.viewed_at)).scalar()
            top = (db.query(W.title, func.count(W.id).label("n"))
                   .filter(W.user_id == user_id, W.media_type == "music", or_(*conds))
                   .group_by(W.title).order_by(func.count(W.id).desc())
                   .limit(5).all())
            return {"plays": plays, "tracks": tracks, "last": last,
                    "top": [(t, n) for t, n in top]}
    except Exception as e:
        logger.debug("[watch] music_listening_stats failed for %r: %s", artist, e)
        return None


def format_listening_line(stats: dict | None) -> str:
    """One evidence line from music_listening_stats — honest about silence."""
    if not stats:
        return "NO recorded plays in the owner's listening history."
    when = f", last {stats['last'].strftime('%b %Y')}" if stats.get("last") else ""
    top = "; top tracks: " + ", ".join(
        f"{t} ({n} plays)" for t, n in (stats.get("top") or [])[:3])
    return (f"{stats['plays']} plays across {stats['tracks']} distinct "
            f"tracks{when}{top}")


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
                WatchHistoryEntry.season, WatchHistoryEntry.episode,
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
        a = agg.setdefault(key, {"count": 0, "completed": False, "last": None,
                                 "_eps": set()})
        a["count"] += 1
        a["completed"] = a["completed"] or bool(r.completed)
        if r.episode is not None:
            a["_eps"].add((r.season, r.episode))
        if r.viewed_at and (a["last"] is None or r.viewed_at > a["last"]):
            a["last"] = r.viewed_at
    for a in agg.values():
        a["episodes"] = len(a.pop("_eps"))
    return agg


def watch_tag(status: dict) -> str:
    """One-line watch tag for a status dict (or None → never watched).

    Series honesty: 9 watch-history ROWS are 9 EPISODE plays, not 9 series
    rewatches — "watched 9x" made the curator theorize about a title the
    user had merely started ("you've watched Kill la Kill nine times")."""
    if not status:
        return "NOT watched"
    n = status["count"]
    eps = status.get("episodes") or 0
    if eps >= 2:
        base = f"{eps} episodes played" + (f" ({n} plays)" if n > eps else "")
    else:
        base = (f"watched{f' {n}×' if n > 1 else ''}"
                if status["completed"] else "started, not finished")
    if status.get("last"):
        base += f", last {status['last'].strftime('%b %Y')}"
    return base
