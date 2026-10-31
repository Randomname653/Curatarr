"""The ledger's arrivals from the arrs' own history (2026-10-25).

The first real walk showed Plex's addedAt cannot date an arrival: a replaced
file takes the new file's date (1091 of 1110 upgraded films), and a re-added
library dates every file to the day of the re-add (15,135 anime episodes on
one day: "12 TB new" in a month). Radarr and Sonarr record each import with
its size, and each replaced file as an "Upgrade" deletion in the same second.

Fakes only: an in-memory database and a scripted arr, no network.

    python tests/test_arr_ledger.py
"""
import asyncio
import calendar
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy.orm import sessionmaker  # noqa: E402

import src.services.app_state as app_state  # noqa: E402
import src.services.arr_ledger as al  # noqa: E402
import src.services.library_ledger as ll  # noqa: E402
from src.database.models import Base, LibraryEvent, LibraryItem  # noqa: E402

from _checks import check  # noqa: E402


from _checks import memory_engine, with_users  # noqa: E402
engine = memory_engine()
Base.metadata.create_all(engine)
with_users(engine, 1, 2)
Session = sessionmaker(bind=engine, autoflush=False)


@contextmanager
def fake_db_session():
    db = Session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


state: dict = {}
al.get_db_session = ll.get_db_session = fake_db_session
app_state.get_state = lambda k: state.get(k)
app_state.set_state = lambda k, v: state.__setitem__(k, v) or True

GB = 1024 ** 3
al._PAGE = 2          # the real page is 1000 records
MOVIES = [{"id": 1, "tmdbId": 949, "title": "Heat"}, {"id": 2, "tmdbId": 680, "title": "Pulp Fiction"},
          {"id": 3, "tmdbId": 77, "title": "Memento"}]


def rec(hid, date, et, movie_id, size, reason=None):
    data = {"size": str(size)}
    if reason:
        data["reason"] = reason
    return {"id": hid, "date": date, "eventType": et, "movieId": movie_id, "sourceTitle": "x", "data": data}


def events(source=None, kind=None):
    with fake_db_session() as db:
        q = db.query(LibraryEvent)
        if source:
            q = q.filter(LibraryEvent.source == source)
        if kind:
            q = q.filter(LibraryEvent.kind == kind)
        return sorted((e.plex_rating_key, e.kind, round((e.size_mb or 0) / 1024, 2)) for e in q)


def item(rk):
    with fake_db_session() as db:
        r = db.query(LibraryItem).filter_by(plex_rating_key=rk).one()
        return r.added_at, r.added_from


class FakeArr:
    """Radarr's history, ascending pages of ``page_size`` (the real one: 1000)."""

    def __init__(self, records, page_size=2):
        self.records, self.page_size, self.calls = records, page_size, []

    async def get_movies(self):
        return MOVIES

    async def request(self, method, endpoint, params=None, timeout=None):
        self.calls.append((endpoint, dict(params or {})))
        recs = sorted(self.records, key=lambda r: (r["date"], r["id"]))
        if endpoint.endswith("/since"):
            since = params["date"]
            return [r for r in recs if r["date"] >= since]
        p = int(params["page"])
        return {"records": recs[(p - 1) * self.page_size:p * self.page_size]}


