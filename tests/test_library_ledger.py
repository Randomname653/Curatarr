"""The library ledger: what arrived when, and what left.

Nothing recorded either side of the balance before: GB freed was read off
Curatarr's own deletions only (a title deleted straight in Radarr never
counted) and GB added was not known at all. The tech sync's whole-library
walk now feeds two durable tables — arrivals back-dated from Plex
``addedAt``, departures when a complete walk no longer lists a title.

    python tests/test_library_ledger.py
"""
import asyncio
import calendar
import sys
import types
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.services.library_ledger as ll  # noqa: E402
import src.services.plex_sync as ps  # noqa: E402
from src.database.models import Base, LibraryConfig, LibraryEvent, LibraryItem, MediaTechProfile  # noqa: E402

from _checks import check  # noqa: E402


engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
Base.metadata.create_all(engine)
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


ll.get_db_session = fake_db_session
GB = 1024 ** 3


def ts(y, m, d):
    return calendar.timegm(datetime(y, m, d, 12).utctimetuple())


def walk(files):
    """files: [(rating_key, media_type, title, addedAt, bytes)] → the tech
    sync's accumulator shape."""
    agg = {}
    for rk, mt, title, added, size in files:
        a = agg.setdefault(rk, {"media_type": mt, "title": title, "tmdb_id": None,
                                "tvdb_id": None, "size_bytes": 0, "arrivals": {}})
        ll.note_arrival(a["arrivals"], added, size)
        a["size_bytes"] += size
    return agg


def events(kind=None):
    with fake_db_session() as db:
        q = db.query(LibraryEvent)
        if kind:
            q = q.filter(LibraryEvent.kind == kind)
        return sorted((e.plex_rating_key, e.kind, e.bucket, round(e.size_mb / 1024, 2))
                      for e in q)


def item(rk):
    with fake_db_session() as db:
        r = db.query(LibraryItem).filter_by(plex_rating_key=rk).one()
        return types.SimpleNamespace(added_at=r.added_at, gone_at=r.gone_at,
                                     size_gb=round(r.size_mb / 1024, 2))


FILM = ("10", "movie", "Heat", ts(2026, 1, 5), 4 * GB)
S_MAR = [("20", "show", "Severance", ts(2026, 3, 1), GB), ("20", "show", "Severance", ts(2026, 3, 8), GB)]
S_APR = [("20", "show", "Severance", ts(2026, 4, 2), GB)]
S_MAY = [("20", "show", "Severance", ts(2026, 5, 3), GB)]
BOTH = {"movie", "show"}
T1, T2, T3 = datetime(2026, 4, 10), datetime(2026, 5, 10), datetime(2026, 6, 10)

# ── 1. the first walk back-dates the library's arrivals ─────────────────────
ll.persist_ledger(walk([FILM] + S_MAR + S_APR), BOTH, now=T1)
check("one arrival per title and month, sized by the files of that month",
      events("added") == [("10", "added", "2026-01", 4.0), ("20", "added", "2026-03", 2.0),
                          ("20", "added", "2026-04", 1.0)])
check("a series arrived with its first episode", item("20").added_at == datetime(2026, 3, 1, 12))
v = ll.velocity(datetime(2026, 1, 1), datetime(2026, 6, 1))
check("GB in by month, from the first walk on",
      v["gb_in"] == 7.0 and v["gb_out"] == 0 and v["by_month"]["2026-03"] == {"in": 2.0, "out": 0.0}
      and v["biggest_in"]["title"] == "Heat")
ll.persist_ledger(walk([FILM] + S_MAR + S_APR), BOTH, now=T1)
check("the same walk again writes nothing new", len(events()) == 3)

# ── 2. a title deleted anywhere is gone; new episodes are arrivals ──────────
ll.persist_ledger(walk(S_MAR + S_APR + S_MAY), BOTH, now=T2)
check("a title a complete walk no longer lists is gone, with its last size",
      events("gone") == [("10", "gone", "2026-05-10T00:00:00", 4.0)] and item("10").gone_at == T2)
check("a new episode is that month's arrival, not a size change",
      ("20", "added", "2026-05", 1.0) in events() and not events("grew"))
