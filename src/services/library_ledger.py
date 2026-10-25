"""Curatarr — the library ledger: what arrived when, and what left.

Nothing recorded either side of the balance before. ``MediaTechProfile`` is
a snapshot (overwritten, pruned when a title leaves); GB freed was read off
Curatarr's own deletions only, so a title deleted straight in Radarr never
counted, and GB added was not known at all. The wrap-up's hoarder-vs-pruner
balance and "hoarded 412 days before pressing play" need both.

The tech sync already walks the whole video library every 12 hours, and
every file it sees carries Plex's ``addedAt``. This module turns that walk
into two durable tables (``LibraryItem``, ``LibraryEvent`` in models.py):

* arrivals are back-dated from ``addedAt``, one event per title and month,
  so the current library's history is known from the first walk on;
* a title a COMPLETE walk no longer lists is gone (the same guard the tech
  profile prune uses: an outage or a cut-short walk never empties a
  category), and a gone title listed again has returned.

Arrivals of a title Radarr or Sonarr manage come from their history instead
(services/arr_ledger), once it has been read to the end: Plex moves
``addedAt`` with every replaced file and every re-added library. This walk
still decides what left.

Music is not walked by default (see ``sync_tech_profiles``), so it has no
ledger yet.
"""
import logging
from datetime import datetime
from typing import Optional

from src.database.connection import get_db_session

logger = logging.getLogger(__name__)

_MB = 1024 * 1024
IN_KINDS = ("added", "returned", "grew")
OUT_KINDS = ("gone", "shrunk")
# A size change of a title still listed counts once it passes this: a season
# deleted (shrunk) or a file upgraded (grew), not a re-mux's last megabytes.
_CHANGE_MIN_MB = 512
_CHANGE_MIN_SHARE = 0.05


def note_arrival(arrivals: dict, added_ts, size_bytes) -> None:
    """Fold one walked file into an item's arrivals: ``{YYYY-MM: [earliest
    unix time, bytes]}``. A file without ``addedAt`` is skipped."""
    try:
        ts = int(added_ts)
    except (TypeError, ValueError):
        return
    if ts <= 0:
        return
    month = datetime.utcfromtimestamp(ts).strftime("%Y-%m")
    b = arrivals.setdefault(month, [ts, 0])
    b[0] = min(b[0], ts)
    b[1] += int(size_bytes or 0)


def _covered(a: dict, arr: Optional[dict]) -> bool:
    """Does an arr's history date this title's arrivals (services/arr_ledger)?"""
    if not arr:
        return False
    if a.get("media_type") == "movie":
        return a.get("tmdb_id") in arr.get("movie", ())
    return a.get("tvdb_id") in arr.get("tv", ())


def _arr_since(db, known: dict) -> dict:
    """``{rating key: [(at, ±MB)]}``: the arrivals and upgrades the arr history
    holds for a title since the oldest previous walk, by when they happened
    (a first read of the whole history writes years of rows at once: their
    write time says nothing about the files)."""
    from src.database.models import LibraryEvent as E
    seen = [r.last_seen_at for r in known.values() if r.last_seen_at]
    if not seen:
        return {}
    out: dict = {}
    for rk, kind, mb, at in (db.query(E.plex_rating_key, E.kind, E.size_mb, E.at)
                             .filter(E.source == "arr", E.at >= min(seen),
                                     E.kind.in_(("added", "grew", "shrunk")))):
        out.setdefault(rk, []).append((at, -(mb or 0) if kind == "shrunk" else (mb or 0)))
    return out


