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

# ── 6. real_state: the real app_state table behind a dict's face ───────────
from _checks import real_state  # noqa: E402
from src.services import app_state as _as  # noqa: E402
st = real_state()
st.clear()
st["harness:a"] = "1"
check("real_state: a write lands in the real table", _as.get_state("harness:a") == "1")
_as.set_state("harness:b", "2")
check("real_state: the app's own writes are visible", st.get("harness:b") == "2" and "harness:b" in st)
check("real_state: keys, items and len read the table",
      set(st.keys()) == {"harness:a", "harness:b"} and len(st) == 2 and dict(st.items())["harness:a"] == "1")
check("real_state: a missing key is a KeyError / the default",
      st.get("nope", "d") == "d" and "nope" not in st)
check("real_state: pop and del remove the row", st.pop("harness:a") == "1" and _as.get_state("harness:a") is None)
st.update({"harness:c": "3"})
st.clear()
check("real_state: clear empties the table", len(st) == 0 and _as.get_state("harness:b") is None)

import asyncio  # noqa: E402
from src.database import loop_guard  # noqa: E402
loop_guard.reset()


async def _bookkeeping():
    st["harness:loop"] = "x"            # a suite's fake snapshotting state on the loop
    return st.get("harness:loop"), dict(st.items())

got, snap = asyncio.run(_bookkeeping())
check("real_state on the event loop works and stays off the loop guard",
      got == "x" and snap == {"harness:loop": "x"}
      and not [s for s in loop_guard.snapshot()["sites"] if "tests/" in s["site"] or s.get("coroutine")])

# ── 7. a stand-in for get_db_session keeps its contract ────────────────────
# connection.get_db_session commits on a clean exit and rolls back on an
# exception. Fakes that only closed made every write the app relied on that
# commit for (app_state among them) vanish without a trace.
import ast as _ast  # noqa: E402

_bad = []
for _p in sorted((ROOT / "tests").glob("test_*.py")):
    for _fn in _ast.walk(_ast.parse(_p.read_text(encoding="utf-8"))):
        if isinstance(_fn, _ast.FunctionDef) and any(
                getattr(d, "id", getattr(d, "attr", None)) == "contextmanager"
                for d in _fn.decorator_list):
            _src = _ast.unparse(_fn)
            if "yield" in _src and any(m in _src for m in ("Session(", "make()", " S()", "sessionmaker")) \
                    and ".commit()" not in _src:
                _bad.append(f"{_p.name}:{_fn.lineno} {_fn.name}")
check(f"every fake session commits on a clean exit, like get_db_session {_bad or ''}", not _bad)

# ── 8. run_all --for: which suites exercise a module ───────────────────────
_d = Path(tempfile.mkdtemp())
(_d / "test_a.py").write_text("import src.services.vector_reconcile as vr\n", encoding="utf-8")
(_d / "test_b.py").write_text("from src.services import app_state, vector_reconcile\n", encoding="utf-8")
(_d / "test_c.py").write_text("src = (ROOT / 'src/services/vector_reconcile.py').read_text()\n", encoding="utf-8")
(_d / "test_d.py").write_text("import src.services.vector_reconcile_extra\n# vector_reconcile in prose\n",
                              encoding="utf-8")
_files = sorted(_d.glob("test_*.py"))
check.table("run_all --for finds a module by every spelling", [
    ("bare name", "vector_reconcile", ["test_a.py", "test_b.py", "test_c.py"]),
    ("dotted", "src.services.vector_reconcile", ["test_a.py", "test_b.py", "test_c.py"]),
    ("path", "src/services/vector_reconcile.py", ["test_a.py", "test_b.py", "test_c.py"]),
], lambda target: [f.name for f in run_all.suites_for(target, _files)])

check.done()
