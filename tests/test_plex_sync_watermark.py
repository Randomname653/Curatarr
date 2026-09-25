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
import asyncio
import re
import sys
import threading
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
from src.services.task_monitor import task_monitor, TaskStatus

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── throwaway DB ─────────────────────────────────────────────────────────────
engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
Base.metadata.create_all(engine)
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
            section_params.append(dict(params))
            since = int(params.get("lastViewedAt>>", 0))
            # the plays live in section 1; any other section is empty
            rows = [{"ratingKey": rk, "lastViewedAt": at, "title": f"Film {rk}",
                     "type": "movie", "duration": 1000, "Genre": [{"tag": "Drama"}]}
                    for rk, at in plays if at >= since] if sec == "1" else []
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

src = (Path(__file__).resolve().parents[1] / "src/services/plex_sync.py").read_text(encoding="utf-8")
check("busy skip is marked so the scheduler can tell it from the cooldown",
      '"busy": True' in src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
