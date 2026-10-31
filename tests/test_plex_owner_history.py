"""Plex sync: every play of the owner is its own row, and a rewatch in
progress is not a completion.

The section listing carries one timestamp per item (lastViewedAt). Two plays
of a film between two syncs became one row, the owner's history from before
the first sync was one row per item ever watched, and every listed item was
written as finished. The wrap-up's rewatches, streaks, baselines and
completion rate were measured on that. The owner's plays now also come from
/status/sessions/history/all (accountID 1), walked from the start once, and
a listed item with a resume point and no finished view at its timestamp is a
rewatch in progress.

Runs ``sync_plex_history`` against an in-memory SQLite DB and a fake Plex.

    python tests/test_plex_owner_history.py
"""
import asyncio
import calendar
import re
import sys
import types
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.services.plex_sync as ps
import src.services.taste_engine as te
from src.database.models import Base, LibraryConfig, User, WatchHistoryEntry

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


with fake_db_session() as _db:
    _db.add(User(plex_user_id="1001", plex_username="owner", is_admin=True))
    _db.add(LibraryConfig(plex_section_key="1", plex_section_title="Movies",
                          plex_section_type="movie", media_category="movie"))
    _db.add(LibraryConfig(plex_section_key="2", plex_section_title="TV Shows",
                          plex_section_type="show", media_category="show"))
with fake_db_session() as _db:
    OWNER_ID = _db.query(User).one().id

T0 = datetime(2026, 10, 20, 20, 0, 0)
clock = {"now": T0}


class FakeDatetime(datetime):
    @classmethod
    def utcnow(cls):
        return clock["now"]


def _ts(dt):
    return calendar.timegm(dt.utctimetuple())


def film_event(rk, at, section=1):
    return {"ratingKey": rk, "viewedAt": at, "type": "movie", "title": f"Film {rk}",
            "librarySectionID": section, "accountID": 1}


state: dict = {}
LAST = _ts(T0 - timedelta(days=1))
# What the listing answers: (ratingKey, lastViewedAt, viewOffset).
listing = [("500", LAST, None)]
# The owner's play history: the listed play (seconds apart from the
# listing), an earlier rewatch of the same film, an episode of a show the
# listing no longer carries, a play in a library Curatarr does not follow,
# and a member's play (not the owner's).
history = [
    film_event("500", LAST + 20),
    film_event("500", _ts(T0 - timedelta(days=10))),
    {"ratingKey": "650", "viewedAt": _ts(T0 - timedelta(days=3)), "type": "episode",
     "title": "Pilot", "grandparentTitle": "Gone Show", "grandparentKey": "/library/metadata/649",
     "parentIndex": 1, "index": 1, "librarySectionID": 2, "accountID": 1},
    # Plex answers "title": null for a play whose item is gone (seen live).
    {"ratingKey": "660", "viewedAt": _ts(T0 - timedelta(days=5)), "type": "episode",
     "title": None, "grandparentTitle": "Gone Show", "grandparentKey": "/library/metadata/649",
     "parentIndex": 1, "index": 2, "librarySectionID": 2, "accountID": 1},
    film_event("777", _ts(T0 - timedelta(days=4)), section=9),
    dict(film_event("500", _ts(T0 - timedelta(days=6))), accountID=2002),
]
history_calls: list = []
history_fails = {"on": False}
rec_hits: list = []


class FakeResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        params = params or {}
        m = re.search(r"/library/sections/(\d+)/all$", url)
        if m and "viewCount>>" in params:
            since = int(params.get("lastViewedAt>>", 0))
            rows = []
            if m.group(1) == "1":
                for rk, at, off in listing:
                    if at >= since:
                        row = {"ratingKey": rk, "lastViewedAt": at, "title": f"Film {rk}",
                               "type": "movie", "duration": 6_000_000,
                               "Genre": [{"tag": "Drama"}]}
                        if off:
                            row["viewOffset"] = off
                        rows.append(row)
            return FakeResp(200, {"MediaContainer": {"Metadata": rows}})
        if url.endswith("/accounts"):
            return FakeResp(200, {"MediaContainer": {"Account": [
                {"id": 1, "name": "owner"}, {"id": 2002, "name": "stranger"}]}})
        if url.endswith("/status/sessions/history/all"):
            history_calls.append(dict(params))
            if history_fails["on"]:
                return FakeResp(500)
            since = int(params.get("viewedAt>>", 0))
            return FakeResp(200, {"MediaContainer": {"Metadata": [
                e for e in history if e["viewedAt"] >= since]}})
        return FakeResp(404)


async def _noop(*a, **kw):
    return None


ps.settings = types.SimpleNamespace(effective_plex_url="http://plex",
                                    effective_plex_token="tok", PLEX_CLIENT_ID="test")
ps.get_db_session = fake_db_session
ps.get_datetime = lambda k: state.get(k)
ps.set_datetime = lambda k, v: state.__setitem__(k, v)
ps.get_state = lambda k: state.get(k)
ps.set_state = lambda k, v: state.__setitem__(k, v)
ps.acquire_state_lock = lambda k: True
ps.release_state_lock = lambda k: None
ps.httpx = types.SimpleNamespace(AsyncClient=FakeClient)
ps.datetime = FakeDatetime
ps._sync_music_ratings = _noop
ps._process_rec_watch_hits = lambda hits: rec_hits.extend(hits)
te.compute_all_taste_vectors = _noop


def owner_rows():
    with fake_db_session() as db:
        return sorted(((r.plex_item_id, _ts(r.viewed_at), r.completed, r.duration_ms,
                        r.genres, r.view_offset_ms)
                       for r in db.query(WatchHistoryEntry).filter_by(user_id=OWNER_ID)),
                      key=lambda r: (r[0], r[1]))