v = ll.velocity(datetime(2026, 5, 1), datetime(2026, 6, 1))
check("the month's balance: 1 GB in, 4 GB out", v["gb_in"] == 1.0 and v["gb_out"] == 4.0
      and v["net_gb"] == -3.0 and v["biggest_out"]["title"] == "Heat")

# ── 3. a deleted season shrinks a title that stays ──────────────────────────
ll.persist_ledger(walk(S_APR + S_MAY), BOTH, now=T3)
check("a deleted season is a shrink of its size",
      events("shrunk") == [("20", "shrunk", "2026-06-10T00:00:00", 2.0)])
check("...and the arrival it once was stays as it was", ("20", "added", "2026-03", 2.0) in events())
check("a series keeps the date it first arrived", item("20").added_at == datetime(2026, 3, 1, 12))

# ── 4. an incomplete walk empties nothing ───────────────────────────────────
ll.persist_ledger(walk([]), set(), now=datetime(2026, 6, 11))
check("a cut-short walk never reads as an emptied library", item("20").gone_at is None)

# ── 5. back again, and upgraded ─────────────────────────────────────────────
T4 = datetime(2026, 7, 1)
up = [("20", "show", "Severance", ts(2026, 4, 2), 4 * GB)] + S_MAY
ll.persist_ledger(walk([FILM] + up), BOTH, now=T4)
check("a gone title listed again has returned", events("returned") == [
    ("10", "returned", "2026-07-01T00:00:00", 4.0)] and item("10").gone_at is None)
check("a file replaced by a bigger one grew by the difference",
      events("grew") == [("20", "grew", "2026-07-01T00:00:00", 3.0)])
v = ll.velocity(datetime(2026, 7, 1), datetime(2026, 8, 1))
check("returns and upgrades count as GB in", v["gb_in"] == 7.0 and v["gb_out"] == 0)
ll.persist_ledger(walk([("10", "movie", "Heat", ts(2025, 11, 1), 4 * GB)] + up), BOTH,
                  now=datetime(2026, 7, 2))
check("an earlier addedAt moves the arrival date back", item("10").added_at == datetime(2025, 11, 1, 12))
ll.persist_ledger(walk([("10", "movie", "Heat", ts(2026, 6, 1), 4 * GB)] + up), BOTH,
                  now=datetime(2026, 7, 3))
check("...a later one never moves it forward", item("10").added_at == datetime(2025, 11, 1, 12))
check("a file without addedAt is no arrival", not ll.note_arrival({}, None, GB))

# ── 6. the tech sync feeds it ───────────────────────────────────────────────
with fake_db_session() as db:
    db.add(LibraryConfig(plex_section_key="1", plex_section_title="Movies",
                         plex_section_type="movie", media_category="movie"))

walked = [{"ratingKey": "30", "title": "Past Lives", "addedAt": ts(2026, 8, 1), "duration": 6_000_000,
           "Media": [{"duration": 6_000_000, "Part": [{"size": 2 * GB}]}]}]


class FakeResp:
    status_code = 200

    def __init__(self, items):
        self._items = items

    def json(self):
        return {"MediaContainer": {"Metadata": self._items}}


class FakeClient:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        return FakeResp(walked if url.endswith("/sections/1/all") else [])


state: dict = {}
ps.settings = types.SimpleNamespace(effective_plex_url="http://plex",
                                    effective_plex_token="tok", PLEX_CLIENT_ID="test")
ps.get_db_session = fake_db_session
ps.httpx = types.SimpleNamespace(AsyncClient=FakeClient)
ps.get_datetime = lambda k: state.get(k)
ps.set_datetime = lambda k, v: state.__setitem__(k, v)
asyncio.run(ps.sync_tech_profiles(force=True))
with fake_db_session() as db:
    prof = db.query(MediaTechProfile).filter_by(plex_rating_key="30").count()
check("the tech walk writes the ledger next to the profiles",
      prof == 1 and ("30", "added", "2026-08", 2.0) in events())
check("...and, being complete, marks the films it no longer lists as gone",
      item("10").gone_at is not None)

check.done()
