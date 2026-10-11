"""Shutdown releases the database before Syncthing may touch data/ again.

The lifespan teardown used to stop only APScheduler: the long-running work it
had launched (custodian queues, enrichment, music pipeline) kept writing
while the flags were cleared, and nothing disposed the engine or checkpointed
the WAL before sync_guard.disable() re-enabled Syncthing on an open WAL-mode
database — the corruption case Pass 100 exists to prevent.

    python tests/test_shutdown_release.py
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_tmp = Path(tempfile.mkdtemp())
DB_FILE = (_tmp / "curatarr.db").resolve()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"

from sqlalchemy import text  # noqa: E402

from src.database import connection as conn_mod  # noqa: E402
from src.database.runner import run_write, shutdown_writer  # noqa: E402
from src.services import bg_tasks  # noqa: E402
from src.services import scheduler as sched  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── drain(): cancels both trackers' tasks and lets their finally run ────────
async def drain_case():
    cleaned = []

    async def worker(tag):
        try:
            await asyncio.sleep(3600)
        finally:
            cleaned.append(tag)

    release = asyncio.Event()

    async def stubborn():
        while not release.is_set():  # swallows every cancel until released
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    a = bg_tasks.track_task(worker("bg"), name="bg-worker")
    b = sched._track_task(asyncio.create_task(worker("sched")))
    s = bg_tasks.track_task(stubborn(), name="stubborn")
    await asyncio.sleep(0)
    t0 = asyncio.get_running_loop().time()
    await bg_tasks.drain(timeout=0.3)
    waited = asyncio.get_running_loop().time() - t0
    result = (sorted(cleaned), a.cancelled(), b.cancelled(), s.done(), waited)
    release.set()                   # cleanup for the test only
    await s
    return result


cleaned, a_cancelled, b_cancelled, s_done, waited = asyncio.run(drain_case())
check("drain cancels bg_tasks- and scheduler-tracked tasks", a_cancelled and b_cancelled)
check("their finally blocks ran before drain returned", cleaned == ["bg", "sched"])
check("a task that swallows the cancel cannot hold the exit past the timeout",
      not s_done and waited < 1.0)
check("drain with nothing tracked is a no-op", asyncio.run(bg_tasks.drain(timeout=0.1)) is None)

# ── checkpoint_and_dispose(): WAL folded into the main file and truncated ──
with conn_mod.engine.begin() as c:
    c.execute(text("CREATE TABLE t (k INTEGER PRIMARY KEY, v TEXT)"))
    c.execute(text("INSERT INTO t (v) VALUES ('a'), ('b'), ('c')"))


def _write_through_writer():
    with conn_mod.write_session() as db:
        db.execute(text("INSERT INTO t (v) VALUES ('queued')"))


try:
    with conn_mod.write_session() as db:
        db.execute(text("INSERT INTO t (v) VALUES ('rolled back')"))
        raise RuntimeError("boom")
except RuntimeError:
    pass
with conn_mod.engine.connect() as c:
    check("write_session rolls back on an exception",
          c.execute(text("SELECT COUNT(*) FROM t WHERE v = 'rolled back'")).scalar() == 0)

asyncio.run(run_write(_write_through_writer))
wal = Path(str(DB_FILE) + "-wal")
check("precondition: the WAL holds uncheckpointed pages", wal.exists() and wal.stat().st_size > 0)
shutdown_writer()
conn_mod.checkpoint_and_dispose()
check("after release the -wal file is empty", not wal.exists() or wal.stat().st_size == 0)
check("the pool holds no open connection", conn_mod.engine.pool.checkedout() == 0)
with sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True) as ro:
    rows = [r[0] for r in ro.execute("SELECT v FROM t ORDER BY k")]
check("every committed row, the writer's included, is in the main file",
      rows == ["a", "b", "c", "queued"])
check("the writer comes back on demand after a shutdown",
      asyncio.run(run_write(lambda: 42)) == 42)
shutdown_writer()

# ── the lifespan teardown runs the steps in this order ──────────────────────
body = (ROOT / "src/main.py").read_text(encoding="utf-8").split("    yield\n", 1)[1]
order = ["stop_scheduler()", "await drain(", 'set_state("enrichment_running", "0")',
         "shutdown_writer()", "checkpoint_and_dispose()", "_sync_guard_disable()"]
pos = [body.find(s) for s in order]
check("teardown: stop scheduler → drain → clear flags → release DB → Syncthing",
      all(p >= 0 for p in pos) and pos == sorted(pos))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
