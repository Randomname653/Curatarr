"""force_set_state must write the app's own DB file on every OS.

It derived the path with ``DATABASE_URL.replace("sqlite:///", "").lstrip("/")``,
which turns the POSIX absolute URL ``sqlite:////home/…/curatarr.db`` into the
relative path ``home/…/curatarr.db``: the crash-cleanup write opened the wrong
file, found no app_state table and returned False, so a stale *_running flag
outlived the crash.

Runs against a temp file DB swapped in for the app engine, with the working
directory moved elsewhere so a relative path would miss.

    python tests/test_force_set_state_path.py
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine

import src.database.connection as conn_mod
from src.services import app_state

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
db_file = (tmp / "data" / "curatarr.db").resolve()
db_file.parent.mkdir(parents=True)
url = f"sqlite:///{db_file.as_posix()}"
conn_mod.engine = create_engine(url)

with sqlite3.connect(db_file) as c:
    c.execute("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    c.execute("INSERT INTO app_state VALUES ('music_pipeline_running', '1', 'x')")

elsewhere = tmp / "cwd"
elsewhere.mkdir()
old_cwd = os.getcwd()
os.chdir(elsewhere)
try:
    ok = app_state.force_set_state("music_pipeline_running", "0", timeout_s=5)
finally:
    os.chdir(old_cwd)

with sqlite3.connect(db_file) as c:
    value = c.execute("SELECT value FROM app_state WHERE key='music_pipeline_running'").fetchone()
check("force_set_state reports success", ok is True)
check("the flag was cleared in the app's own DB file", value == ("0",))
check("no stray database was created under the working directory",
      not any(elsewhere.rglob("*.db")))

conn_mod.engine.dispose()
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
