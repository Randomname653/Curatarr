"""Plex sync: the incremental watermark and the Activity card.

* ``last_sync_at`` used to be stamped with the time the sync FINISHED while
  Plex was asked at the START. A play landing in between was older than the
  next sync's ``lastViewedAt>>`` filter and never reached watch history —
  which is what the "watched in the last 90 days" deletion veto reads.
* Early returns and an unguarded Plex call left the "Plex sync" card
  RUNNING forever (and every later sync reused the stuck card).
* The per-entry DB walk ran on the event loop; it now runs in a thread.

Runs ``sync_plex_history`` against an in-memory SQLite DB and a fake Plex
that honours ``lastViewedAt>>`` — no network, no real data touched.

    python tests/test_plex_sync_watermark.py
"""
from _harness import check  # noqa: I001 - first: temp stores

import asyncio
import re
import threading
import types
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path


from sqlalchemy.orm import sessionmaker

import src.services.plex_sync as ps
import src.services.taste_engine as te
from src.database.models import Base, LibraryConfig, User, WatchHistoryEntry
from src.services.task_monitor import task_monitor, TaskStatus


# ── throwaway DB ─────────────────────────────────────────────────────────────
from _checks import memory_engine  # noqa: E402
engine = memory_engine()
Base.metadata.create_all(engine)
# Bind the app's own session factory too: app_state and the other modules
# that import get_db_session directly would otherwise reach the live
# data/curatarr.db (they only passed while that file happened to exist).
import src.database.connection as _conn  # noqa: E402
_conn.engine = engine
_conn.SessionLocal.configure(bind=engine)
Session = sessionmaker(bind=engine, autoflush=False)
session_threads: list = []


@contextmanager
def fake_db_session():
    session_threads.append(threading.get_ident())
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
    _db.add(User(plex_user_id="1", plex_username="owner", is_admin=True))
    _db.add(LibraryConfig(plex_section_key="1", plex_section_title="Movies",
                          plex_section_type="movie", media_category="movie"))

# ── fake clock + app_state ───────────────────────────────────────────────────
T0 = datetime(2026, 9, 1, 20, 0, 0)
clock = {"now": T0}


class FakeDatetime(datetime):
    @classmethod
    def utcnow(cls):
        return clock["now"]


state: dict = {}

# ── fake Plex ────────────────────────────────────────────────────────────────
# Plays on the server: (ratingKey, lastViewedAt). The section endpoint only
# returns rows at/after the lastViewedAt>> filter, like the real one.
plays: list = []
plex_down = {"on": False}
failing_sections: set = set()        # section keys whose watched-items fetch answers 503
section_params: list = []
other_sections: dict = {}             # section key -> [(ratingKey, lastViewedAt)]
# Called after the watched-items fetch: models time passing (and new plays
# landing) while the rest of the sync runs.
after_section_fetch = {"fn": None}


def _ts(dt):
    import calendar
    return calendar.timegm(dt.utctimetuple())


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
        if plex_down["on"]:
            raise ConnectionError("Plex unreachable")
        params = params or {}
        m = re.search(r"/library/sections/(\d+)/all$", url)
        if m and "viewCount>>" in params:
            sec = m.group(1)
            if sec in failing_sections:
                return FakeResp(503)
            section_params.append(dict(params, _sec=sec))
            since = int(params.get("lastViewedAt>>", 0))
            # the plays live in section 1 (and in other_sections); any other is empty
            rows = [{"ratingKey": rk, "lastViewedAt": at, "title": f"Film {rk}",
                     "type": "movie", "duration": 1000, "Genre": [{"tag": "Drama"}]}
                    for rk, at in (plays if sec == "1" else other_sections.get(sec, []))
                    if at >= since]
            if after_section_fetch["fn"]:
                after_section_fetch["fn"]()
            return FakeResp(200, {"MediaContainer": {"Metadata": rows}})
        # in-progress, /accounts, metadata: nothing to report
        return FakeResp(404)


async def _no_history(*a, **kw):
    return {}, {}


async def _noop(*a, **kw):
    return None


ps.settings = types.SimpleNamespace(effective_plex_url="http://plex",
                                    effective_plex_token="tok",
                                    PLEX_CLIENT_ID="test")
ps.get_db_session = fake_db_session
ps.get_datetime = lambda k: state.get(k)
ps.set_datetime = lambda k, v: state.__setitem__(k, v)
ps.acquire_state_lock = lambda k: True
ps.release_state_lock = lambda k: None
ps.httpx = types.SimpleNamespace(AsyncClient=FakeClient)
ps.datetime = FakeDatetime
ps._fetch_plex_history_lookup = _no_history
ps._sync_music_ratings = _noop
ps._process_rec_watch_hits = lambda hits: None
te.compute_all_taste_vectors = _noop


def rows():
    with fake_db_session() as db:
        return sorted(r.plex_item_id for r in db.query(WatchHistoryEntry).all())


def sync_cards():
    return [t for t in task_monitor._tasks.values() if t.category == "sync"]


def run(**kw):
    return asyncio.run(ps.sync_plex_history(**kw))


# ── 1. the watermark is the fetch time, not the finish time ─────────────────
plays.append(("100", _ts(T0 - timedelta(days=1))))


def _slow_sync_with_play_in_between():
    # The sync takes 10 minutes; five minutes in, someone finishes a film.
    plays.append(("200", _ts(T0 + timedelta(minutes=5))))
    clock["now"] = T0 + timedelta(minutes=10)


