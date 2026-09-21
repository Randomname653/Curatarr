"""
Curatarr — music that arrives later still finds its history.

The Spotify match runs once per play: a cursor in app_state marks how far it
got, and only newer plays are attempted. That cursor exists for a good
reason — the old full re-scan froze the whole app for about a minute every
night — but it bought the speed by giving up on ever improving. The owner's
music arrives in a trickle through SoulSync, so every album that lands
should find the plays it belongs to. With the one-attempt rule it never did:
148,155 of 351,520 plays matched, and that number could only stand still.

The premise turned out to be wrong. Measured on the owner's install on
2026-09-21, against the real 203,365 unmatched plays:

    fetch (three columns, not ORM rows)      0.26 s
    normalise into 47,494 unique keys        0.74 s
    ------------------------------------------------
    a full retro-match pass                  0.99 s

The minute came from building 203k ORM objects, the same thing that made the
proactive triggers slow (PR #102). One second is not a budget problem.

So this runs the match the other way round. Instead of asking every old play
whether Plex has it now, it asks every newly arrived track whether the
history was waiting for it. The arrivals come from the local Plex track
index that the lyrics collector refreshes daily — no extra call to Plex —
and only tracks newer than the last pass are considered, so a quiet day
costs one indexed query and nothing else.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)

_STAMP = "music_rematch_added_at"
_CHUNK = 400          # ids per UPDATE ... IN (...) — SQLite's variable cap is ~999


def _index_db_path() -> Optional[str]:
    try:
        from src.services.lyrics import PLEX_MUSIC_DB_PATH
        return str(PLEX_MUSIC_DB_PATH)
    except Exception as e:                                   # pragma: no cover
        logger.debug("[rematch] no music index: %s", e)
        return None


def arrivals(since: int, *, db_path: Optional[str] = None) -> list:
    """(rating_key, artist, title, added_at) for tracks the Plex index has
    seen since ``since``. Read-only on the collector's own database."""
    path = db_path or _index_db_path()
    if not path:
        return []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except Exception as e:
        logger.debug("[rematch] index unreadable: %s", e)
        return []
    try:
        return list(con.execute(
            "select plex_rating_key, artist, title, added_at from track_lyrics "
            "where added_at > ? and artist is not null and artist <> '' "
            "and title is not null and title <> '' order by added_at",
            (int(since or 0),)))
    except Exception as e:
        logger.debug("[rematch] index query failed: %s", e)
        return []
    finally:
        con.close()


def _unmatched_index(user_id: int) -> dict:
    """{(norm artist, norm title): [play id, …]} over the plays still holding
    a Spotify uri. Columns only — the ORM objects are what used to cost a
    minute."""
    from sqlalchemy import and_

    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry as W
    from src.services.music_matcher import _normalize

    out: dict = {}
    with get_db_session() as db:
        rows = (db.query(W.id, W.series_title, W.title)
                .filter(and_(W.user_id == user_id, W.source == "spotify",
                             W.media_type == "music",
                             W.plex_item_id.like("spotify%")))
                .all())
    for pid, artist, title in rows:
        if not artist or not title:
            continue
        out.setdefault((_normalize(artist), _normalize(title)), []).append(pid)
    return out


def _attach(play_ids: list, rating_key: str) -> int:
    """Point those plays at the Plex track. Still filtered on the unmatched
    shape, so a concurrent run cannot overwrite a match someone else made."""
    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry as W
    done = 0
    with get_db_session() as db:
        for i in range(0, len(play_ids), _CHUNK):
            chunk = play_ids[i:i + _CHUNK]
            done += (db.query(W)
                     .filter(W.id.in_(chunk), W.plex_item_id.like("spotify%"))
                     .update({"plex_item_id": str(rating_key)}, synchronize_session=False))
    return done


def rematch_arrivals(user_id: int, *, db_path: Optional[str] = None,
                     get_state=None, set_state=None) -> dict:
    """Match tracks that arrived in Plex since the last pass against the
    plays that are still unmatched. Returns the numbers; a quiet run costs
    one indexed query."""
    if get_state is None or set_state is None:
        from src.services.app_state import get_state as _gs, set_state as _ss
        get_state, set_state = get_state or _gs, set_state or _ss
    key = f"{_STAMP}:{user_id}"
    try:
        since = int(get_state(key) or 0)
    except (TypeError, ValueError):
        since = 0

    new_tracks = arrivals(since, db_path=db_path)
    if not new_tracks:
        return {"arrivals": 0, "matched_plays": 0, "matched_tracks": 0, "since": since}

    from src.services.music_matcher import _normalize
    index = _unmatched_index(user_id)
    matched_plays = matched_tracks = 0
    high = since
    if index:
        for rating_key, artist, title, added_at in new_tracks:
            high = max(high, int(added_at or 0))
            ids = index.pop((_normalize(artist), _normalize(title)), None)
            if not ids:
                continue
            wrote = _attach(ids, rating_key)
            if wrote:
                matched_tracks += 1
                matched_plays += wrote
    else:
        high = max([int(t[3] or 0) for t in new_tracks] + [since])

    set_state(key, str(high))
    logger.info("[rematch] %d new Plex tracks -> %d tracks matched %d historic plays "
                "(stamp %d -> %d)", len(new_tracks), matched_tracks, matched_plays, since, high)
    return {"arrivals": len(new_tracks), "matched_plays": matched_plays,
            "matched_tracks": matched_tracks, "since": since, "stamp": high}