# The library as the Plex walk knew it: Heat (rk 10), Pulp Fiction (rk 11).
# Its addedAt arrivals, written before the arr history was read: Heat dated
# by an upgrade's new file, Pulp Fiction by a re-added library.
with fake_db_session() as db:
    db.add(LibraryItem(plex_rating_key="10", media_type="movie", title="Heat", tmdb_id=949,
                       added_at=datetime(2026, 6, 23), size_mb=6 * 1024, last_seen_at=datetime(2026, 9, 1)))
    db.add(LibraryItem(plex_rating_key="11", media_type="movie", title="Pulp Fiction", tmdb_id=680,
                       added_at=datetime(2026, 6, 23), size_mb=10 * 1024, last_seen_at=datetime(2026, 9, 1)))
    db.add(LibraryEvent(at=datetime(2026, 6, 23), kind="added", bucket="2026-06", plex_rating_key="10",
                        media_type="movie", title="Heat", size_mb=6 * 1024, source="plex"))
    db.add(LibraryEvent(at=datetime(2026, 6, 23), kind="shrunk", bucket="t", plex_rating_key="10",
                        media_type="movie", title="Heat", size_mb=4 * 1024, source="plex"))
    db.add(LibraryEvent(at=datetime(2026, 6, 23), kind="added", bucket="2026-06", plex_rating_key="11",
                        media_type="movie", title="Pulp Fiction", size_mb=10 * 1024, source="plex"))
    db.add(LibraryEvent(at=datetime(2026, 8, 2), kind="gone", bucket="t2", plex_rating_key="12",
                        media_type="movie", title="Gone Film", size_mb=3 * 1024, source="plex"))
    db.commit()

HISTORY = [
    rec(1, "2026-02-01T10:00:00Z", "grabbed", 1, 4 * GB),                       # not kept
    rec(2, "2026-02-01T11:00:00Z", "downloadFolderImported", 1, 4 * GB),        # Heat arrives
    rec(3, "2026-03-05T09:00:00Z", "downloadFolderImported", 2, 10 * GB),       # Pulp Fiction arrives
    rec(4, "2026-06-23T20:00:00Z", "movieFileDeleted", 1, 4 * GB, "Upgrade"),   # Heat's old file...
    rec(5, "2026-06-23T20:00:00Z", "downloadFolderImported", 1, 6 * GB),        # ...and the better one
    rec(6, "2026-07-01T08:00:00Z", "movieFileDeleted", 2, 10 * GB, "Manual"),   # a departure: Plex's job
]

# ── one history record → one row ────────────────────────────────────────────
titles = al.title_maps("radarr", MOVIES)
keys = {949: "10", 680: "11"}
r = al.resolve("radarr", HISTORY[1], titles, keys)
check("an import is a row: the title's Plex key, the file's size, its own ref",
      r["rk"] == "10" and r["linked"] and r["size_mb"] == 4096.0 and r["ref"] == "radarr:2"
      and r["entity"] == "radarr:1" and r["at"] == datetime(2026, 2, 1, 11))
check("grabs are not kept; an Upgrade deletion is, and a hand deletion is a removal",
      al.resolve("radarr", HISTORY[0], titles, keys) is None
      and al.resolve("radarr", HISTORY[5], titles, keys)["removed"] is True
      and al.resolve("radarr", HISTORY[3], titles, keys)["deleted"] is True
      and not al.resolve("radarr", HISTORY[3], titles, keys)["removed"])
check("a file 'missing from disk' is no departure (seen live: 32,352 episodes on one day)",
      al.resolve("radarr", rec(99, "2026-04-17T10:00:00Z", "movieFileDeleted", 1, GB, "MissingFromDisk"),
                 titles, keys) is None)
check("a title Plex does not list keeps an arr key", al.resolve(
    "radarr", rec(9, "2026-01-01T00:00:00Z", "downloadFolderImported", 3, GB), titles, keys)["rk"] == "arr:radarr:3")

# ── walking the history ─────────────────────────────────────────────────────
arr = FakeArr(HISTORY, page_size=2)
res = asyncio.run(al.walk("radarr", arr, pages=2))
check("a budget of pages per run; not done while pages are full",
      res["pages"] == 2 and res["done"] is False and json.loads(state[al._state_key("radarr")])["page"] == 3)
check("the Plex walk's arrivals stay until the history is read to the end",
      ("10", "added", 6.0) in events("plex"))
res = asyncio.run(al.walk("radarr", arr, pages=5))
check("...then it is done, and its films are covered", res["done"] is True
      and al.covered()["movie"] == {949, 680, 77})
check("arrivals dated by the arr: Heat in February, Pulp Fiction in March",
      ("10", "added", 4.0) in events("arr") and ("11", "added", 10.0) in events("arr")
      and item("10") == (datetime(2026, 2, 1, 11), "arr") and item("11") == (datetime(2026, 3, 5, 9), "arr"))
