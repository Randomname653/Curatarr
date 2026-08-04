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


def viewing_pattern(user_id: int, title: str, category: str = None) -> str | None:
    """Derive the SIGNALS from episode-level history that raw counts hide:
    watched in order vs scattered, where the user stopped (down to the
    abandon-point inside an episode via view_offset), entry-loop rewatches
    (E1 three times, never further), and binge vs slow-drip pacing. One
    compact line for the curator, None for movies / no episode rows."""
    if not user_id or not title:
        return None
    # Episode semantics NEVER apply to music: Spotify rows carry
    # season=1/episode=1 per play, so a band anchor produced "played S1E1
    # 39 times" and a fake binge line (the PRESIDENT chat). Music is
    # excluded unconditionally — its depth line is music_listening_stats.
    if category == "music":
        return None
    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry as W
    try:
        with get_db_session() as db:
            # series rows carry the EPISODE name in ``title`` — matching it
            # made the movie "Inception" inherit some series' episode rows.
            # Only series_title identifies the series.
            q = db.query(W.season, W.episode, W.completed, W.viewed_at,
                         W.view_offset_ms, W.duration_ms).filter(
                W.user_id == user_id,
                W.series_title == title,
                W.episode.isnot(None),
                W.media_type != "music",
            )
            rows = q.all()
    except Exception as e:
        logger.debug("[watch] viewing_pattern failed for %r: %s", title, e)
        return None
    if not rows:
        return None

    per_ep: dict = {}
    for r in rows:
        key = (r.season or 1, r.episode)
        d = per_ep.setdefault(key, {"plays": 0, "completed": False,
                                    "pct": None, "last": None})
        d["plays"] += 1
        d["completed"] = d["completed"] or bool(r.completed)
        if r.duration_ms and r.view_offset_ms is not None:
            pct = r.view_offset_ms / max(r.duration_ms, 1)
            d["pct"] = max(d["pct"] or 0.0, min(pct, 1.0))
        if r.viewed_at and (d["last"] is None or r.viewed_at > d["last"]):
            d["last"] = r.viewed_at

    keys = sorted(per_ep)
    parts = []
    # order vs gaps (per lowest season with plays; simple and honest)
    seasons = sorted({s for s, _ in keys})
    if len(seasons) == 1:
        s = seasons[0]
        eps = sorted(e for _, e in keys)
        if len(eps) == 1:
            parts.append(f"only S{s}E{eps[0]}")
        elif eps == list(range(1, len(eps) + 1)):
            parts.append(f"S{s}E1-E{eps[-1]} in order")
        else:
            parts.append(f"{len(eps)} episodes of season {s}, with gaps")
    else:
        parts.append(f"{len(keys)} episodes scattered across seasons "
                     f"{seasons[0]}-{seasons[-1]}")
    # stop point + abandon position — TIME-AWARE. An unconditional "stopped
    # after S1E2" the morning after two episodes reads as abandonment: the
    # curator offered deletion for a series the user discovered YESTERDAY.
    # People sleep and go to work between episodes; only dormancy is a stop.
    from datetime import datetime
    stop = keys[-1]
    stop_d = per_ep[stop]
    last_any = max((d["last"] for d in per_ep.values() if d["last"]), default=None)
    days_since = (datetime.utcnow() - last_any).days if last_any else None
    if days_since is not None and days_since <= 7:
        when = ("today" if days_since == 0 else
                "yesterday" if days_since == 1 else f"{days_since} days ago")
        stop_txt = (f"CURRENTLY WATCHING — at S{stop[0]}E{stop[1]} so far, "
                    f"last episode played {when}")
    elif days_since is not None and days_since <= 60:
        stop_txt = f"paused at S{stop[0]}E{stop[1]}, last played {days_since} days ago"
    else:
        stop_txt = f"stopped after S{stop[0]}E{stop[1]}"
        if days_since is not None:
            stop_txt += f" ({max(days_since // 30, 2)} months without activity)"
        if not stop_d["completed"] and stop_d["pct"] and 0.02 < stop_d["pct"] < 0.95:
            stop_txt += f" (abandoned that episode at {stop_d['pct']:.0%})"
    parts.append(stop_txt)
    # pacing
    dates = [d["last"] for d in per_ep.values() if d["last"]]
    if len(dates) >= 2:
        span = (max(dates) - min(dates)).days
        if span <= 1 and len(per_ep) >= 20:
            # 86 "episodes" in one day is a Plex bulk-mark import, not viewing
            parts.append("likely bulk-marked as watched, not real-time viewing")
        elif span <= 14 and len(per_ep) >= 3:
            # >= 3: two episodes in an evening is just watching, not a binge
            parts.append(f"binged in {max(span, 1)} day(s)")
        elif span > 90:
            parts.append(f"slow drip over {span // 30} months")
    # entry-loop / rewatched episodes
    re_eps = sorted(((k, d["plays"]) for k, d in per_ep.items() if d["plays"] > 1),
                    key=lambda x: -x[1])[:2]
    if re_eps:
        parts.append("re-played " + ", ".join(
            f"S{k[0]}E{k[1]} x{n}" for k, n in re_eps))
    return "; ".join(parts)


def viewing_stop_point(user_id: int, title: str) -> tuple[int, int] | None:
    """The coordinates behind viewing_pattern's stop line: the highest
    (season, episode) the user played for this series, or None."""
    if not user_id or not title:
        return None
    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry as W
    try:
        with get_db_session() as db:
            rows = (db.query(W.season, W.episode)
                    .filter(W.user_id == user_id, W.series_title == title,
                            W.episode.isnot(None),
                            W.media_type != "music").all())
        if not rows:
            return None
        return max((r.season or 1, r.episode) for r in rows)
    except Exception as e:
        logger.debug("[watch] stop point failed for %r: %s", title, e)
        return None


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
