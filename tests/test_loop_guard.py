"""The loop guard sees database statements on the event-loop thread
(src/database/loop_guard.py).

  * a statement from a coroutine is recorded under its src/ call site
  * the same call through run_read / run_write is not
  * raise mode turns it into LoopBlockingQuery (for loop-free pins)
  * the lifespan's own startup counts as lifecycle, not as a site
  * GET /api/system/loop-guard is admin-only and returns the snapshot

Runs against a temp file database bound to the app's session factory.

    python tests/test_loop_guard.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402

import src.database.connection as conn_mod  # noqa: E402
from src.database import loop_guard  # noqa: E402
from src.database.models import Base  # noqa: E402
from src.database.runner import run_read, run_write  # noqa: E402
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
eng = create_engine(f"sqlite:///{(tmp / 'guard.db').as_posix()}",
                    connect_args={"check_same_thread": False})
Base.metadata.create_all(eng)
conn_mod.SessionLocal.configure(bind=eng)
loop_guard.install(eng)
loop_guard.install(eng)   # idempotent: one listener, one count per statement
loop_guard.serving(True)


def _sites():
    return {s["site"]: s["statements"] for s in loop_guard.snapshot()["sites"]}


# ── 1. on the loop vs. off it ───────────────────────────────────────────────
print("statements on the loop thread")
loop_guard.reset()


async def _on_loop():
    app_state.get_state("guard_probe")


asyncio.run(_on_loop())
sites = _sites()
check("a get_state from a coroutine is recorded", len(sites) == 1)
site = next(iter(sites), "")
check("under the src/ frame that issued it, not src/database/",
      site.startswith("src/services/app_state.py:") and "(get_state)" in site)
check("one listener per engine: one statement, one count", sites.get(site) == 1)

# A coroutine in src/ that calls a sync helper is named itself: that is the
# code that should have awaited a worker. (Compiled under a src/ filename.)
_ns = {"get_state": app_state.get_state}
exec(compile("async def caller():\n    get_state('guard_probe')\n",
             str(ROOT / "src" / "services" / "guard_probe.py"), "exec"), _ns)
loop_guard.reset()
asyncio.run(_ns["caller"]())
check("the innermost src/ coroutine is the site, not the helper it called",
      list(_sites()) == ["src/services/guard_probe.py:2 (caller)"])
check("... and is marked as a src/ coroutine (what run_all fails on)",
      [x["coroutine"] for x in loop_guard.snapshot()["sites"]] == [True])
loop_guard.reset()
asyncio.run(_on_loop())
check("a test's own coroutine calling a sync src/ helper is not marked",
      [x["coroutine"] for x in loop_guard.snapshot()["sites"]] == [False])


async def _off_loop():
    await run_read(app_state.get_state, "guard_probe")
    await run_write(app_state.set_state, "guard_probe", "1")


loop_guard.reset()
asyncio.run(_off_loop())
check("the same calls through run_read / run_write are not", _sites() == {})


async def _wrappers():
    await app_state.aset_state("guard_probe", "2")
    got = [await app_state.aget_state("guard_probe")]
    got.append(await app_state.aacquire_state_lock("guard_lock"))
    got.append(await app_state.aacquire_state_lock("guard_lock"))
    got.append(await app_state.arelease_state_lock("guard_lock"))
    got.append(await app_state.aset_states_if_changed({"guard_probe": "3"}))
    return got


loop_guard.reset()
check("the app_state a*-wrappers keep their results and stay off the loop",
      asyncio.run(_wrappers()) == ["2", True, False, True, {"guard_probe": "2"}]
      and _sites() == {})
check("a call with no running loop is not", (app_state.get_state("guard_probe"), _sites())[1] == {})

# ── 2. raise mode ───────────────────────────────────────────────────────────
print("raise mode")
os.environ["CURATARR_LOOP_GUARD"] = "raise"
raised = []
try:
    asyncio.run(_on_loop())
except loop_guard.LoopBlockingQuery as e:
    raised.append(str(e))
finally:
    os.environ.pop("CURATARR_LOOP_GUARD", None)
check("a statement on the loop raises LoopBlockingQuery",
      raised and "src/services/app_state.py" in raised[0])

# ── 3. lifecycle phase ──────────────────────────────────────────────────────
print("lifespan startup")
loop_guard.reset()
loop_guard.serving(False)
asyncio.run(_on_loop())
snap = loop_guard.snapshot()
loop_guard.serving(True)
check("startup statements count as lifecycle, not as sites",
      snap["sites"] == [] and snap["lifecycle_statements"] >= 1)
check("main.py's lifespan flips the guard around its yield",
      (lambda src: src.count("loop_guard.serving(False)") == 2
       and "loop_guard.serving(True)\n    yield" in src)(
          (ROOT / "src" / "main.py").read_text(encoding="utf-8")))

# ── 4. endpoint ─────────────────────────────────────────────────────────────
print("admin endpoint")
from src.main import app  # noqa: E402

route = next((r for r in app.routes if getattr(r, "path", "") == "/api/system/loop-guard"), None)
check("GET /api/system/loop-guard exists", route is not None and "GET" in route.methods)
deps = [d.call.__name__ for d in route.dependant.dependencies] if route else []
check("and is admin-only", any("admin" in n for n in deps))
loop_guard.reset()
asyncio.run(_on_loop())
body = route.endpoint() if route else {}
check("it returns the sites with counts",
      body.get("statements_on_loop") == 1 and body["sites"][0]["site"].startswith("src/services/"))
check("both engines carry the hook", id(conn_mod.engine) in loop_guard._installed)

# ── 5. the battery dump ─────────────────────────────────────────────────────
print("dump for tests/run_all.py")
import subprocess  # noqa: E402

dump = tmp / "dump"
dump.mkdir()
code = (
    "import asyncio, sys; sys.path.insert(0, %r)\n"
    "from sqlalchemy import create_engine\n"
    "from src.database import loop_guard\n"
    "eng = create_engine('sqlite://'); loop_guard.install(eng)\n"
    "async def main():\n"
    "    with eng.connect() as c: c.exec_driver_sql('SELECT 1')\n"
    "asyncio.run(main())\n" % str(ROOT))
subprocess.run([sys.executable, "-c", code], check=True,
               env={**os.environ, "CURATARR_LOOP_GUARD_DUMP": str(dump)})
files = list(dump.glob("*.json"))
import json  # noqa: E402
check("with CURATARR_LOOP_GUARD_DUMP set, a process writes its sites on exit",
      len(files) == 1 and json.loads(files[0].read_text())["statements_on_loop"] == 1)

sys.path.insert(0, str(ROOT / "tests"))
import run_all  # noqa: E402

(dump / "1.json").write_text(json.dumps({"argv": ["x", "tests/test_x.py"], "sites": [
    {"site": "src/services/a.py:1 (job)", "statements": 2, "coroutine": True},
    {"site": "src/services/b.py:9 (helper)", "statements": 1, "coroutine": False},
    {"site": "src/services/guard_probe.py:2 (caller)", "statements": 1, "coroutine": True}]}))
check("run_all fails on a src/ coroutine site, not on a test's own or the probe",
      list(run_all.loop_guard_sites(str(dump))) == ["src/services/a.py:1 (job)"])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
