"""Scheduler jobs, custodian queues and their services run no database
statement on the event-loop thread (src/database/loop_guard.py in raise mode).

Each check drives a converted coroutine against a temp database with the
outside world stubbed, and asserts both that the guard saw no site in these
files and that the job still did its work:

  * scheduler.job_arr_sync          admin lookup, deletion_run lock, proposal write
  * scheduler._tracked + job_memory_decay   read-modify-write, last-run stamp
  * data_custodian.custodian_tick   due check, run, stamp, stored report
  * data_custodian._run_recs        per-user cache state, flag cleared
  * orphan_repair.apply_orphan_mapping      dedupe + insert in one write
  * model_updates.check_updates     state load / save
  * main /api/system/notifications  a plain def handler (threadpool)

    python tests/test_loop_free_g3.py
"""
import asyncio
import inspect
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402

import src.database.connection as conn_mod  # noqa: E402
from src.database import loop_guard  # noqa: E402
from src.database.models import (Base, DeletionProposal, EpisodicMemory,  # noqa: E402
                                 User, WatchHistoryEntry)
from src.services import app_state  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


tmp = Path(tempfile.mkdtemp())
eng = create_engine(f"sqlite:///{(tmp / 'g3.db').as_posix()}",
                    connect_args={"check_same_thread": False})
Base.metadata.create_all(eng)
conn_mod.SessionLocal.configure(bind=eng)
conn_mod.engine = eng
loop_guard.install(eng)
loop_guard.serving(True)

MINE = ("src/services/scheduler.py", "src/services/data_custodian.py", "src/main.py",
        "src/services/orphan_repair.py", "src/services/raw_refresh.py",
        "src/services/model_updates.py", "src/services/embedding_migration.py")

with conn_mod.get_db_session() as _db:
    _db.add(User(id=1, plex_user_id="p1", plex_username="admin", is_admin=True, is_active=True))
    _db.commit()


def guarded(coro_fn, *a, **kw):
    """(result or the exception, sites in my files) of one run in raise mode."""
    loop_guard.reset()
    os.environ["CURATARR_LOOP_GUARD"] = "raise"
    try:
        try:
            out = asyncio.run(coro_fn(*a, **kw))
        except Exception as e:  # noqa: BLE001 - the check reports it
            out = e
    finally:
        os.environ.pop("CURATARR_LOOP_GUARD", None)
    sites = [s["site"] for s in loop_guard.snapshot()["sites"]
             if s["site"].startswith(MINE)]
    return out, sites


# ── 1. job_arr_sync: lock + proposal write off the loop ─────────────────────
print("scheduler.job_arr_sync")
from src.services import scheduler as sched  # noqa: E402

_retired = []


def _fake_retire(db, user_id):
    # Runs on the writer thread with the job's session.
    _retired.append(db.query(DeletionProposal).count())
    return 0


async def _candidates(category=None):
    return [{"title": "Old Film", "service": "radarr", "arr_id": 7, "category": "movie"}]


async def _gen(user_id, items, category, monitor_task=None):
    return [{"title": "Old Film", "pitch": "unwatched for years", "confidence": 0.9,
             "arr_id": 7, "service": "radarr", "category": "movie"}]


async def _enrich(p, item_map, category):
    return {**p, "poster_url": "http://x/p.jpg"}


# Stand-ins for the router and engine modules: the job imports them inside,
# and the real ones open ChromaDB at import.
sys.modules["src.routers.recommendations"] = types.SimpleNamespace(
    _fetch_arr_candidates=_candidates, _enrich_proposal=_enrich,
    build_proposal_item_map=lambda items: {}, _retire_switched_off=_fake_retire)
sys.modules["src.services.recommendations_engine"] = types.SimpleNamespace(
    generate_deletion_proposals=_gen, run_judged_everything=lambda cat: True)
import src.services.music_source as _ms  # noqa: E402
_ms.music_service = lambda: "lidarr"

app_state.set_state("deletion_run", "0")
out, sites = guarded(sched.job_arr_sync)
with conn_mod.get_db_session() as _db:
    rows = [(p.title, p.status, p.category, p.poster_url)
            for p in _db.query(DeletionProposal).all()]
check("no statement on the loop", sites == [])
check("the scan completed (None, not the False of a failed run)", out is None)
check("the proposal was written with its enrichment",
      rows == [("Old Film", "pending", "movie", "http://x/p.jpg")] and _retired == [0])
check("the deletion_run lock is released and the sync stamped",
      app_state.get_state("deletion_run") == "0"
      and bool(app_state.get_state("last_arr_sync_at")))

app_state.set_state("deletion_run", "1")   # a manual analysis holds the mutex
out, sites = guarded(sched.job_arr_sync)
check("held lock: stays due (False), the manual run's lock untouched, no loop statement",
      out is False and app_state.get_state("deletion_run") == "1" and sites == [])
app_state.set_state("deletion_run", "0")
del sys.modules["src.routers.recommendations"], sys.modules["src.services.recommendations_engine"]

# ── 2. _tracked + job_memory_decay ──────────────────────────────────────────
print("scheduler._tracked(job_memory_decay)")
with conn_mod.get_db_session() as _db:
    _db.add(EpisodicMemory(user_id=1, memory_type="feedback", content="old",
                           importance=0.8, created_at=datetime.utcnow() - timedelta(days=200)))
    _db.add(EpisodicMemory(user_id=1, memory_type="feedback", content="new",
                           importance=0.8, created_at=datetime.utcnow()))
    _db.commit()