check("an upgrade is only its difference: 2 GB grew, the replaced file neutral",
      ("10", "grew", 2.0) in events("arr") and ("10", "replaced_used", 4.0) in events("arr")
      and len(events("arr", "added")) == 2)
check("the Plex walk's addedAt arrivals and their upgrade shrink are removed; departures stay",
      events("plex") == [("12", "gone", 3.0)])
check("a departure in the arr is kept as a neutral removal, never as 'gone'",
      not events("arr", "gone") and ("11", "removed", 10.0) in events("arr"))

n = len(events())
asyncio.run(al.walk("radarr", arr, pages=5))
check("read again (overlap): nothing doubles", len(events()) == n)
check("once done, a run asks only for what came since",
      arr.calls[-1][0].endswith("/history/since"))

# An upgrade whose two halves land on two pages, import first.
arr2_hist = [rec(20, "2026-09-01T10:00:00Z", "downloadFolderImported", 2, 12 * GB),
             rec(21, "2026-09-01T10:00:00Z", "movieFileDeleted", 2, 10 * GB, "Upgrade")]
got = asyncio.run(asyncio.to_thread(al.apply, [al.resolve("radarr", arr2_hist[0], titles, keys)]))
got = asyncio.run(asyncio.to_thread(al.apply, [al.resolve("radarr", arr2_hist[1], titles, keys)]))
check("...and with the deletion read after its import, the import becomes the difference",
      ("11", "grew", 2.0) in events("arr") and got["upgrades"] == 1
      and item("11") == (datetime(2026, 3, 5, 9), "arr"))

# Both halves in one batch, the usual case (seen live: 1 of 1663 paired
# while a row was not flushed before the next one looked for it).
same = [rec(30, "2026-09-10T10:00:00Z", "movieFileDeleted", 3, 2 * GB, "Upgrade"),
        rec(31, "2026-09-10T10:00:00Z", "downloadFolderImported", 3, 5 * GB)]
got = asyncio.run(asyncio.to_thread(al.apply, [al.resolve("radarr", x, titles, keys) for x in same]))
check("an upgrade whose deletion and import arrive in one batch pairs too",
      got["upgrades"] == 1 and ("arr:radarr:3", "grew", 3.0) in events("arr")
      and not [e for e in events("arr", "added") if e[0] == "arr:radarr:3"])

# ── the Plex walk next to it ────────────────────────────────────────────────
cov = al.covered()


def walk_items(rk, tmdb, title, added, size_gb):
    return {rk: {"media_type": "movie", "title": title, "tmdb_id": tmdb, "tvdb_id": None,
                 "size_bytes": size_gb * GB,
                 "arrivals": {datetime.utcfromtimestamp(added).strftime("%Y-%m"): [added, size_gb * GB]}}}


def ts(y, m, d):
    return calendar.timegm(datetime(y, m, d, 12).utctimetuple())


before = set(events())
items = {}
items.update(walk_items("10", 949, "Heat", ts(2026, 6, 23), 6))           # addedAt of the new file
items.update(walk_items("11", 680, "Pulp Fiction", ts(2026, 9, 1), 12))   # upgraded since: arr explains it
items.update(walk_items("13", None, "Home Video", ts(2026, 10, 2), 1))    # no arr knows it
# The walks run after the arr rows were recorded (recorded_at = now).
WALK1 = datetime.utcnow() + timedelta(days=1)
ll.persist_ledger(items, {"movie"}, now=WALK1, arr=cov)
new = set(events()) - before
check("a covered film gets no addedAt arrival and no growth from the walk",
      not any(e[0] in ("10", "11") for e in new))
