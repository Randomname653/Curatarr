"""Run the whole test battery — the one command CI and humans share.

    python tests/run_all.py

No pytest, no venv (project convention): each suite is a plain script.
Two styles exist and both are handled:

* self-running suites (the stdlib ``check()`` runner, unittest files,
  drift scripts) — executed as a subprocess. A suite counts as failed on
  a non-zero exit OR a "N failed" summary line with N > 0 (some older
  runners print the count but never set an exit code).
* bare function suites (module-level ``def test_*`` with no runner —
  running them with plain python would execute NOTHING and exit 0, a
  false green) — imported and every argless ``test_*`` called, in a
  subprocess per file (``--bare``) so module state can't bleed across
  suites.

Every suite also runs with the loop guard dumping its sites
(src/database/loop_guard.py): a database statement that ran on the event
loop from a src/ coroutine anywhere in the battery fails the run, even
when no suite asserts on it.

Every suite gets its own temp database, vector store, enrichment cache and
.env (``suite_env``), so none reaches the developer's data/ — and the data/
guard fails the run if one still does. That is also what lets the suites
run side by side, one per CPU by default:

    python tests/run_all.py --serial     # one at a time; names a data/ writer
    python tests/run_all.py -j4          # or CURATARR_TEST_JOBS=4

Where is a module tested? The suites carry the name of the audit that wrote
them (loop_free_g3, fail_closed_s2_g1, ...), not of their subsystem:

    python tests/run_all.py --for vector_reconcile --list   # which suites
    python tests/run_all.py --for src/services/wrapup_facts.py   # run them
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# The children are pinned to UTF-8 below; the PARENT prints their last line,
# which on a cp1252 Windows console died on the first suite whose final
# stderr line carried an arrow or an accented title (a logging line, not a
# failure) — and took the whole battery down with it.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# The vector store admits one process at a time. Every suite now gets its own
# temp store (suite_env), so this should never fire; if it does, a suite
# bypassed the redirect and opened data/chromadb while the app holds it.
LOCK_HINT = "in use by another process"

_BARE_MARKERS = re.compile(r"unittest\.main|sys\.exit|__main__|check\.done\(\)")

# Coroutine sites the battery may produce on purpose: test_loop_guard compiles
# a probe coroutine under this src/ path to check the guard's attribution.
_LOOP_GUARD_ALLOWED = ("src/services/guard_probe.py:",)


def loop_guard_sites(dump_dir: str) -> dict:
    """{site: [suite, ...]} for statements that ran on the event loop from a
    src/ coroutine, over every process that dumped into ``dump_dir``."""
    out: dict = {}
    for f in sorted(Path(dump_dir).glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        suite = Path(d.get("argv", ["?"])[-1]).name
        for s in d.get("sites", []):
            if s.get("coroutine") and not s["site"].startswith(_LOOP_GUARD_ALLOWED):
                out.setdefault(s["site"], []).append(suite)
    return out


def is_bare(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    return bool(re.search(r"^def test_", text, re.M)) and not _BARE_MARKERS.search(text)


def run_bare(path: Path) -> int:
    """--bare mode: import the module, call every argless test_* function."""
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    passed = failed = 0
    for name in sorted(dir(mod)):
        fn = getattr(mod, name)
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"  PASS  {name}")
            except Exception as e:
                failed += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


DATA = ROOT / "data"


def data_snapshot() -> dict:
    """{path: (mtime_ns, size)} for every file under the developer's data/
    (logs excepted: the test log lives there on purpose)."""
    if not DATA.exists():
        return {}
    out = {}
    for p in DATA.rglob("*"):
        if p.is_file() and "logs" not in p.relative_to(DATA).parts:
            try:
                st = p.stat()
            except OSError:
                continue
            out[str(p.relative_to(DATA))] = (st.st_mtime_ns, st.st_size)
    return out


def data_changes(before: dict, after: dict) -> list:
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def suite_env(base: dict) -> dict:
    """Every store the app opens, pointed at a private temp dir per suite —
    the live curatarr.db, chromadb, enrichment cache (and plex_music.db next
    to it) and the developer's .env never reach a test. A suite that sets its
    own paths still wins: it overrides these in-process."""
    tmp = Path(tempfile.mkdtemp(prefix="curatarr-suite-"))
    (tmp / "cache").mkdir()
    return {**base,
            "CURATARR_SUITE_TMP": str(tmp),
            "DATABASE_URL": f"sqlite:///{(tmp / 'curatarr.db').as_posix()}",
            "CHROMADB_PATH": str(tmp / "chromadb"),
            "ENRICHMENT_CACHE": str(tmp / "cache" / "enrichment.db"),
            "CURATARR_ENV_FILE": str(tmp / ".env")}


def run_suite(f: Path, env: dict) -> dict:
    """One suite in its own process with its own temp stores."""
    cmd = ([sys.executable, __file__, "--bare", str(f)] if is_bare(f)
           else [sys.executable, str(f)])
    senv = suite_env(env)
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, cwd=ROOT, env=senv, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
    finally:
        shutil.rmtree(senv["CURATARR_SUITE_TMP"], ignore_errors=True)
    out = (r.stdout or "") + (r.stderr or "")
    summary = re.search(r"(\d+) passed, (\d+) failed", out)
    bad = r.returncode != 0 or bool(summary and summary.group(2) != "0")
    tail = summary.group(0) if summary else (out.strip().splitlines()[-1:] or ["(no output)"])[0]
    return {"name": f.name, "bad": bad, "tail": tail, "out": out,
            "secs": time.monotonic() - t0}


def _module_names(target: str) -> tuple:
    """``src/services/vector_reconcile.py`` / ``src.services.vector_reconcile``
    / ``vector_reconcile`` -> (dotted name, file path, bare name)."""
    t = target.strip().removesuffix(".py").replace("\\", "/")
    dotted = t.replace("/", ".")
    if not dotted.startswith("src."):
        hits = sorted(ROOT.glob(f"src/**/{dotted.rsplit('.', 1)[-1]}.py"))
        if len(hits) == 1:
            dotted = ".".join(hits[0].relative_to(ROOT).with_suffix("").parts)
    return dotted, dotted.replace(".", "/") + ".py", dotted.rsplit(".", 1)[-1]


def suites_for(target: str, files: list) -> list:
    """The suites that exercise ``target``: they import it (any spelling) or
    read its source. Answers "where is X tested" across the suites that are
    named after the audit that wrote them, not after their subsystem."""
    dotted, path, bare = _module_names(target)
    parent, _, leaf = dotted.rpartition(".")
    pats = [re.compile(r"\b(import|from)\s+" + re.escape(dotted) + r"\b"),
            re.compile(r"\bfrom\s+" + re.escape(parent) + r"\s+import\s+[^\n]*\b" + re.escape(leaf) + r"\b"),
            re.compile(re.escape(path))]
    return [f for f in files if any(p.search(f.read_text(encoding="utf-8")) for p in pats)]


def jobs_wanted() -> int:
    """--serial, -jN / --jobs N, CURATARR_TEST_JOBS, else one per CPU."""
    args = sys.argv[1:]
    if "--serial" in args:
        return 1
    for i, a in enumerate(args):
        if a.startswith("-j") and a[2:].isdigit():
            return max(1, int(a[2:]))
        if a == "--jobs" and i + 1 < len(args) and args[i + 1].isdigit():
            return max(1, int(args[i + 1]))
    env = os.environ.get("CURATARR_TEST_JOBS", "")
    return max(1, int(env)) if env.isdigit() else (os.cpu_count() or 1)


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--bare":
        return run_bare(Path(sys.argv[2]))

    files = sorted(TESTS.glob("test_*.py"))
    targets = [a for i, a in enumerate(sys.argv[1:], 1) if sys.argv[i - 1] == "--for"]
    if targets:
        files = sorted({f for t in targets for f in suites_for(t, files)})
        print(f"{len(files)} suite(s) exercise {', '.join(targets)}: "
              + ", ".join(f.name for f in files))
        if "--list" in sys.argv:
            return 0
        if not files:
            return 1
    # PYTHONIOENCODING: a captured child inherits the console codepage, so on a
    # cp1252 Windows shell a suite printing an arrow or an accented title dies
    # in print() and reads as a test failure. Pin UTF-8 so the result does not
    # depend on which terminal launched it.
    dump_dir = tempfile.mkdtemp(prefix="curatarr-loop-guard-")
    env = {**os.environ,
           "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8",
           "CURATARR_LOOP_GUARD_DUMP": dump_dir}
    jobs = jobs_wanted()
    failures, locked, touched, results = [], [], {}, []
    t0 = time.monotonic()

    def report(res: dict) -> None:
        results.append(res)
        print(f"{'FAIL' if res['bad'] else 'ok  '}  {res['name']}  {res['tail']}", flush=True)
        if res["bad"]:
            failures.append(res["name"])
            if LOCK_HINT in res["out"]:
                locked.append(res["name"])
            else:
                print(res["out"][-2000:], flush=True)

    if jobs == 1:
        # Serial: the data/ guard can name the suite that touched data/.
        for f in files:
            before = data_snapshot()
            res = run_suite(f, env)
            changed = data_changes(before, data_snapshot())
            if changed:
                touched[f.name] = changed
            report(res)
    else:
        # Every suite has its own stores, so they run side by side; the
        # data/ guard then sees the whole battery at once.
        before = data_snapshot()
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for fut in as_completed([pool.submit(run_suite, f, env) for f in files]):
                report(fut.result())
        changed = data_changes(before, data_snapshot())
        if changed:
            touched[f"(one of the suites, {jobs} at a time — rerun with --serial to name it)"] = changed
    print(f"\n{len(files) - len(failures)}/{len(files)} suites green in "
          f"{time.monotonic() - t0:.0f} s ({jobs} at a time)"
          + (f" — FAILED: {', '.join(sorted(failures))}" if failures else ""))
    slowest = sorted(results, key=lambda r: -r["secs"])[:5]
    print("slowest: " + ", ".join(f"{r['name']} {r['secs']:.1f} s" for r in slowest))
    on_loop = loop_guard_sites(dump_dir)
    for site, suites in sorted(on_loop.items()):
        print(f"FAIL  loop guard: database statement on the event loop at {site} "
              f"(seen in {', '.join(sorted(set(suites))[:3])})")
    if on_loop:
        print(f"{len(on_loop)} event-loop database site(s): move them to "
              "run_read/run_write/run_chroma or asyncio.to_thread")
        failures.append("loop guard")
    for name, paths in sorted(touched.items()):
        print(f"FAIL  {name} wrote to the live data/: {', '.join(paths[:5])}")
    if touched:
        print(f"{len(touched)} suite(s) touched data/: route the store through "
              "tests/_harness.py or a temp path")
        failures.append("data/ guard")
    if locked:
        # Worth saying plainly: not a code regression, a suite that reached
        # the live data/chromadb (the data/ guard above names it too).
        print(f"\n{len(locked)} of those hit a vector store held by another "
              f"process: they opened the live data/chromadb instead of their temp one.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