def persist_ledger(items: dict, complete: set, now: Optional[datetime] = None,
                   arr: Optional[dict] = None) -> dict:
    """Write one walk into the ledger. Blocking — call via ``run_write``.

    ``items``: ``{rating_key: {media_type, title, tmdb_id, tvdb_id,
    size_bytes, arrivals}}`` (the tech sync's accumulator). ``complete``:
    the media types whose walk was complete and non-empty — only there is a
    title that went unlisted gone. ``arr``: the TMDB / TVDB ids whose
    arrivals the arr history dates (``arr_ledger.covered``). For those this
    walk writes no arrival, return or growth — Plex's addedAt moves with
    every replaced file and every re-added library — only what left: gone,
    or shrunk by more than the arr's own records since the last walk explain."""
    from src.database.models import LibraryEvent, LibraryItem
    now = now or datetime.utcnow()
    stamp = now.isoformat(timespec="seconds")
    counts = {"new": 0, "arrivals": 0, "gone": 0, "returned": 0, "grew": 0, "shrunk": 0}
    types = {a.get("media_type") for a in items.values()} | set(complete)
    with get_db_session() as db:
        known = {r.plex_rating_key: r for r in
                 db.query(LibraryItem).filter(LibraryItem.media_type.in_(sorted(t for t in types if t)))}
        added = {(e.plex_rating_key, e.bucket): e for e in
                 db.query(LibraryEvent).filter(LibraryEvent.kind == "added", LibraryEvent.source == "plex")}
        arr_since = _arr_since(db, known) if arr else {}
        for rk, a in items.items():
            rk = str(rk)
            arrivals = a.get("arrivals") or {}
            first = min((v[0] for v in arrivals.values()), default=None)
            added_at = datetime.utcfromtimestamp(first) if first else None
            size_mb = round((a.get("size_bytes") or 0) / _MB, 2)
            title = (a.get("title") or "")[:512] or None
            row = known.get(rk)
            by_arr = _covered(a, arr)
            before_mb = None if row is None or row.gone_at is not None else (row.size_mb or 0)
            last_seen = row.last_seen_at if row is not None else None
            # The month the previous walk saw: an arrival month before it was
            # complete then. New files get today's addedAt, so such a month
            # can only grow through a replaced file — an upgrade, not arrivals.
            filling = (row.last_seen_at or now).strftime("%Y-%m") if row is not None else ""
            if row is None:
                row = LibraryItem(plex_rating_key=rk, media_type=a.get("media_type") or "",
                                  first_seen_at=now)
                db.add(row)
                known[rk] = row
                counts["new"] += 1
            elif row.gone_at is not None:
                row.gone_at = None
                if not by_arr:      # else its re-import is the arr's arrival
                    db.add(LibraryEvent(at=now, kind="returned", bucket=stamp, plex_rating_key=rk,
                                        media_type=row.media_type, title=title or row.title,
                                        size_mb=size_mb))
                    counts["returned"] += 1
            row.media_type = a.get("media_type") or row.media_type
            row.title = title or row.title
            row.tmdb_id = a.get("tmdb_id") or row.tmdb_id
            row.tvdb_id = a.get("tvdb_id") or row.tvdb_id
            if added_at and row.added_from != "arr" and (row.added_at is None or added_at < row.added_at):
                row.added_at = added_at
            row.size_mb = size_mb
            row.last_seen_at = now
            arrived_mb = 0.0      # what this walk's new arrivals explain of a size change
            if by_arr:
                arrived_mb = sum(mb for at, mb in arr_since.get(rk, ())
                                 if last_seen is None or at is None or at > last_seen)
                if before_mb is not None:
                    change = size_mb - before_mb - arrived_mb
                    if change <= -max(_CHANGE_MIN_MB, _CHANGE_MIN_SHARE * before_mb):
                        db.add(LibraryEvent(at=now, kind="shrunk", bucket=stamp, plex_rating_key=rk,
                                            media_type=row.media_type, title=row.title,
                                            size_mb=round(-change, 2)))
                        counts["shrunk"] += 1
                continue
            for month, (ts, nbytes) in arrivals.items():
                at, mb = datetime.utcfromtimestamp(ts), round(nbytes / _MB, 2)
                ev = added.get((rk, month))
                if ev is None:
                    ev = LibraryEvent(at=at, kind="added", bucket=month, plex_rating_key=rk,
                                      media_type=row.media_type, title=row.title, size_mb=mb)
                    db.add(ev)
                    added[(rk, month)] = ev
                    counts["arrivals"] += 1
                    arrived_mb += mb
                elif month >= filling:
                    # A later walk sees fewer files of that month once some
                    # were deleted: an arrival never shrinks after the fact.
                    ev.at = min(ev.at, at)
                    arrived_mb += max(0.0, mb - (ev.size_mb or 0))
                    ev.size_mb = max(ev.size_mb or 0, mb)
            if before_mb is not None:
                change = size_mb - before_mb - arrived_mb
                if abs(change) >= max(_CHANGE_MIN_MB, _CHANGE_MIN_SHARE * before_mb):
                    kind = "grew" if change > 0 else "shrunk"
                    db.add(LibraryEvent(at=now, kind=kind, bucket=stamp, plex_rating_key=rk,
                                        media_type=row.media_type, title=row.title,
                                        size_mb=round(abs(change), 2)))
                    counts[kind] += 1
        for rk, row in known.items():
            if row.media_type in complete and row.gone_at is None and rk not in items:
                row.gone_at = now
                db.add(LibraryEvent(at=now, kind="gone", bucket=stamp, plex_rating_key=rk,
                                    media_type=row.media_type, title=row.title,
                                    size_mb=row.size_mb or 0))
                counts["gone"] += 1
        db.commit()
    if any(counts.values()):
        logger.info("[ledger] %d new titles, %d arrival months, %d gone, %d returned, "
                    "%d grew, %d shrunk", counts["new"], counts["arrivals"], counts["gone"],
                    counts["returned"], counts["grew"], counts["shrunk"])
    return counts


