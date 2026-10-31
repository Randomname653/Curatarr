"""The music lease: one music runner at a time, across OS processes.

* scripts/music_enricher.py took ``music_pipeline_running`` with a
  read-then-write, so two runners could both read "0" and both start.
* The server set the flag to "0" at every boot and shutdown, and the in-app
  runner's ``finally`` did the same — a live enricher in another process
  lost its lock and the custodian started a second pass beside it.
* The boot resume checked the flag and then ran WITHOUT taking it.
* The UI's Stop button set a flag the script never read.

Real processes against a throwaway SQLite file; the second half pins the
call sites.

    python tests/test_music_lease.py
"""
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import psutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services import state_lease as L

from _checks import check  # noqa: E402


tmp = Path(tempfile.mkdtemp())
DB = str(tmp / "lease.db")
with sqlite3.connect(DB) as c:
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    c.execute("INSERT INTO app_state VALUES ('k', '1', NULL)")   # a legacy bare "1"

K = "k"


def value(key):
    with sqlite3.connect(DB) as c:
        row = c.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


print("\n-- legacy flag and own lease --")
check("a legacy '1' with no holder is stale: acquirable", L.acquire(K, DB))
check("holder recorded with this PID", L.holder_pid(K, DB) == os.getpid())
check("the holder itself cannot take it twice (in-process exclusion)", not L.acquire(K, DB))
check("heartbeat succeeds while ours", L.heartbeat(K, DB))
check("release clears it", L.release(K, DB) and value(K) == "0")
check("a second release is refused (nothing of ours left)", not L.release(K, DB))

print("\n-- a live holder in another process --")
child = subprocess.Popen(
    [sys.executable, "-c",
     "import sys, time; sys.path.insert(0, sys.argv[1]);"
     "from src.services import state_lease as L;"
     "print('held' if L.acquire('k', sys.argv[2]) else 'refused', flush=True); time.sleep(120)",
     str(ROOT), DB],
    stdout=subprocess.PIPE, text=True)
try:
    check("child took the lease", child.stdout.readline().strip() == "held")
    check("acquire refused while it lives", not L.acquire(K, DB))
    check("boot reclaim leaves a live holder alone", not L.reclaim_if_stale(K, DB) and value(K) == "1")
    check("a release by a non-holder is refused (no blind '0')", not L.release(K, DB) and value(K) == "1")
    check("heartbeat by a non-holder is refused", not L.heartbeat(K, DB))
    # On Windows a venv's python.exe is a launcher that runs the real
    # interpreter as ITS child, so the lease holder may be one level below
    # child.pid; killing the launcher alone would leave the holder alive.
    _tree = [psutil.Process(child.pid)] + psutil.Process(child.pid).children(recursive=True)
    check("holder_pid names the child", L.holder_pid(K, DB) in [pr.pid for pr in _tree])
finally:
    # Kill the whole tree and wait until it is gone: Windows ends a killed
    # process asynchronously, and a holder still exiting is still alive.
    _procs = locals().get("_tree") or [psutil.Process(child.pid)]
    for pr in _procs:
        try:
            pr.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(_procs, timeout=10)
    child.wait()

print("\n-- the holder dies hard --")
check("a killed holder's lease is reclaimed at boot", L.reclaim_if_stale(K, DB) and value(K) == "0")
child = subprocess.Popen(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, sys.argv[1]);"
     "from src.services import state_lease as L; L.acquire('k', sys.argv[2])",
     str(ROOT), DB])
child.wait()
check("a dead holder's lease is taken over by the next acquire", L.acquire(K, DB))
L.release(K, DB)

print("\n-- liveness rules --")
me = L.my_token()
now = time.time()
check("this host: a live PID wins over an old heartbeat", L.holder_alive(f"{me}|1", now=now))
host = socket.gethostname()
check("this host: a dead PID is stale",
      not L.holder_alive(f"{host}|999999999|1.0|{now:.0f}"))
check("other host: fresh heartbeat is alive", L.holder_alive(f"elsewhere|1|1.0|{now:.0f}", now=now))
check("other host: no heartbeat for the TTL is stale",
      not L.holder_alive(f"elsewhere|1|1.0|{now - L.LEASE_TTL_S - 1:.0f}", now=now))
check("garbage / empty holder is stale", not L.holder_alive("") and not L.holder_alive(None)
      and not L.holder_alive("1"))

print("\n-- every writer goes through the lease --")
src = lambda p: (ROOT / p).read_text(encoding="utf-8")
main = src("src/main.py")
check("boot: reclaim, not a blind reset",
      'reclaim_if_stale("music_pipeline_running")' in main
      and 'set_state("music_pipeline_running", "0")' not in main)
check("shutdown: releases only its own lease", 'state_lease.release("music_pipeline_running")' in main)
music = src("src/routers/music.py")
# /start is a plain def since #162 (FastAPI's worker thread), so the lease
# is called directly rather than through asyncio.to_thread.
check("/start acquires the lease", 'state_lease.acquire("music_pipeline_running")' in music)
check("the in-app runner's finally compares-and-clears",
      'state_lease.release, "music_pipeline_running"' in music
      and 'set_state("music_pipeline_running",        "0")' not in music)
sched = src("src/services/scheduler.py")
i = sched.index("async def _resume_music_pipeline_if_needed")
resume = sched[i:sched.index("async def _run_music_pipeline_bg")]
check("boot resume takes the lease before running",
      'state_lease.acquire, "music_pipeline_running"' in resume
      and resume.index("state_lease.acquire") < resume.index("_run_music_pipeline_bg"))
cust = src("src/services/data_custodian.py")
check("custodian acquires and releases the lease",
      'state_lease.acquire, "music_pipeline_running"' in cust
      and 'state_lease.release("music_pipeline_running")' in cust
      and 'acquire_state_lock("music_pipeline_running")' not in cust)
script = src("scripts/music_enricher.py")
check("enricher script: lease, not get-then-set",
      "state_lease.acquire(_LEASE_KEY)" in script
      and 'get_state("music_pipeline_running") == "1"' not in script)
check("enricher script: honours the web UI's Stop and heartbeats",
      "stop_requested_from_ui()" in script and "state_lease.heartbeat(_LEASE_KEY)" in script)
check("enricher script: Windows console close (SIGBREAK) stops it cleanly",
      'getattr(signal, "SIGBREAK", None)' in script)

check.done()
