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


def persist_ledger(items: dict, complete: set, now: Optional[datetime] = None) -> dict:
    """Write one walk into the ledger. Blocking — call via ``run_write``.

    ``items``: ``{rating_key: {media_type, title, tmdb_id, tvdb_id,
    size_bytes, arrivals}}`` (the tech sync's accumulator). ``complete``:
    the media types whose walk was complete and non-empty — only there is a
    title that went unlisted gone."""
    from src.database.models import LibraryEvent, LibraryItem
    now = now or datetime.utcnow()
    stamp = now.isoformat(timespec="seconds")
    counts = {"new": 0, "arrivals": 0, "gone": 0, "returned": 0, "grew": 0, "shrunk": 0}
    types = {a.get("media_type") for a in items.values()} | set(complete)
    with get_db_session() as db:
        known = {r.plex_rating_key: r for r in
                 db.query(LibraryItem).filter(LibraryItem.media_type.in_(sorted(t for t in types if t)))}
        added = {(e.plex_rating_key, e.bucket): e for e in
                 db.query(LibraryEvent).filter(LibraryEvent.kind == "added")}
        for rk, a in items.items():
            rk = str(rk)
            arrivals = a.get("arrivals") or {}
            first = min((v[0] for v in arrivals.values()), default=None)
            added_at = datetime.utcfromtimestamp(first) if first else None
            size_mb = round((a.get("size_bytes") or 0) / _MB, 2)
            title = (a.get("title") or "")[:512] or None
            row = known.get(rk)
            before_mb = None if row is None or row.gone_at is not None else (row.size_mb or 0)
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
                db.add(LibraryEvent(at=now, kind="returned", bucket=stamp, plex_rating_key=rk,
                                    media_type=row.media_type, title=title or row.title,
                                    size_mb=size_mb))
                counts["returned"] += 1
            row.media_type = a.get("media_type") or row.media_type
            row.title = title or row.title
            row.tmdb_id = a.get("tmdb_id") or row.tmdb_id
            row.tvdb_id = a.get("tvdb_id") or row.tvdb_id
            if added_at and (row.added_at is None or added_at < row.added_at):
                row.added_at = added_at
            row.size_mb = size_mb
            row.last_seen_at = now
            arrived_mb = 0.0      # what this walk's new arrivals explain of a size change
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


def velocity(start: datetime, end: datetime, types: Optional[list] = None) -> dict:
    """GB in vs GB out over ``[start, end)``, by month, with the biggest
    arrival and departure. Blocking — call via ``run_read``."""
    from src.database.models import LibraryEvent as E
    with get_db_session() as db:
        q = db.query(E.kind, E.at, E.size_mb, E.title, E.media_type).filter(
            E.at >= start, E.at < end)
        if types:
            q = q.filter(E.media_type.in_(list(types)))
        rows = q.all()
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