def sync():
    asyncio.run(ps.sync_plex_history(force=True))


# ── 1. a fresh install: every play of the owner, once ───────────────────────
sync()
rows = owner_rows()
films = [r for r in rows if r[0] == "500"]
check("both plays of the film are rows: the listed one and the earlier rewatch",
      [r[1] for r in films] == [_ts(T0 - timedelta(days=10)), LAST])
check("the history event seconds away from the listing is the same play, not a third row",
      len(films) == 2)
check("the earlier rewatch borrows duration and genres from the listing",
      films[0][2] is True and films[0][3] == 6_000_000 and films[0][4] == "Drama")
check("an episode only the history knows arrives as the owner's play",
      [r[0] for r in rows if r[0] == "650"] == ["650"])
check("a play whose item is gone (title null) is kept, and does not sink the sync",
      [r[0] for r in rows if r[0] == "660"] == ["660"])
check("a library Curatarr does not follow stays out", "777" not in [r[0] for r in rows])
check("another account's play is not the owner's",
      _ts(T0 - timedelta(days=6)) not in [r[1] for r in films])
check("a complete walk records the owner's backfill",
      state.get(ps.OWNER_BACKFILL_KEY) == "1")

# ── 2. later syncs are incremental and double nothing ───────────────────────
history_calls.clear()
clock["now"] = T0 + timedelta(hours=3)
REPLAY = _ts(T0 + timedelta(hours=1))
listing[0] = ("500", _ts(T0 + timedelta(hours=2)), None)
history.append(film_event("500", REPLAY))                       # a play between two syncs
history.append(film_event("500", _ts(T0 + timedelta(hours=2)) + 5))  # the listed one
sync()
films = [r for r in owner_rows() if r[0] == "500"]
check("the next walk starts at the last sync, not at the beginning",
      history_calls and "viewedAt>>" in history_calls[0])
check("two plays between two syncs are two rows (the listing only had the last)",
      REPLAY in [r[1] for r in films] and len(films) == 4)
sync()
check("running the sync again adds nothing", len([r for r in owner_rows() if r[0] == "500"]) == 4)

# ── 3. an install that synced before: its history is recovered once ─────────
with fake_db_session() as db:
    db.query(WatchHistoryEntry).delete()
    db.add(WatchHistoryEntry(user_id=OWNER_ID, plex_user_id="1", plex_item_id="500",
                             title="Film 500", media_type="movie", viewed_at=datetime.utcfromtimestamp(LAST),
                             duration_ms=6_000_000, view_offset_ms=6_000_000, completed=True,
                             genres="Drama"))
state.clear()
state["last_sync_at"] = T0 + timedelta(hours=3)
state[ps.MEMBER_BACKFILL_KEY] = "1"
history_calls.clear()
rec_hits.clear()
listing[:] = []                     # nothing new in the listing
sync()
films = [r for r in owner_rows() if r[0] == "500"]
check("the first sync after the change walks the whole history even with an empty listing",
      history_calls and "viewedAt>>" not in history_calls[0])
check("the stored play is matched by its seconds and not duplicated",
      [r[1] for r in films].count(LAST) == 1 and LAST + 20 not in [r[1] for r in films])
check("the older plays come back from the history, with the stored metadata",
      _ts(T0 - timedelta(days=10)) in [r[1] for r in films]
      and all(r[3] == 6_000_000 and r[4] == "Drama" for r in films))
check("recovered old plays are not news for the watched-a-recommendation follow-up",
      not [h for h in rec_hits if h["viewed_at"] < (T0 + timedelta(hours=2)).isoformat()])
check("...and the backfill is recorded", state.get(ps.OWNER_BACKFILL_KEY) == "1")

# ── 4. a rewatch in progress is a partial play, then one finished view ──────
state["last_sync_at"] = clock["now"]
clock["now"] = T0 + timedelta(days=1)
PART = _ts(T0 + timedelta(hours=20))
listing[:] = [("500", PART, 2_400_000)]      # resume point, no finished view at PART
sync()
part = [r for r in owner_rows() if r[0] == "500" and r[1] == PART]
check("a watched film listed with a resume point and no finished view is a partial play",
      len(part) == 1 and part[0][2] is False and part[0][5] == 2_400_000)
before = len([r for r in owner_rows() if r[0] == "500"])
state["last_sync_at"] = clock["now"]
clock["now"] = T0 + timedelta(days=1, hours=3)
DONE = _ts(T0 + timedelta(days=1, hours=2))
listing[:] = [("500", DONE, None)]
history.append(film_event("500", DONE + 3))
sync()
films = [r for r in owner_rows() if r[0] == "500"]
check("finishing it turns the partial row into the finished view — one viewing, one row",
      len(films) == before and PART not in [r[1] for r in films]
      and [r[2] for r in films if r[1] == DONE] == [True])

# ── 5. a cut-short history walk decides nothing ─────────────────────────────
state["last_sync_at"] = clock["now"]
state.pop(ps.OWNER_BACKFILL_KEY, None)
clock["now"] = T0 + timedelta(days=2)
LATE = _ts(T0 + timedelta(days=1, hours=20))
listing[:] = [("500", LATE, 1_000_000)]
history_fails["on"] = True
sync()
late = [r for r in owner_rows() if r[0] == "500" and r[1] == LATE]
check("without the history a resume point is not read as a partial play",
      [r[2] for r in late] == [True])
check("...and the backfill is not recorded, so it runs again",
      state.get(ps.OWNER_BACKFILL_KEY) is None)

check.done()
