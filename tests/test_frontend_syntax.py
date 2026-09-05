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
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INDEX = _ROOT / "frontend" / "index.html"


def _inline_scripts(html: str) -> list[str]:
    return [m.group(1) for m in
            re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)]


def test_every_inline_script_parses():
    node = shutil.which("node")
    html = _INDEX.read_text(encoding="utf-8")
    scripts = _inline_scripts(html)
    assert scripts, "index.html carries its app logic inline - none found?"
    if not node:
        print("  (node not installed - JS syntax check skipped)")
        return
    tmp = pathlib.Path(tempfile.mkdtemp())
    for i, src in enumerate(scripts):
        p = tmp / f"inline_{i}.js"
        p.write_text(src, encoding="utf-8")
        r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, f"inline script #{i} does not parse:\n{r.stderr[:800]}"


def test_no_js_string_literal_spans_a_line():
    """Cheap node-free guard for the exact failure: a single-quoted string
    that opens in a prompt()/alert() call and never closes on its line."""
    html = _INDEX.read_text(encoding="utf-8")
    for n, line in enumerate(html.split("\n"), 1):
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
