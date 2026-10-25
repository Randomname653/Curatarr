"""Curatarr — the library ledger's arrivals, read from the arrs' own history.

The first real walk showed Plex's ``addedAt`` cannot date an arrival:
* a replaced file takes the new file's date (1091 of 1110 upgraded films), so
  every upgrade read as a new arrival plus a "shrunk" of the old size;
* a re-added library dates every file to the day of the re-add (15,135 anime
  episodes on one day), so a month showed 12 TB "new".

Radarr and Sonarr record every import with its size, and every replaced file
as a deletion with reason ``Upgrade``, in the same second as the import that
replaced it. This module walks that history (all of it once, then what came
since) into ``LibraryEvent`` rows with ``source="arr"``, one per history
record (``bucket`` = ``radarr:<history id>``, so a re-read writes nothing):

* an import is an ``added`` arrival with the file's size;
* an import paired with its ``Upgrade`` deletion is the net change only
  (``grew`` / ``shrunk`` by new minus old); the deletion row stays as the
  neutral ``replaced_used`` (an unpaired one is ``replaced``);
* a file deleted in the arr by hand is a neutral ``removed`` row; it counts
  as GB out only for the time before the Plex walk existed (before that the
  arr and Curatarr's own deletions are the only witnesses). After it,
  departures are the Plex walk's: it sees a title or a season leave however
  it left, and Sonarr drops the history of a deleted series;
* "MissingFromDisk" is never a departure: moved or re-mounted libraries
  produce tens of thousands in a day.

Once a service's history has been read to the end, its titles are "covered":
the Plex walk stops writing arrivals and upgrades for them (see
``library_ledger.persist_ledger``) and the arrivals it wrote before are
removed once. A covered title's ``LibraryItem.added_at`` is its first arr
import (``added_from="arr"``).
"""
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from src.database.connection import get_db_session

logger = logging.getLogger(__name__)

_MB = 1024 * 1024
_PAGE = 1000
_PAGES_PER_RUN = 60          # Sonarr's history on the real server: ~330 pages
_PAIR_WINDOW = timedelta(minutes=10)
_SINCE_OVERLAP = timedelta(minutes=5)
_IMPORT = "downloadFolderImported"
_DELETED = ("movieFileDeleted", "episodeFileDeleted")
# Neutral kinds: not in library_ledger.IN_KINDS / OUT_KINDS.
REPLACED, REPLACED_USED, UPGRADE = "replaced", "replaced_used", "upgrade"
# A file deleted in the arr by hand (or through its API): a departure, but
# counted only for the time before the Plex walk witnessed departures
# (library_ledger.velocity). "MissingFromDisk" is not one: on the real server
# 32,352 episodes "went missing" on one day (a moved library) and came back
# without an import.
REMOVED = "removed"
_REMOVED_REASONS = ("Manual", "ManualOverride")
# State generation: 2 = the history read once more for the removals.
_STATE_V = 2
SERVICES = ("radarr", "sonarr")


def _state_key(svc: str) -> str:
    return f"arr_ledger:{svc}"


def _covered_key(svc: str) -> str:
    return f"arr_ledger_covered:{svc}"


