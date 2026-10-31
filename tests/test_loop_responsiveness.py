"""The server answers while another process holds the SQLite write lock.

Before the loop-free change, a coroutine's synchronous write waited out the
lock on the event-loop thread: every request and stream stalled for up to
busy_timeout (60 s). Now writes from coroutines wait on the DB writer thread
and the loop keeps serving, which is what lets busy_timeout be 15 s.

  * control: a sync set_state from a coroutine while the lock is held
    stalls the loop for the hold (the old pattern; proves the probe works)
  * aset_state / run_write(acquire_state_lock) waiting on that lock leave
    the loop ticking (max gap < 200 ms) and a real API request answering
    in < 200 ms, and both writes land once the lock is released
  * the engine's busy_timeout is the lowered 15 s, and the pool settings
    that only made sense with 60 s waits are gone

Runs against a temp file database (DATABASE_URL set before the app is
imported); the lock holder is a separate python process.

    python tests/test_loop_responsiveness.py
"""
import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
_DB = (_TMP / "curatarr.db").resolve()
os.environ["DATABASE_URL"] = f"sqlite:///{_DB.as_posix()}"

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import Base  # noqa: E402
from src.database.runner import run_write  # noqa: E402
from src.services import app_state  # noqa: E402

PASS = FAIL = 0
HOLD_S = 0.8          # long enough to tell a stall (>0.48 s) from a tick (<0.2 s)


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


_HOLDER = f"""
import sqlite3, sys, time
c = sqlite3.connect({str(_DB)!r}, isolation_level=None, timeout=5)
c.execute("BEGIN IMMEDIATE")
c.execute("INSERT OR REPLACE INTO app_state (key, value, updated_at) VALUES ('holder', '1', 'x')")
print("locked", flush=True)
time.sleep({HOLD_S})
c.execute("COMMIT")
"""


def _hold_lock() -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-c", _HOLDER], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "locked"
    return p


async def _heartbeat(stop: asyncio.Event, gaps: list):
    last = time.monotonic()
    while not stop.is_set():
        await asyncio.sleep(0.01)
        now = time.monotonic()
        gaps.append(now - last)
        last = now


async def _measure(work):
    stop, gaps = asyncio.Event(), []
    beat = asyncio.create_task(_heartbeat(stop, gaps))
    await asyncio.sleep(0.05)
    result = await work()
    stop.set()
    await beat
    return result, max(gaps)


Base.metadata.create_all(conn_mod.engine)

# ── 1. control: the old pattern freezes the loop ────────────────────────────
print("control: sync write on the loop")
holder = _hold_lock()


async def _old_pattern():
    app_state.set_state("probe_old", "1")   # waits on the loop thread


_, gap = asyncio.run(_measure(_old_pattern))
holder.wait()
check(f"a sync set_state from a coroutine stalls the loop for the hold ({gap:.2f} s)",
      gap > HOLD_S * 0.6)

# ── 2. the converted pattern keeps serving ──────────────────────────────────
print("writes on the DB writer thread")
from httpx import ASGITransport, AsyncClient  # noqa: E402
from src.main import app  # noqa: E402

holder = _hold_lock()


async def _new_pattern():
    writes = asyncio.gather(
        app_state.aset_state("probe_new", "1"),
        run_write(app_state.acquire_state_lock, "probe_lock"),
    )
    await asyncio.sleep(0.2)   # both are now waiting on the lock
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        t0 = time.monotonic()
        r = await c.get("/api/setup/status")
        latency = time.monotonic() - t0
    pending = not writes.done()
    return (r.status_code, latency, pending, await writes)


(status, latency, pending, results), gap = asyncio.run(_measure(_new_pattern))
holder.wait()
check("the writes were still waiting on the lock during the request", pending)
check(f"an API request answers while they wait ({latency * 1000:.0f} ms)",
      status == 200 and latency < 0.2)
check(f"the loop keeps ticking (max gap {gap * 1000:.0f} ms)", gap < 0.2)
check("both writes land once the lock is released",
      results == [True, True] and app_state.get_state("probe_new") == "1"
      and app_state.get_state("probe_lock") == "1")

# ── 3. the timeouts ─────────────────────────────────────────────────────────
print("timeouts")
with conn_mod.engine.connect() as c:
    busy = c.exec_driver_sql("PRAGMA busy_timeout").scalar()
    temp = c.exec_driver_sql("PRAGMA temp_store").scalar()
check(f"busy_timeout is 15 s (was 60 s): {busy} ms", busy == 15000)
check("temp_store=MEMORY", temp == 2)
pool = conn_mod.engine.pool
check("pool: 10 connections, 10 s checkout wait, no pre-ping",
      pool.size() == 10 and pool._timeout == 10 and not pool._pre_ping)
src = (ROOT / "src" / "database" / "connection.py").read_text(encoding="utf-8")
check("no per-checkout busy_timeout reset", '"checkout"' not in src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