check("...a film no arr knows still arrives by addedAt", ("13", "added", 1.0) in new)
check("...and the arr's date of arrival is not moved by Plex", item("10") == (datetime(2026, 2, 1, 11), "arr"))
items["11"]["size_bytes"] = 4 * GB
ll.persist_ledger(items, {"movie"}, now=WALK1 + timedelta(days=1), arr=cov)
check("a covered title that loses size without an arr record shrank (a deleted season, a file gone)",
      ("11", "shrunk", 8.0) in set(events()))

v = ll.velocity(datetime(2026, 1, 1), WALK1 + timedelta(days=30), ["movie"])
check("the balance counts arrivals and differences, never the replaced files; the removal "
      "before the walk existed is GB out",
      v["gb_in"] == 4 + 10 + 2 + 2 + 3 + 1 and v["gb_out"] == 3 + 8 + 10)

# ── departures before the Plex walk existed ─────────────────────────────────
from src.database.models import DeletionProposal  # noqa: E402

START = ll.ledger_start(Session())
with fake_db_session() as db:
    db.add(DeletionProposal(user_id=1, title="Old Film", service="radarr", media_id="7", reason="x",
                            confidence=0.9, storage_mb=5 * 1024, status="deleted", category="movie",
                            resolved_at=datetime(2026, 4, 3, 12)))
    db.add(DeletionProposal(user_id=2, title="Old Film", service="radarr", media_id="7", reason="x",
                            confidence=0.9, storage_mb=5 * 1024, status="deleted", category="movie",
                            resolved_at=datetime(2026, 4, 3, 13)))
    db.add(LibraryEvent(at=datetime(2026, 4, 3, 12, 1), kind="removed", bucket="radarr:500", source="arr",
                        plex_rating_key="arr:radarr:7", media_type="movie", title="Old Film", size_mb=5 * 1024))
    db.add(LibraryEvent(at=START + timedelta(hours=1), kind="removed", bucket="radarr:501", source="arr",
                        plex_rating_key="arr:radarr:8", media_type="movie", title="After", size_mb=7 * 1024))
    db.commit()
april = ll.velocity(datetime(2026, 4, 1), datetime(2026, 5, 1), ["movie"])
check("before the walk: Curatarr's deletion counts once, its arr side and its second proposal not",
      april["gb_out"] == 5 and april["titles_out"] == 1 and april["biggest_out"]["title"] == "Old Film")
after = ll.velocity(START, START + timedelta(days=2), ["movie"])
check("after the walk began, a removal in the arr is left to the walk", after["gb_out"] == 0)

# ── a history read before removals were kept is read once more ──────────────
rows_before = sorted(events("arr"))
state[al._state_key("radarr")] = json.dumps({"page": 9, "done": True, "since": "2026-10-01T00:00:00",
                                             "cleaned": True})
arr3 = FakeArr(HISTORY, page_size=2)
asyncio.run(al.walk("radarr", arr3, pages=1))
st3 = json.loads(state[al._state_key("radarr")])
check("an old read starts again from the first page, and keeps its coverage meanwhile",
      arr3.calls[0][1].get("page") == 1 and st3["done"] is False and st3.get("reread")
      and al.covered()["movie"] == {949, 680, 77})
asyncio.run(al.walk("radarr", arr3, pages=10))
st3 = json.loads(state[al._state_key("radarr")])
check("...and when it is through, it is the new generation and nothing doubled",
      st3["done"] is True and st3["v"] == al._STATE_V and not st3.get("reread")
      and sorted(events("arr")) == rows_before)

# ── wiring ──────────────────────────────────────────────────────────────────
from src.services.data_custodian import _registry  # noqa: E402

job = next((t for t in _registry() if t.job_id == "arr_ledger"), None)
check("the custodian reads the history (no model)", job is not None and not job.needs_llm)
src = (ROOT / "src/services/plex_sync.py").read_text(encoding="utf-8")
check("the tech walk reads what the arrs recorded first, then writes the ledger with their coverage",
      "await arr_ledger.run_arr_ledger(since_only=True)" in src
      and src.index("run_arr_ledger(since_only=True)") < src.index("persist_ledger, agg, scanned - incomplete, None, arr_cov"))

check.done()
