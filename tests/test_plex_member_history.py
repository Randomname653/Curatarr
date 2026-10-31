"""Plex sync: a household member's plays reach watch history.

2026-10-21: the deletion judge proposed "Free Rein" as a CUT although a
household member had played it 14 times. The sync lists watched items with
the admin's token, and Plex answers "watched" for the token's account, so a
title only a member watched never appeared - none of that member's plays
since January reached Curatarr, and the household floor had nothing to go
on. Members' plays now come from their own play history
(/status/sessions/history/all?accountID=...): their whole history on the
first sync, then incrementally.

Runs ``sync_plex_history`` against an in-memory SQLite DB and a fake Plex.

    python tests/test_plex_member_history.py
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

from sqlalchemy.orm import sessionmaker

import src.services.plex_sync as ps
import src.services.taste_engine as te
from src.database.models import Base, LibraryConfig, User, WatchHistoryEntry

from _checks import check  # noqa: E402


from _checks import memory_engine  # noqa: E402
engine = memory_engine()
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


MEMBER = "2002"
with fake_db_session() as _db:
    _db.add(User(plex_user_id="1001", plex_username="owner", is_admin=True))
    _db.add(User(plex_user_id=MEMBER, plex_username="member", is_admin=False))
    _db.add(LibraryConfig(plex_section_key="1", plex_section_title="Movies",
                          plex_section_type="movie", media_category="movie"))
    _db.add(LibraryConfig(plex_section_key="2", plex_section_title="TV Shows",
                          plex_section_type="show", media_category="show"))

with fake_db_session() as _db:
    OWNER_ID = _db.query(User).filter_by(plex_username="owner").one().id
    MEMBER_ID = _db.query(User).filter_by(plex_username="member").one().id

T0 = datetime(2026, 10, 20, 20, 0, 0)
clock = {"now": T0}


class FakeDatetime(datetime):
    @classmethod
    def utcnow(cls):
        return clock["now"]


def _ts(dt):
    return calendar.timegm(dt.utctimetuple())


state: dict = {}
FILM_AT = _ts(T0 - timedelta(days=2))
# The owner's own watched film (the section listing returns it)...
owner_plays = [("500", FILM_AT)]
# ...and the member's play history: three Free Rein episodes, one play in a
# library Curatarr does not follow (section 9), and the same film right
# where the listing already has it (left to the listing, no second row).
member_events = [
    {"ratingKey": "901", "viewedAt": _ts(T0 - timedelta(days=5)), "type": "episode",
     "title": "Pilot", "grandparentTitle": "Free Rein", "grandparentKey": "/library/metadata/900",
     "parentIndex": 1, "index": 1, "librarySectionID": 2},
    {"ratingKey": "902", "viewedAt": _ts(T0 - timedelta(days=4)), "type": "episode",
     "title": "Ep 2", "grandparentTitle": "Free Rein", "grandparentKey": "/library/metadata/900",
     "parentIndex": 1, "index": 2, "librarySectionID": 2},
    {"ratingKey": "903", "viewedAt": _ts(T0 - timedelta(days=3)), "type": "episode",
     "title": "Ep 3", "grandparentTitle": "Free Rein", "grandparentKey": "/library/metadata/900",
     "parentIndex": 1, "index": 3, "librarySectionID": 2},
    {"ratingKey": "777", "viewedAt": _ts(T0 - timedelta(days=3)), "type": "movie",
     "title": "Home video", "librarySectionID": 9},
    {"ratingKey": "500", "viewedAt": FILM_AT + 30, "type": "movie",
     "title": "Film 500", "librarySectionID": 1},
]
history_params: list = []


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
            rows = [{"ratingKey": rk, "lastViewedAt": at, "title": f"Film {rk}", "type": "movie",
                     "duration": 1000, "Genre": [{"tag": "Drama"}]}
                    for rk, at in owner_plays if at >= since] if m.group(1) == "1" else []
            return FakeResp(200, {"MediaContainer": {"Metadata": rows}})
        if url.endswith("/accounts"):
            return FakeResp(200, {"MediaContainer": {"Account": [
                {"id": 1, "name": "owner"}, {"id": int(MEMBER), "name": "member"},
                {"id": 999, "name": "stranger"}]}})
        if url.endswith("/status/sessions/history/all"):
            history_params.append(dict(params))
            since = int(params.get("viewedAt>>", 0))
            acc = params.get("accountID")
            evs = [dict(e, accountID=int(MEMBER)) for e in member_events
                   if acc == MEMBER and e["viewedAt"] >= since]
            return FakeResp(200, {"MediaContainer": {"Metadata": evs}})
        return FakeResp(404)


async def _no_history(*a, **kw):
    return {}, {}


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
ps._fetch_plex_history_lookup = _no_history
ps._sync_music_ratings = _noop
ps._process_rec_watch_hits = lambda hits: None
te.compute_all_taste_vectors = _noop


def rows(user_id):
    with fake_db_session() as db:
        return sorted((r.plex_item_id, r.series_title, r.completed, r.plex_user_id)
                      for r in db.query(WatchHistoryEntry).filter_by(user_id=user_id).all())


# ── 1. first sync: the member's whole history comes in ──────────────────────
asyncio.run(ps.sync_plex_history(force=True))
member_rows = rows(MEMBER_ID)
check("the member's Free Rein episodes reach watch history under the member",
      [r[0] for r in member_rows] == ["901", "902", "903"]
      and all(r[1] == "Free Rein" and r[2] is True and r[3] == MEMBER for r in member_rows))
check("the owner keeps their own film and gets none of the member's plays",
      [r[0] for r in rows(OWNER_ID)] == ["500"])
check("a play in a library Curatarr does not follow is skipped",
      "777" not in [r[0] for r in member_rows])
check("a play the section listing already carries gets no second row",
      "500" not in [r[0] for r in member_rows])
check("only Curatarr members' accounts are asked, never a stranger's",
      {p.get("accountID") for p in history_params} == {MEMBER})
check("the first member sync walks the whole history and records the backfill",
      "viewedAt>>" not in history_params[0] and state.get(ps.MEMBER_BACKFILL_KEY) == "1")

# ── 2. later syncs are incremental and add no duplicates ────────────────────
history_params.clear()
clock["now"] = T0 + timedelta(hours=2)
member_events.append({"ratingKey": "904", "viewedAt": _ts(T0 + timedelta(hours=1)),
                      "type": "episode", "title": "Ep 4", "grandparentTitle": "Free Rein",
                      "grandparentKey": "/library/metadata/900", "parentIndex": 1, "index": 4,
                      "librarySectionID": 2})
asyncio.run(ps.sync_plex_history(force=True))
check("the next sync asks only for plays since the last one",
      history_params and "viewedAt>>" in history_params[0])
check("the new episode arrives once and nothing doubles",
      [r[0] for r in rows(MEMBER_ID)] == ["901", "902", "903", "904"])

# ── 3. the household floor can see it now ───────────────────────────────────
from src.services import pillars  # noqa: E402

with fake_db_session() as db:
    others = pillars._other_users_watch(db, OWNER_ID, {"title": "Free Rein"}, "show")
check("the judge's household lookup finds the member's Free Rein episodes",
      [(o["name"], o["distinct_episodes"]) for o in others] == [("member", 4)])
check("...and the household floor protects the show from a CUT",
      pillars._household_claim(others, "show", episodes_total=10) == "member")

check.done()