after_section_fetch["fn"] = _slow_sync_with_play_in_between
main_thread = threading.get_ident()
session_threads.clear()
r1 = run(force=True)
after_section_fetch["fn"] = None
check("initial sync stores the watched film", rows() == ["100"])
check("last_sync_at is the moment Plex was asked (T0), not the finish time",
      state.get("last_sync_at") == T0)

clock["now"] = T0 + timedelta(hours=2)
r2 = run(force=True)
check("next sync filters from before the in-between play",
      int(section_params[-1]["lastViewedAt>>"]) <= _ts(T0 + timedelta(minutes=5)))
check("the play that landed during the previous sync is not lost",
      rows() == ["100", "200"])
check("the per-entry DB walk ran off the event-loop thread",
      any(t != main_thread for t in session_threads))

# ── 3. the Activity card is closed on every exit path ───────────────────────
clock["now"] = T0 + timedelta(hours=4)
r3 = run(force=True)       # nothing new since the last watermark
check("'nothing new' early return still reports the empty result",
      r3.get("synced") == 0)
check("...and closes its card (was left RUNNING)",
      not any(t.status == TaskStatus.RUNNING for t in sync_cards()))

stamp_before = state.get("last_sync_at")
plex_down["on"] = True
clock["now"] = T0 + timedelta(hours=6)
raised = False
try:
    run(force=True)
except ConnectionError:
    raised = True
plex_down["on"] = False
check("an unreachable Plex still raises to the caller", raised)
check("...the card ends in ERROR instead of RUNNING forever",
      not any(t.status == TaskStatus.RUNNING for t in sync_cards())
      and any(t.status == TaskStatus.ERROR for t in sync_cards()))
check("...and the watermark does not move on a failed sync",
      state.get("last_sync_at") == stamp_before)

with fake_db_session() as db:
    db.query(LibraryConfig).delete()
n_cards = len(sync_cards())
r5 = run(force=True)
check("no library config -> error result, no orphaned card",
      "error" in r5 and len(sync_cards()) == n_cards)

# ── a library answering an error must not move the watermark past it ───────
with fake_db_session() as db:
    db.add(LibraryConfig(plex_section_key="1", plex_section_title="Movies",
                         plex_section_type="movie", media_category="movie"))
    db.add(LibraryConfig(plex_section_key="2", plex_section_title="Anime",
                         plex_section_type="show", media_category="anime"))
stamp_before = state.get("last_sync_at")
plays.append(("300", _ts(T0 + timedelta(hours=7))))
failing_sections.add("2")
clock["now"] = T0 + timedelta(hours=8)
r6 = run(force=True)
failing_sections.clear()
check("a failed library keeps the old watermark while the others' plays still land",
      state.get("last_sync_at") == stamp_before and "300" in rows())
check("...and the result names the library", r6.get("skipped_sections") == ["Anime"])
clock["now"] = T0 + timedelta(hours=10)
run(force=True)
check("once every library answers, the watermark moves again",
      state.get("last_sync_at") == T0 + timedelta(hours=10))
failing_sections.add("1")
failing_sections.add("2")
clock["now"] = T0 + timedelta(hours=12)
r7 = run(force=True)
failing_sections.clear()
check("an empty sync with failures says so too and leaves the watermark alone",
      r7.get("skipped_sections") == ["Movies", "Anime"]
      and state.get("last_sync_at") == T0 + timedelta(hours=10))

# ── a library mapped after the first syncs reads its whole history once ─────
# (2026-11-14: Music was never mapped; mapping it would have brought only the
# plays since the last sync, not six weeks of Plexamp.)
from src.routers.libraries import LibraryConfigItem, LibraryConfigRequest, configure_libraries  # noqa: E402
from src.services.app_state import get_state as _real_get, set_state as _real_set  # noqa: E402

other_sections["3"] = [("700", _ts(T0 - timedelta(days=60))), ("701", _ts(T0 + timedelta(hours=13)))]
_real_set("last_sync_at", "2026-09-01T22:00:00")       # an install that has synced before
with fake_db_session() as db:
    owner = db.query(User).first()
    res = configure_libraries(LibraryConfigRequest(libraries=[
        LibraryConfigItem(key="1", title="Movies", plex_type="movie", category="movie"),
        LibraryConfigItem(key="3", title="Music", plex_type="artist", category="music")]), owner, db)
check("saving the mapping names the newly followed library, not the one already followed",
      res.get("read_whole") == ["3"], res)
check("... and the history walks start from the beginning again",
      _real_get(ps.OWNER_BACKFILL_KEY) == "0" and _real_get(ps.MEMBER_BACKFILL_KEY) == "0")
section_params.clear()
clock["now"] = T0 + timedelta(hours=14)
run(force=True)
music_ask = [p for p in section_params if p["_sec"] == "3"]
check("the new library is asked without a time filter", music_ask and "lastViewedAt>>" not in music_ask[0])
check("its old plays arrive too, not only the ones since the last sync", {"700", "701"} <= set(rows()), rows())
check("the other libraries keep their incremental window",
      all("lastViewedAt>>" in p for p in section_params if p["_sec"] == "1"))
check("read whole once: nothing left pending", ps._new_sections() == set())
section_params.clear()
clock["now"] = T0 + timedelta(hours=16)
run(force=True)
check("the next sync asks it incrementally like the rest",
      all("lastViewedAt>>" in p for p in section_params if p["_sec"] == "3"))

src = (Path(__file__).resolve().parents[1] / "src/services/plex_sync.py").read_text(encoding="utf-8")
check("busy skip is marked so the scheduler can tell it from the cooldown",
      '"busy": True' in src)

check.done()