def _parse_at(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _size_mb(rec: dict) -> float:
    try:
        return round(int((rec.get("data") or {}).get("size") or 0) / _MB, 2)
    except (TypeError, ValueError):
        return 0.0


# ── what the arr knows about its titles ─────────────────────────────────────

def title_maps(svc: str, listing: list) -> dict:
    """``{arr id: {ext, title, media_type}}`` from the arr's movie / series
    listing: the TMDB id of a film, the TVDB id of a series."""
    from src.services.arr_client import classify_sonarr_category
    out = {}
    for x in listing or []:
        if not isinstance(x, dict) or x.get("id") is None:
            continue
        if svc == "radarr":
            out[x["id"]] = {"ext": x.get("tmdbId"), "title": x.get("title"), "media_type": "movie"}
        else:
            out[x["id"]] = {"ext": x.get("tvdbId"), "title": x.get("title"),
                            "media_type": classify_sonarr_category(x)}
    return out


def resolve(svc: str, rec: dict, titles: dict, keys: dict) -> Optional[dict]:
    """One history record as a ledger row, or None for a record the ledger
    does not keep. ``keys``: the LibraryItem rating key by external id."""
    et = rec.get("eventType")
    reason = (rec.get("data") or {}).get("reason")
    if et != _IMPORT and not (et in _DELETED and reason in ("Upgrade",) + _REMOVED_REASONS):
        return None
    at = _parse_at(rec.get("date"))
    if at is None or rec.get("id") is None:
        return None
    arr_id = rec.get("movieId") if svc == "radarr" else rec.get("seriesId")
    t = titles.get(arr_id) or {}
    rk = keys.get(t.get("ext")) if t.get("ext") else None
    entity = (f"radarr:{rec.get('movieId')}" if svc == "radarr"
              else f"sonarr:ep:{rec.get('episodeId')}")
    return {"ref": f"{svc}:{rec['id']}", "at": at, "entity": entity,
            "deleted": et != _IMPORT, "removed": reason in _REMOVED_REASONS,
            "size_mb": _size_mb(rec),
            "rk": rk or f"arr:{svc}:{arr_id}", "linked": rk is not None,
            "media_type": t.get("media_type") or ("movie" if svc == "radarr" else "show"),
            "title": (t.get("title") or rec.get("sourceTitle") or "")[:512] or None}


def _item_keys(svc: str) -> dict:
    """LibraryItem rating keys by TMDB id (films) or TVDB id (series)."""
    from src.database.models import LibraryItem as I
    with get_db_session() as db:
        if svc == "radarr":
            q = db.query(I.tmdb_id, I.plex_rating_key).filter(I.media_type == "movie", I.tmdb_id.isnot(None))
        else:
            q = db.query(I.tvdb_id, I.plex_rating_key).filter(I.media_type.in_(("show", "anime")),
                                                               I.tvdb_id.isnot(None))
        return {ext: rk for ext, rk in q}


# ── writing ─────────────────────────────────────────────────────────────────

def apply(rows: list, now: Optional[datetime] = None) -> dict:
    """Write resolved rows in history order. Blocking — call via
    ``run_write``. A ref already written is skipped, so a page read twice
    changes nothing. Each row is flushed before the next is read: the two
    halves of an upgrade usually sit in one batch (the session does not
    autoflush, and on the real history a batch-blind lookup paired 1 of
    1663 upgrades), and they pair across two pages as well."""
    from src.database.models import LibraryEvent as E, LibraryItem as I
    now = now or datetime.utcnow()
    counts = {"added": 0, "upgrades": 0, "replaced": 0, "removed": 0, "skipped": 0}
    rows = sorted(rows, key=lambda r: (r["at"], r["ref"]))
    with get_db_session() as db:
        refs = [r["ref"] for r in rows]
        have = set()
        for i in range(0, len(refs), 500):
            have |= {b for (b,) in db.query(E.bucket).filter(E.source == "arr", E.bucket.in_(refs[i:i + 500]))}
        for r in rows:
            if r["ref"] in have:
                counts["skipped"] += 1
                continue
            have.add(r["ref"])
            lo, hi = r["at"] - _PAIR_WINDOW, r["at"] + _PAIR_WINDOW
            ev = E(at=r["at"], bucket=r["ref"], plex_rating_key=r["rk"], media_type=r["media_type"],
                   title=r["title"], size_mb=r["size_mb"], source="arr", entity=r["entity"],
                   recorded_at=now)
            if r.get("removed"):
                ev.kind = REMOVED
                counts["removed"] += 1
                db.add(ev)
                continue
            if r["deleted"]:
                # The file an upgrade replaced: its import may already be here.
                imp = (db.query(E).filter(E.source == "arr", E.entity == r["entity"], E.kind == "added",
                                          E.at >= lo, E.at <= hi).order_by(E.at).first())
                if imp is not None:
                    _net(imp, imp.size_mb - r["size_mb"])
                    ev.kind = REPLACED_USED
                    counts["upgrades"] += 1
                else:
                    ev.kind = REPLACED
                    counts["replaced"] += 1
                db.add(ev)
                db.flush()      # the next row of this batch may be its import
                continue
            old = (db.query(E).filter(E.source == "arr", E.entity == r["entity"], E.kind == REPLACED,
                                      E.at >= lo, E.at <= hi).order_by(E.at).first())
            if old is not None:
                old.kind = REPLACED_USED
                ev.kind = "added"          # _net turns it into the difference
                _net(ev, r["size_mb"] - (old.size_mb or 0))
                counts["upgrades"] += 1
            else:
                ev.kind = "added"
                counts["added"] += 1
                if r["linked"]:
                    item = db.query(I).filter(I.plex_rating_key == r["rk"]).first()
                    if item is not None and (item.added_from != "arr" or item.added_at is None
                                             or r["at"] < item.added_at):
                        item.added_at, item.added_from = r["at"], "arr"
            db.add(ev)
            db.flush()
        db.commit()
    return counts


def _net(ev, delta_mb: float) -> None:
    """An import that replaced a file: only the difference is GB in or out."""
    delta_mb = round(delta_mb or 0.0, 2)
    ev.kind = UPGRADE if abs(delta_mb) < 1 else ("grew" if delta_mb > 0 else "shrunk")
    ev.size_mb = abs(delta_mb)


def remove_plex_arrivals(svc: str, ext_ids: set) -> int:
    """The Plex walk's arrivals and size changes of the titles this service
    now covers — written from addedAt before the arr history was read.
    Departures (``gone``) stay. Blocking — call via ``run_write``."""
    from src.database.models import LibraryEvent as E, LibraryItem as I
    if not ext_ids:
        return 0
    with get_db_session() as db:
        if svc == "radarr":
            q = db.query(I.plex_rating_key, I.tmdb_id).filter(I.media_type == "movie")
        else:
            q = db.query(I.plex_rating_key, I.tvdb_id).filter(I.media_type.in_(("show", "anime")))
        rks = [rk for rk, ext in q if ext in ext_ids]
        n = 0
        for i in range(0, len(rks), 500):
            n += (db.query(E).filter(E.source == "plex", E.kind != "gone",
                                     E.plex_rating_key.in_(rks[i:i + 500]))
                  .delete(synchronize_session=False))
        db.commit()
    return n


def covered() -> dict:
    """``{"movie": {tmdb ids}, "tv": {tvdb ids}}`` of the services whose
    history has been read to the end. Blocking — call via ``run_read``."""
    from src.services.app_state import get_state
    out = {"movie": set(), "tv": set()}
    for svc, kind in (("radarr", "movie"), ("sonarr", "tv")):
        st = _load(get_state(_state_key(svc)))
        # A second read for the removals keeps the coverage of the first.
        if st.get("done") or st.get("reread"):
            out[kind] = set(_load(get_state(_covered_key(svc)), []))
    return out


def _load(raw, default=None):
    try:
        return json.loads(raw) if raw else ({} if default is None else default)
    except (TypeError, ValueError):
        return {} if default is None else default


# ── reading the history ─────────────────────────────────────────────────────

def _client(svc: str):
    from src.config import settings
    from src.services.arr_client import RadarrClient, SonarrClient
    if svc == "radarr":
        if settings.RADARR_URL and settings.RADARR_API_KEY:
            return RadarrClient(settings.RADARR_URL, settings.RADARR_API_KEY)
    elif settings.SONARR_URL and settings.SONARR_API_KEY:
        return SonarrClient(settings.SONARR_URL, settings.SONARR_API_KEY)
    return None


async def walk(svc: str, client, pages: int = _PAGES_PER_RUN, since_only: bool = False) -> dict:
    """Advance one service: the whole history oldest first, ``pages`` pages
    per call, then what came since the newest record read. True ``done``
    once the history has been read to the end."""
    from src.database.runner import run_read, run_write
    from src.services.app_state import get_state, set_state
    st = _load(await run_read(get_state, _state_key(svc)))
    if st.get("done") and int(st.get("v") or 1) < _STATE_V:
        # Read before the removals were kept: once more from the start (the
        # refs make every record already written a no-op).
        st.update(page=1, done=False, reread=True)
    st["v"] = _STATE_V
    if since_only and not st.get("done"):
        return {"done": False, "skipped": "history not read to the end yet"}
    listing = await (client.get_movies() if svc == "radarr" else client.get_series())
    titles = title_maps(svc, listing)
    keys = await run_read(_item_keys, svc)
    total = {"added": 0, "upgrades": 0, "replaced": 0, "removed": 0, "skipped": 0, "pages": 0}
    newest = _parse_at(st.get("since")) if st.get("since") else None

    async def take(records):
        nonlocal newest
        rows = [r for r in (resolve(svc, x, titles, keys) for x in records) if r]
        for x in records:
            at = _parse_at(x.get("date"))
            if at and (newest is None or at > newest):
                newest = at
        got = await run_write(apply, rows)
        for k, v in got.items():
            total[k] += v

    if st.get("done"):
        since = (newest or datetime.utcnow()) - _SINCE_OVERLAP
        recs = await client.request("GET", "/api/v3/history/since",
                                    params={"date": since.strftime("%Y-%m-%dT%H:%M:%SZ")}, timeout=120)
        await take(recs if isinstance(recs, list) else [])
        total["pages"] = 1
    else:
        page = int(st.get("page") or 1)
        for _ in range(pages):
            d = await client.request("GET", "/api/v3/history", timeout=120, params={
                "page": page, "pageSize": _PAGE, "sortKey": "date", "sortDirection": "ascending"})
            recs = (d or {}).get("records") or []
            await take(recs)
            total["pages"] += 1
            if len(recs) < _PAGE:
                st["done"] = True
                break
            page += 1
        st["page"] = page
    st["since"] = newest.isoformat() if newest else st.get("since")
    st["done"] = bool(st.get("done"))
    if st["done"]:
        st.pop("reread", None)
        ext = sorted({t["ext"] for t in titles.values() if t.get("ext")})
        await run_write(set_state, _covered_key(svc), json.dumps(ext))
        if not st.get("cleaned"):
            removed = await run_write(remove_plex_arrivals, svc, set(ext))
            st["cleaned"] = True
            logger.info("[arr-ledger] %s: history read to the end; %d arrival row(s) the Plex "
                        "walk had dated by addedAt removed", svc, removed)
    await run_write(set_state, _state_key(svc), json.dumps(st))
    total["done"] = st["done"]
    return total


async def run_arr_ledger(task=None, since_only: bool = False) -> bool:
    """The custodian job (and the tech walk's catch-up, ``since_only``): each
    configured arr advanced once. True when every one is read to the end."""
    from src.services.task_monitor import task_monitor
    done = True
    for svc in SERVICES:
        client = _client(svc)
        if client is None:
            continue
        try:
            async with client:
                res = await walk(svc, client, since_only=since_only)
        except Exception as e:  # noqa: BLE001 - one arr down never stops the other
            logger.warning("[arr-ledger] %s history not read this run: %s", svc, e)
            done = False
            continue
        if task is not None:
            task_monitor.update(task, message=f"{svc}: {res.get('added', 0)} arrivals, "
                                              f"{res.get('upgrades', 0)} upgrades")
        if res.get("pages"):
            logger.info("[arr-ledger] %s: %s", svc, res)
        done = done and bool(res.get("done"))
    return done
