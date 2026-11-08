"""On this day, a year ago: what a member finished around today's date.

A quiet row on the recommendations page, shown only on days that have one,
never a push and never a curator line. Memory features are known to hurt
when they bring back the wrong thing (Facebook's and Google Photos' own
research), so every title can be hidden for good, per member.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Optional

WINDOW = timedelta(days=3)       # "a year ago" means within three days of it
LIMIT = 8
_HIDDEN = "on_this_day_hidden:user_id={uid}"


def hidden(user_id: int) -> set:
    """The normalised titles this member hid. Blocking."""
    from src.services.app_state import get_state
    try:
        return set(json.loads(get_state(_HIDDEN.format(uid=user_id)) or "[]"))
    except (ValueError, TypeError):
        return set()


def hide(user_id: int, title: str) -> None:
    """Never show ``title`` here again for this member. Blocking."""
    from src.services.app_state import set_state
    from src.services.library_memory import normalize_title
    keep = hidden(user_id) | {normalize_title(title)}
    set_state(_HIDDEN.format(uid=user_id), json.dumps(sorted(keep)))


def a_year_ago(user_id: int, now: Optional[datetime] = None) -> list[dict]:
    """[{title, category, viewed_at, episodes}] finished within three days
    of this date one year ago, a series once (with how many episodes), the
    closest to the date first; hidden titles left out. Blocking."""
    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry as W
    from src.services.library_memory import normalize_title
    now = now or datetime.utcnow()
    try:
        then = now.replace(year=now.year - 1)
    except ValueError:                       # 29 February
        then = now.replace(year=now.year - 1, day=28)
    with get_db_session() as db:
        rows = (db.query(W.title, W.series_title, W.media_type, W.viewed_at)
                .filter(W.user_id == user_id, W.completed.is_(True),
                        W.media_type.in_(("movie", "show", "anime")),
                        W.viewed_at >= then - WINDOW, W.viewed_at <= then + WINDOW).all())
    skip = hidden(user_id)
    by: dict = {}
    for title, series, cat, at in rows:
        name = series if cat in ("show", "anime") and series else title
        if not name or normalize_title(name) in skip:
            continue
        cur = by.setdefault((cat, name), {"title": name, "category": cat, "viewed_at": at, "episodes": 0})
        cur["episodes"] += 1 if cat != "movie" else 0
        if abs((at - then).total_seconds()) < abs((cur["viewed_at"] - then).total_seconds()):
            cur["viewed_at"] = at
    out = sorted(by.values(), key=lambda x: abs((x["viewed_at"] - then).total_seconds()))
    return [{**x, "viewed_at": x["viewed_at"].isoformat() + "Z"} for x in out[:LIMIT]]