def ledger_start(db) -> Optional[datetime]:
    """When the Plex walk began witnessing departures: its first title."""
    from sqlalchemy import func
    from src.database.models import LibraryItem
    return db.query(func.min(LibraryItem.first_seen_at)).scalar()


def _departures_before(db, start: datetime, end: datetime, types: Optional[list]) -> list:
    """What left before the Plex walk existed, as ``(kind, at, mb, title,
    media_type)`` rows of kind ``gone``: Curatarr's own deletions, and the
    files deleted in the arrs by hand (arr_ledger's ``removed`` rows) that
    are not one of those. Radarr and Sonarr drop the history of a deleted
    film or series, so Curatarr's record is the only one of those."""
    from src.database.models import DeletionProposal as P, LibraryEvent as E
    first = ledger_start(db)
    stop = min(end, first) if first else end
    if stop <= start:
        return []
    out = []
    q = db.query(P.resolved_at, P.storage_mb, P.title, P.category).filter(
        P.status == "deleted", P.resolved_at >= start, P.resolved_at < stop)
    if types:
        q = q.filter(P.category.in_(list(types)))
    ours: dict = {}
    for at, mb, title, cat in q:
        key = (title or "").lower()
        if any(t.date() == at.date() for t in ours.get(key, ())):
            continue        # one deletion, proposed to two members
        out.append(("gone", at, mb or 0, title, cat))
        ours.setdefault(key, []).append(at)
    q = db.query(E.at, E.size_mb, E.title, E.media_type).filter(
        E.kind == "removed", E.at >= start, E.at < stop)
    if types:
        q = q.filter(E.media_type.in_(list(types)))
    for at, mb, title, mtype in q:
        if any(abs((at - t).total_seconds()) < 2 * 86400 for t in ours.get((title or "").lower(), ())):
            continue        # the arr's side of a deletion Curatarr already counted
        out.append(("gone", at, mb or 0, title, mtype))
    return out


def velocity(start: datetime, end: datetime, types: Optional[list] = None, db=None) -> dict:
    """GB in vs GB out over ``[start, end)``, by month, with the biggest
    arrival and departure. Blocking — call via ``run_read``, or pass the
    caller's session as ``db``."""
    from src.database.models import LibraryEvent as E

    def _rows(s):
        q = s.query(E.kind, E.at, E.size_mb, E.title, E.media_type).filter(
            E.at >= start, E.at < end)
        if types:
            q = q.filter(E.media_type.in_(list(types)))
        rows = [r for r in q.all() if r[0] != "removed"]
        return rows + _departures_before(s, start, end, types)

    if db is not None:
        rows = _rows(db)
    else:
        with get_db_session() as s:
            rows = _rows(s)
    by_month: dict = {}
    gb_in = gb_out = 0.0
    titles_in, titles_out = set(), set()
    big_in = big_out = None
    for kind, at, mb, title, mtype in rows:
        gb = (mb or 0) / 1024
        m = by_month.setdefault(at.strftime("%Y-%m"), {"in": 0.0, "out": 0.0})
        if kind in IN_KINDS:
            gb_in += gb
            m["in"] += gb
            titles_in.add(title)
            if big_in is None or gb > big_in["gb"]:
                big_in = {"title": title, "media_type": mtype, "gb": round(gb, 1)}
        elif kind in OUT_KINDS:
            gb_out += gb
            m["out"] += gb
            titles_out.add(title)
            if big_out is None or gb > big_out["gb"]:
                big_out = {"title": title, "media_type": mtype, "gb": round(gb, 1)}
    return {"gb_in": round(gb_in, 1), "gb_out": round(gb_out, 1),
            "net_gb": round(gb_in - gb_out, 1),
            "titles_in": len(titles_in), "titles_out": len(titles_out),
            "by_month": {k: {"in": round(v["in"], 1), "out": round(v["out"], 1)}
                         for k, v in sorted(by_month.items())},
            "biggest_in": big_in, "biggest_out": big_out}
