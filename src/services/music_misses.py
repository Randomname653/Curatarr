"""
Curatarr — a lookup that found nothing is a result, and it has to be written
down.

Measured on the owner's install on 2026-09-20: 1,696 Spotify artists carried
no MusicBrainz id, and the resolver's selection was simply "artist_mbid IS
NULL". A miss wrote nothing, so the same names came back the next night, were
queried again at MusicBrainz's ~1 request per second, and failed again.
Roughly seventeen minutes of rate-limited traffic a day for a yield near
zero, for names MusicBrainz will never know: "JuliensBlog", "Hotline in
Paris", "DREWXHILL", 893 of them with a single play to their name.

The genre phase in the same module already does this right — Last.fm misses
are written back as an empty genre so the track leaves the retry pool. The
artist id cannot use that trick: too many readers treat "artist_mbid is not
null" as "resolved", and an empty string would count as a hit in the
Knowledge Base and hand the Lidarr-add flow an empty id. So the negative
lives in its own small table, the way enrichment_status already records a
failed match.

Not permanent: MusicBrainz grows, and a name that is unknown today can be
there in a month. Each miss earns a longer wait than the last (14, 30, then
90 days), so a queue of hopeless names costs one query per name per quarter
instead of one per night.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

ARTIST_MBID = "artist_mbid"

# Attempt number -> how long before we ask again. The last entry repeats.
_BACKOFF_DAYS = (14, 30, 90)


def _wait_days(attempts: int) -> int:
    idx = min(max(attempts, 1), len(_BACKOFF_DAYS)) - 1
    return _BACKOFF_DAYS[idx]


def _norm(name: str) -> str:
    return (name or "").strip().lower()


def due_filter(names: Iterable[str], *, kind: str = ARTIST_MBID,
               now: Optional[datetime] = None) -> list:
    """The subset of ``names`` worth querying: never missed, or past the
    wait. Order is preserved — the caller's batch cap still means "the first
    N that are due"."""
    now = now or datetime.utcnow()
    names = list(names)
    if not names:
        return []
    from src.database.connection import get_db_session
    from src.database.models import MusicLookupMiss
    wanted = {_norm(n) for n in names}
    try:
        with get_db_session() as db:
            rows = (db.query(MusicLookupMiss.name, MusicLookupMiss.next_retry_at)
                    .filter(MusicLookupMiss.kind == kind).all())
    except Exception as e:                                   # pragma: no cover
        logger.debug("[music-misses] read failed, querying everything: %s", e)
        return names
    parked = {r[0] for r in rows
              if _norm(r[0]) in wanted and r[1] is not None and r[1] > now}
    if not parked:
        return names
    out = [n for n in names if _norm(n) not in {_norm(p) for p in parked}]
    logger.info("[music-misses] %s: %d of %d names still waiting out a miss",
                kind, len(names) - len(out), len(names))
    return out


def record_miss(name: str, *, kind: str = ARTIST_MBID, reason: str = "",
                now: Optional[datetime] = None) -> int:
    """Write down that this lookup found nothing; returns the wait in days."""
    now = now or datetime.utcnow()
    if not (name or "").strip():
        return 0
    from src.database.connection import get_db_session
    from src.database.models import MusicLookupMiss
    try:
        with get_db_session() as db:
            row = (db.query(MusicLookupMiss)
                   .filter(MusicLookupMiss.kind == kind, MusicLookupMiss.name == name)
                   .first())
            if row is None:
                row = MusicLookupMiss(kind=kind, name=name, attempts=0)
                db.add(row)
            row.attempts = int(row.attempts or 0) + 1
            row.last_attempt_at = now
            days = _wait_days(row.attempts)
            row.next_retry_at = now + timedelta(days=days)
            row.last_reason = (reason or "")[:200] or None
            return days
    except Exception as e:                                   # pragma: no cover
        logger.debug("[music-misses] write failed for %r: %s", name, e)
        return 0


def clear_miss(name: str, *, kind: str = ARTIST_MBID) -> None:
    """A later lookup succeeded — drop the negative so the history stays
    honest and a renamed artist does not inherit an old wait."""
    from src.database.connection import get_db_session
    from src.database.models import MusicLookupMiss
    try:
        with get_db_session() as db:
            (db.query(MusicLookupMiss)
             .filter(MusicLookupMiss.kind == kind, MusicLookupMiss.name == name)
             .delete(synchronize_session=False))
    except Exception as e:                                   # pragma: no cover
        logger.debug("[music-misses] clear failed for %r: %s", name, e)


def stats(kind: str = ARTIST_MBID, *, now: Optional[datetime] = None) -> dict:
    """For the Knowledge Base: how much of the queue is parked, and when the
    next one is due back."""
    now = now or datetime.utcnow()
    from src.database.connection import get_db_session
    from src.database.models import MusicLookupMiss
    try:
        with get_db_session() as db:
            q = db.query(MusicLookupMiss).filter(MusicLookupMiss.kind == kind)
            total = q.count()
            waiting = q.filter(MusicLookupMiss.next_retry_at > now).count()
            nxt = (q.filter(MusicLookupMiss.next_retry_at > now)
                   .order_by(MusicLookupMiss.next_retry_at.asc()).first())
            return {"missed": total, "waiting": waiting, "due": total - waiting,
                    "next_retry_at": nxt.next_retry_at.isoformat() if nxt else None}
    except Exception as e:                                   # pragma: no cover
        logger.debug("[music-misses] stats failed: %s", e)
        return {"missed": 0, "waiting": 0, "due": 0, "next_retry_at": None}