out, sites = guarded(sched._tracked("memory_decay")(sched.job_memory_decay))
with conn_mod.get_db_session() as _db:
    imp = {m.content: round(m.importance, 3) for m in _db.query(EpisodicMemory).all()}
check("no statement on the loop", sites == [])
check("only the old memory decayed", imp == {"old": 0.68, "new": 0.8})
check("the run is stamped", bool(app_state.get_state("job_last_run:memory_decay")))

# ── 3. custodian_tick: due check, run, stamp, report ────────────────────────
print("data_custodian.custodian_tick")
from src.services import data_custodian as dc  # noqa: E402

_ran = []


async def _runner():
    _ran.append(1)
    return True


_real_registry = dc._registry
dc._registry = lambda: [dc.Task("g3_probe", "Probe", 24.0, _runner, reports_own=True)]
try:
    out, sites = guarded(dc.custodian_tick, wait=True)
finally:
    dc._registry = _real_registry
_report = json.loads(app_state.get_state(f"{dc._REPORT_KEY}:background") or "{}")
check("no statement on the loop", sites == [])
check("the due task ran once and was stamped",
      _ran == [1] and bool(app_state.get_state("job_last_run:g3_probe")))
check("the queue report is stored",
      [a["task"] for a in _report.get("actions", [])] == ["g3_probe"]
      and isinstance(out, dict) and out.get("actions"))

# ── 4. _run_recs ────────────────────────────────────────────────────────────
print("data_custodian._run_recs")
_cached = []


async def _fake_cache(uid):
    _cached.append(uid)


_real_cache = sched._cache_recommendations
sched._cache_recommendations = _fake_cache
app_state.set_state("recs_all_sampled:user_id=1", "1")
try:
    out, sites = guarded(dc._run_recs)
finally:
    sched._cache_recommendations = _real_cache
check("no statement on the loop", sites == [])
check("the stale user is refreshed and its flag cleared",
      out is True and _cached == [1]
      and app_state.get_state("recs_all_sampled:user_id=1") == "")

# ── 5. apply_orphan_mapping ─────────────────────────────────────────────────
print("orphan_repair.apply_orphan_mapping")
from src.services import orphan_repair as orp  # noqa: E402

_ts = int(datetime(2026, 1, 2).timestamp())


class _Resp:
    status_code = 200

    def json(self):
        return {"MediaContainer": {"Metadata": [
            {"librarySectionID": 9, "ratingKey": 55, "viewedAt": _ts, "title": "Ep",
             "type": "episode", "grandparentTitle": "Show", "duration": 100},
            {"librarySectionID": 9, "ratingKey": 55, "viewedAt": _ts, "title": "Ep",
             "type": "episode", "duration": 100},              # duplicate
            {"librarySectionID": 3, "ratingKey": 56, "viewedAt": _ts, "title": "Other"},
        ]}}


class _Client:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **kw):
        return _Resp()


_real_client = orp.httpx.AsyncClient
orp.httpx.AsyncClient = _Client
try:
    out, sites = guarded(orp.apply_orphan_mapping,
                         [{"section_id": "9", "category": "show", "title": "Old"}])
finally:
    orp.httpx.AsyncClient = _real_client
with conn_mod.get_db_session() as _db:
    hist = [(h.plex_item_id, h.media_type, h.series_title, h.user_id)
            for h in _db.query(WatchHistoryEntry).all()]
check("no statement on the loop", sites == [])
check("the mapped section's play is imported once, under the admin",
      out == {"saved": 1, "synced": 1} and hist == [("55", "show", "Show", 1)])

# ── 6. model_updates.check_updates ──────────────────────────────────────────
print("model_updates.check_updates")
from src.services import model_updates as mu  # noqa: E402


async def _running(endpoint, model):
    return "sha256:old"


async def _registry(ref):
    return "sha256:new"


_real_w = mu.running_weights, mu.registry_weights
mu.running_weights, mu.registry_weights = _running, _registry
try:
    out, sites = guarded(mu.check_updates)
finally:
    mu.running_weights, mu.registry_weights = _real_w
_stored = json.loads(app_state.get_state(mu.STATE_KEY) or "{}")
check("no statement on the loop", sites == [])
check("every tracked model is classified and the state saved",
      isinstance(out, list) and out and all(r["status"] == "update" for r in out)
      and _stored.get("curator", {}).get("remote") == "sha256:new")

# ── 7. main: the notifications route is a threadpool handler ────────────────
print("main /api/system/notifications")
from src.config import settings  # noqa: E402
settings.CHROMADB_PATH = tmp / "chroma"   # importing the app opens Chroma: not the real store
from src.main import system_notifications_route  # noqa: E402


async def _serve_like_fastapi():
    # FastAPI runs a plain def handler in its threadpool.
    return await asyncio.to_thread(system_notifications_route,
                                   types.SimpleNamespace(is_admin=True))


check("the handler is a plain def", not inspect.iscoroutinefunction(system_notifications_route))
out, sites = guarded(_serve_like_fastapi)
check("served off the loop with the same shape",
      sites == [] and isinstance(out, dict) and set(out) == {"items", "needs_action", "settings_action"}
      and any(i["kind"] == "model_update" for i in out["items"]))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
