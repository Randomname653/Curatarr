"""The single-file frontend must parse — all of it, or nothing runs.

2026-09-05: a patch inserted a literal newline inside a JS string literal
in index.html. The whole inline script failed to parse, so window.onload
never ran, so the setup overlay (visible by default, hidden by JS) greeted
the owner with "Curatarr Setup" on a fully configured install. Python-side
tests could not see it. Node can: every inline <script> block goes through
`node --check`. Skips honestly when node is not installed (CI has it).

    python tests/test_frontend_syntax.py
"""
import pathlib
import re
import shutil
import subprocess
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))   # `tests` is a package only from the repo root
from tests.frontend_files import script

_ROOT = pathlib.Path(__file__).resolve().parents[1]





def test_app_js_parses():
    node = shutil.which("node")
    if not node:
        print("  (node not installed - JS syntax check skipped)")
        return
    
    app_js = _ROOT / "frontend" / "js" / "app.js"
    assert app_js.exists(), "frontend/js/app.js is missing"
    
    r = subprocess.run([node, "--check", str(app_js)], capture_output=True, text=True)
    assert r.returncode == 0, f"app.js does not parse:\n{r.stderr[:800]}"


def test_no_js_string_literal_spans_a_line():
    """Cheap node-free guard for the exact failure: a single-quoted string
    that opens in a prompt()/alert() call and never closes on its line."""
    js = script()
    for n, line in enumerate(js.split("\n"), 1):
        s = line.strip()
        if re.match(r"(const|let|var)\s+\w+\s*=\s*(prompt|alert|confirm)\('", s):
            assert s.count("'") % 2 == 0 or s.endswith("',") or s.endswith("');"), \
                f"line {n}: string literal appears to span lines: {s[:80]}"


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    sys.exit(1 if fails else 0)
