"""Custodian job contract: a failed or skipped run must not be stamped done.

The custodian stamps a task's cadence when its runner returns None/True and
leaves it due on False. The scheduler jobs it wraps caught every exception
(to close their Activity card) and fell off the end — returning None, so a
Plex sync that crashed, an ARR scan that died mid-way or a backup that
failed were all recorded as today's successful run. A Plex sync that found
another sync holding the lock counted as done too.

Also: DB vacuum and backup ran raw sqlite3 on the event loop, freezing the
whole app for the length of a full-file rewrite/copy.

    python tests/test_custodian_job_contract.py
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.database.connection as conn_mod
import src.services.app_state as app_state
import src.services.data_custodian as dc
import src.services.orphan_repair as orphan_repair
import src.services.plex_sync as ps
import src.services.scheduler as sched
from src.config import settings

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# app_state in a dict — no real DB touched
state: dict = {}
app_state.get_state = lambda k: state.get(k)
app_state.set_state = lambda k, v: state.__setitem__(k, v)
sched._gaming = lambda: False


def run(coro):
    return asyncio.run(coro)


# ── job_plex_sync ────────────────────────────────────────────────────────────
async def _tech_ok(**kw):
    return {"skipped": True}


async def _noop(*a, **kw):
    return None


ps.sync_tech_profiles = _tech_ok
sched._recompute_and_cache_recs = _noop


def plex_returns(value):
    async def fake(**kw):
        if isinstance(value, Exception):
            raise value
        return value
    ps.sync_plex_history = fake


plex_returns(RuntimeError("Plex unreachable"))
check("plex_sync: exception -> False (was None = stamped done)",
      run(sched.job_plex_sync()) is False)
plex_returns({"skipped": True, "busy": True, "reason": "A sync is already running"})
check("plex_sync: another sync running -> False (retry soon)",
      run(sched.job_plex_sync()) is False)
plex_returns({"error": "PLEX_URL / PLEX_TOKEN not configured"})
check("plex_sync: error result -> False", run(sched.job_plex_sync()) is False)
plex_returns({"skipped": True, "reason": "Synced 5m ago"})
check("plex_sync: cooldown skip (history is fresh) -> done",
      run(sched.job_plex_sync()) is True)
plex_returns({"synced": 0, "skipped": 3})
check("plex_sync: clean run -> done", run(sched.job_plex_sync()) is True)


async def _tech_boom(**kw):
    raise RuntimeError("tech profile sync failed")


ps.sync_tech_profiles = _tech_boom
check("plex_sync: tech-profile half failing -> False",
      run(sched.job_plex_sync()) is False)
ps.sync_tech_profiles = _tech_ok


# ── jobs that fail inside their own try/except ──────────────────────────────
@contextmanager
def broken_db():
    raise RuntimeError("database is locked")
    yield  # pragma: no cover


_real_db = conn_mod.get_db_session
conn_mod.get_db_session = broken_db
check("arr_sync: exception -> False", run(sched.job_arr_sync()) is False)
check("memory_decay: exception -> False", run(sched.job_memory_decay()) is False)
check("arr_pre_enrich: exception -> False", run(sched.job_arr_pre_enrich()) is False)
conn_mod.get_db_session = _real_db


async def _orphans_boom():
    raise RuntimeError("Plex unreachable")


orphan_repair.detect_orphaned_sections = _orphans_boom
check("orphan_check: exception -> False", run(sched.job_orphan_check()) is False)

sched._gaming = lambda: True
check("arr_sync: skipped for a game -> False (not run, not done)",
      run(sched.job_arr_sync()) is False)
sched._gaming = lambda: False


# ── the custodian honours False, and _tracked does too ──────────────────────
recorded: list = []
sched._record_job_run = lambda job_id: recorded.append(job_id)
sched._job_overdue = lambda job_id, cadence_h: True


async def _fails():
    return False


async def _succeeds():
    return None


dc._registry = lambda: [
    dc.Task("t_fail", "Failing job", 24.0, _fails, reports_own=True),
    dc.Task("t_ok", "Plain job", 24.0, _succeeds, reports_own=True),
]
report = run(dc.custodian_tick(force=True))
check("custodian: a False return is not stamped", "t_fail" not in recorded)
check("custodian: a None return still counts as done", "t_ok" in recorded)
check("custodian report says the failed task continues",
      any(a["task"] == "t_fail" and "partial" in a["result"] for a in report["actions"]))

recorded.clear()
run(sched._tracked("x_fail")(_fails)())
run(sched._tracked("x_ok")(_succeeds)())
check("_tracked: False is not recorded, None is", recorded == ["x_ok"])


# ── vacuum / backup: off the event loop, and failures reported ──────────────
tmp = tempfile.mkdtemp(prefix="curatarr_test_")
db_path = os.path.join(tmp, "curatarr.db")
c = sqlite3.connect(db_path)
c.execute("CREATE TABLE t (x)")
c.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(500)])
c.commit()
c.close()

_orig_url, _orig_cache = settings.DATABASE_URL, settings.ENRICHMENT_CACHE
settings.DATABASE_URL = "sqlite:///" + db_path
settings.ENRICHMENT_CACHE = os.path.join(tmp, "missing-cache.db")
main_thread = threading.get_ident()
threads: list = []
_real_vac, _real_snap = sched._vacuum_sqlite_file, sched._snapshot_sqlite


def _spy(fn):
    def wrapper(*a):
        threads.append(threading.get_ident())
        return fn(*a)
    return wrapper


try:
    sched._vacuum_sqlite_file = _spy(_real_vac)
    sched._snapshot_sqlite = _spy(_real_snap)
    v = run(sched.job_db_vacuum())
    check("vacuum: VACUUM ran in a worker thread, not on the loop",
          threads and all(t != main_thread for t in threads))
    check("vacuum: success -> done", v is not False)

    threads.clear()
    b = run(sched.job_db_backup())
    snaps = [f for f in os.listdir(os.path.join(tmp, "backups")) if f.endswith(".db")]
    check("backup: snapshot written and verified", b is not False and len(snaps) == 1)
    check("backup: the copy ran in a worker thread, not on the loop",
          threads and all(t != main_thread for t in threads))

    def _snap_boom(*a):
        raise sqlite3.OperationalError("disk I/O error")

    def _vac_boom(*a):
        raise sqlite3.OperationalError("database is locked")

    sched._snapshot_sqlite = _snap_boom
    check("backup: failure -> False (was None = stamped done)",
          run(sched.job_db_backup()) is False)
    sched._vacuum_sqlite_file = _vac_boom
    check("vacuum: failure -> False", run(sched.job_db_vacuum()) is False)
finally:
    sched._vacuum_sqlite_file, sched._snapshot_sqlite = _real_vac, _real_snap
    settings.DATABASE_URL, settings.ENRICHMENT_CACHE = _orig_url, _orig_cache
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
