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
from html.parser import HTMLParser

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INDEX = _ROOT / "frontend" / "index.html"


class _ScriptCollector(HTMLParser):
    """Bodies of the inline <script> blocks (no src=), via the stdlib parser:
    tag names are case-insensitive, a '</div>' inside a JS string is data,
    and no regex has to impersonate an HTML parser (CodeQL py/bad-tag-filter)."""

    def __init__(self):
        super().__init__(convert_charrefs=False)   # raw text, byte for byte
        self.scripts: list[str] = []
        self._buf = None

    def handle_starttag(self, tag, attrs):
        if tag == "script" and not any(k == "src" for k, _ in attrs):
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "script" and self._buf is not None:
            self.scripts.append("".join(self._buf))
            self._buf = None

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)

    def close(self):
        super().close()
        if self._buf is not None:   # unterminated <script>: hand it to node so it fails loudly
            self.scripts.append("".join(self._buf) + self.rawdata)
            self._buf = None


def _inline_scripts(html: str) -> list[str]:
    c = _ScriptCollector()
    c.feed(html)
    c.close()
    return c.scripts


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
