"""Curatarr — whose clock: the user's own time zone, DST included.

Play rows carry naive UTC. Local time came from this machine's CURRENT
offset (``viewing_sessions.utc_offset``), applied to every row: plays from
the other half of the year were an hour off across a DST change — enough to
move a 23:30 play out of "night" — and a container whose clock is UTC (the
Docker default) put everyone's evening in UTC. A household member in
another zone was wrong all year.

Now the browser reports its IANA zone (``User.timezone``, POST
/api/users/me/timezone), and each row is converted with the offset that was
in force at ITS time. Without a stored zone the machine's zone is used: TZ,
else /etc/timezone, else the current fixed offset (the old behaviour).

``local_sql`` is the same conversion inside a query: SQLite has no zone
data, so the expression carries the zone's offset changes over the queried
span as a CASE — exact, and a handful of branches per year.
"""
import logging
import os
from datetime import datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


def valid_zone(name: Optional[str]) -> Optional[str]:
    """The name when it is a known IANA zone, else None."""
    if not name or not isinstance(name, str) or len(name) > 64:
        return None
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError):
        return None


@lru_cache(maxsize=1)
def server_zone() -> tzinfo:
    """This machine's zone: TZ, else /etc/timezone, else today's fixed offset."""
    for name in (os.environ.get("TZ", "").lstrip(":"), _etc_timezone()):
        if valid_zone(name):
            return ZoneInfo(name)
    from src.services.viewing_sessions import utc_offset
    return timezone(utc_offset())


def _etc_timezone() -> str:
    try:
        with open("/etc/timezone", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def zone_of(name: Optional[str]) -> tzinfo:
    """A stored zone name → its zone; unknown or empty → the machine's."""
    return ZoneInfo(name) if valid_zone(name) else server_zone()


def user_zone(db, user_id: int) -> tzinfo:
    """The user's zone, read in the caller's session."""
    from src.database.models import User
    name = db.query(User.timezone).filter(User.id == user_id).scalar()
    return zone_of(name)


def to_local(dt: datetime, zone: Optional[tzinfo] = None) -> datetime:
    """Naive UTC → naive wall clock of ``zone`` (the machine's by default),
    with the offset in force at ``dt``."""
    zone = zone or server_zone()
    return dt.replace(tzinfo=timezone.utc).astimezone(zone).replace(tzinfo=None)


def _offset_at(zone: tzinfo, dt: datetime) -> timedelta:
    return dt.replace(tzinfo=timezone.utc).astimezone(zone).utcoffset() or timedelta(0)


def offset_changes(zone: tzinfo, start: datetime, end: datetime) -> list:
    """[(utc moment, offset from then on)] over [start, end], beginning with
    the offset at ``start``. Found by a daily scan, then narrowed to the
    minute — zones change on minute boundaries."""
    out = [(start, _offset_at(zone, start))]
    day = timedelta(days=1)
    t = start
    while t < end:
        nxt = min(t + day, end)
        if _offset_at(zone, nxt) != out[-1][1]:
            lo, hi = t, nxt
            while hi - lo > timedelta(minutes=1):
                mid = lo + (hi - lo) / 2
                if _offset_at(zone, mid) == out[-1][1]:
                    lo = mid
                else:
                    hi = mid
            at = hi.replace(second=0, microsecond=0)
            if _offset_at(zone, at) == out[-1][1]:
                at += timedelta(minutes=1)
            out.append((at, _offset_at(zone, at)))
        t = nxt
    return out


def _modifier(off: timedelta) -> str:
    return f"{int(off.total_seconds() // 60):+d} minutes"


def local_sql(col, zone: tzinfo, start: datetime, end: datetime):
    """SQLite ``datetime(col, <offset>)`` with the offset in force at each
    row's time, for rows inside [start, end)."""
    from sqlalchemy import case, func
    changes = offset_changes(zone, start, end)
    if len(changes) == 1:
        return func.datetime(col, _modifier(changes[0][1]))
    branches = [(col < at, _modifier(prev[1])) for prev, (at, _) in zip(changes, changes[1:])]
    return func.datetime(col, case(*branches, else_=_modifier(changes[-1][1])))
