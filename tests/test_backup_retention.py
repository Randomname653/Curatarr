"""Backup retention and the free-space guard (2026-10 lifecycle audit).

update_db.py wrote a full pre-migrate copy next to the live database on
every run and never removed one: a copy per release, for good. The daily
snapshot rotated, but was written onto a nearly full disk regardless,
taking the space the live database needs for its WAL and the vacuum.

    python tests/test_backup_retention.py
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.services.app_state as app_state
import src.services.scheduler as sched
import update_db
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


tmp = Path(tempfile.mkdtemp(prefix="curatarr_bak_"))

# ── 1. pre-migrate rotation ──────────────────────────────────────────────────
db = tmp / "curatarr.db"
db.write_bytes(b"")
stamps = ["20260101_000000", "20260201_000000", "20260301_000000",
          "20260401_000000", "20260501_000000"]
for s in stamps:
    (tmp / f"curatarr.pre-migrate-{s}.db").write_bytes(b"x")
(tmp / "other.pre-migrate-20250101_000000.db").write_bytes(b"x")   # another DB's
dropped = update_db._rotate_pre_migrate(db)
left = sorted(p.name for p in tmp.glob("curatarr.pre-migrate-*.db"))
check("the newest three pre-migrate copies are kept",
      left == [f"curatarr.pre-migrate-{s}.db" for s in stamps[-3:]] and dropped == 2)
check("another database's copies are not touched",
      (tmp / "other.pre-migrate-20250101_000000.db").exists())
check("the live database is not touched", db.exists())
check("a second run removes nothing", update_db._rotate_pre_migrate(db) == 0)
src = (Path(__file__).resolve().parents[1] / "update_db.py").read_text(encoding="utf-8")
check("update_database rotates right after a verified backup",
      src.index("backup = _backup()") < src.index("_rotate_pre_migrate(Path(")
      < src.index("init_db()\n"))

# ── 2. daily backup: free-space guard ────────────────────────────────────────
live = tmp / "live" / "curatarr.db"
live.parent.mkdir()
c = sqlite3.connect(live)
c.execute("CREATE TABLE t (b BLOB)")
c.executemany("INSERT INTO t VALUES (?)", [(b"x" * 4000,) for _ in range(50)])
c.commit()
c.close()
size = live.stat().st_size
bdir = live.parent / "backups"
bdir.mkdir()
for i in range(7):
    (bdir / f"curatarr_2026010{i}_000000.db").write_bytes(b"x" * 10)

settings.DATABASE_URL = "sqlite:///" + str(live)
app_state.get_state = lambda k: None
_real_free = sched._free_bytes

free = {"bytes": 0}
sched._free_bytes = lambda path: free["bytes"]
res = asyncio.run(sched.job_db_backup())
snaps = sorted(p.name for p in bdir.glob("curatarr_*.db"))
check("too little room: no snapshot written, the task stays due (False)",
      res is False and all(n.startswith("curatarr_2026010") for n in snaps))
check("... after the oldest snapshot made way (KEEP-1 left)",
      len(snaps) == 6 and "curatarr_20260100_000000.db" not in snaps)

free["bytes"] = 2 * size + 1
res = asyncio.run(sched.job_db_backup())
snaps = sorted(p.name for p in bdir.glob("curatarr_*.db"))
check("room for the snapshot plus as much again: backup taken",
      res is not False and any(not n.startswith("curatarr_2026010") for n in snaps))
check("rotation keeps seven", len(snaps) == 7)

# Short at first, enough once the oldest made way: the backup still happens.
calls = []


def freed_by_prune(path):
    calls.append(path)
    return 0 if len(calls) == 1 else 2 * size + 1


sched._free_bytes = freed_by_prune
# The snapshot above is named after this very second; date it back instead
# of sleeping into the next one, so the next snapshot gets a name of its own.
for p in bdir.glob("curatarr_*.db"):
    if not p.name.startswith("curatarr_2026010"):
        p.rename(bdir / "curatarr_20260109_000000.db")
before = set(p.name for p in bdir.glob("curatarr_*.db"))
res = asyncio.run(sched.job_db_backup())
after = set(p.name for p in bdir.glob("curatarr_*.db"))
check("making way for today's snapshot is enough when it frees the room",
      res is not False and len(after - before) == 1 and len(after) == 7)
sched._free_bytes = _real_free
check("the real free-space probe answers", sched._free_bytes(str(tmp)) > 0)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
