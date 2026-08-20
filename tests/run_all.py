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
"""
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

_BARE_MARKERS = re.compile(r"unittest\.main|sys\.exit|__main__")


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


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--bare":
        return run_bare(Path(sys.argv[2]))

    files = sorted(TESTS.glob("test_*.py"))
    env = dict(**__import__("os").environ, PYTHONPATH=str(ROOT))
    failures = []
    for f in files:
        cmd = ([sys.executable, __file__, "--bare", str(f)] if is_bare(f)
               else [sys.executable, str(f)])
        r = subprocess.run(cmd, cwd=ROOT, env=env,
                           capture_output=True, text=True)
        out = (r.stdout or "") + (r.stderr or "")
        summary = re.search(r"(\d+) passed, (\d+) failed", out)
        bad = r.returncode != 0 or (summary and summary.group(2) != "0")
        tail = summary.group(0) if summary else out.strip().splitlines()[-1:] or ["(no output)"]
        print(f"{'FAIL' if bad else 'ok  '}  {f.name}  {tail if isinstance(tail, str) else tail[0]}")
        if bad:
            failures.append(f.name)
            print(out[-2000:])
    print(f"\n{len(files) - len(failures)}/{len(files)} suites green"
          + (f" — FAILED: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
