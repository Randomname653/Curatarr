"""The battery's own isolation: tests/_harness.py and the data/ guard in
tests/run_all.py.

A suite that reaches the developer's data/ reads their live library into its
assertions, writes into it, and fails while the app holds the vector store.
These checks pin that every store is redirected and that a suite which still
writes there is caught.

    python tests/test_test_harness.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from _harness import ROOT, TMP, app_db, check  # noqa: I001 - first: temp stores

sys.path.insert(0, str(ROOT / "tests"))
import run_all  # noqa: E402

DATA = (ROOT / "data").resolve()


def outside_data(path_like) -> bool:
    p = Path(str(path_like).replace("sqlite:///", "")).resolve()
    return DATA not in p.parents and p != DATA


# ── 1. every store the settings open is off data/ ───────────────────────────
from src.config import settings  # noqa: E402
from src.services import lyrics  # noqa: E402

check.table("settings point at the temp dir", [
    ("main database", settings.DATABASE_URL, True),
    ("vector store", settings.CHROMADB_PATH, True),
    ("enrichment cache", settings.ENRICHMENT_CACHE, True),
    ("plex_music.db (derived at import)", lyrics.PLEX_MUSIC_DB_PATH, True),
    ("the .env file", os.environ["CURATARR_ENV_FILE"], True),
], outside_data)
check("they all live under this suite's temp dir",
      str(TMP) in settings.DATABASE_URL and str(TMP) in str(settings.CHROMADB_PATH))

# ── 2. app_db is the app's real engine, migrated ────────────────────────────
conn = app_db()
with conn.engine.connect() as c:
    fk = c.exec_driver_sql("PRAGMA foreign_keys").scalar()
    tables = {r[0] for r in c.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
check("app_db: foreign keys are enforced", fk == 1)
check("app_db: app_state and the user table exist", {"app_state", "users"} <= tables, sorted(tables)[:8])
from src.services import app_state  # noqa: E402
app_state.set_state("harness:probe", "1")
check("app_db: app_state round-trips through the real session factory",
      app_state.get_state("harness:probe") == "1")

# ── 3. the runner's per-suite env ───────────────────────────────────────────
env = run_all.suite_env({})
check("run_all: every suite gets its own temp stores",
      all(outside_data(env[k]) for k in ("DATABASE_URL", "CHROMADB_PATH", "ENRICHMENT_CACHE",
                                         "CURATARR_ENV_FILE")))
check("run_all: two suites never share a store",
      run_all.suite_env({})["DATABASE_URL"] != env["DATABASE_URL"])

# A child started with that env, importing the harness, keeps the runner's dir.
code = ("import sys; sys.path.insert(0, r'%s'); import _harness, os; "
        "print(_harness.TMP)" % (ROOT / "tests"))
out = subprocess.run([sys.executable, "-c", code], env={**os.environ, **env},
                     capture_output=True, text=True, timeout=60).stdout.strip()
check("harness under run_all reuses the suite's temp dir", out == env["CURATARR_SUITE_TMP"], out)

# ── 4. the data/ guard notices create, modify and delete ────────────────────
fake = Path(tempfile.mkdtemp())
run_all.DATA, real = fake, run_all.DATA
try:
    (fake / "logs").mkdir()
    (fake / "cache").mkdir()
    (fake / "curatarr.db").write_bytes(b"a")
    s0 = run_all.data_snapshot()
    (fake / "logs" / "tests.log").write_text("noise")
    check("guard: the test log is not a change", run_all.data_changes(s0, run_all.data_snapshot()) == [])
    (fake / "cache" / "enrichment.db").write_bytes(b"x")
    (fake / "curatarr.db").write_bytes(b"ab")
    s1 = run_all.data_snapshot()
    check("guard: a created and a modified store are both reported",
          run_all.data_changes(s0, s1) == [str(Path("cache/enrichment.db")), "curatarr.db"],
          run_all.data_changes(s0, s1))
    (fake / "curatarr.db").unlink()
    check("guard: a deleted store is reported",
          run_all.data_changes(s1, run_all.data_snapshot()) == ["curatarr.db"])
    run_all.DATA = fake / "missing"
    check("guard: no data/ at all is an empty snapshot", run_all.data_snapshot() == {})
finally:
    run_all.DATA = real

# ── 5. check.table reports a raising row instead of dying ───────────────────
from _harness import _Checks  # noqa: E402
probe = _Checks()
import contextlib, io  # noqa: E401,E402
with contextlib.redirect_stdout(io.StringIO()):
    probe.table("t", [("ok", 2, 4), ("bad", 3, 7), ("boom", None, 0)], lambda x: x * 2)
check("check.table: one pass, two failures, no crash", (probe.passed, probe.failed) == (1, 2))

check.done()
