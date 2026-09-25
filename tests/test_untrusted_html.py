"""Model output must not be able to fire app actions, and esc() must hold in
quoted attributes.

2026-09-25: the chat bubble rendered streamed model output through
DOMPurify.sanitize(marked.parse(...)) with the default config, which keeps
every data-* attribute. The dispatcher in app.js runs any registered action
named in data-action / data-on-<event> — error in the capture phase, so
<img src=x data-on-error="setPrinciple" data-args-error='["../deletions/42",
"approve",null]'> fired with no click, and setPrinciple pasted "../" into its
POST path. Every sanitize now goes through ui.js (ALLOW_DATA_ATTR: false plus
a hook for the dispatcher's attributes), and the handlers encode their path
segments. Separately esc() did not escape quotes although ~14 templates put
it inside title="..." / value="...".

    python tests/test_untrusted_html.py
"""
import pathlib
import re
import shutil
import subprocess
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))   # `tests` is a package only from the repo root
from tests.frontend_files import modules

_UI = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "js" / "ui.js"


def _code(path: pathlib.Path) -> str:
    """The module without // comments (they may name the retired calls)."""
    return re.sub(r"(?m)^\s*//.*$", "", path.read_text(encoding="utf-8"))


def test_only_ui_js_calls_dompurify_or_marked():
    offenders = []
    for p in modules():
        if p.name == "ui.js":
            continue
        code = _code(p)
        if "DOMPurify.sanitize" in code or "marked.parse" in code:
            offenders.append(p.name)
    assert not offenders, f"sanitize/marked outside ui.js's helper: {offenders} — use renderMarkdown()/sanitizeHtml()"


def test_ui_sanitize_drops_data_and_dispatch_attributes():
    code = _code(_UI)
    calls = re.findall(r"DOMPurify\.sanitize\(([^;]*)\);", code)
    assert calls, "ui.js has no DOMPurify.sanitize call"
    for c in calls:
        assert re.search(r"ALLOW_DATA_ATTR\s*:\s*false", c), f"sanitize without ALLOW_DATA_ATTR:false: {c}"
    assert "uponSanitizeAttribute" in code, "the hook that strips the dispatcher's attributes is gone"
    m = re.search(r"const _DISPATCH_ATTR = /(.+)/(\w*);", code)
    assert m, "_DISPATCH_ATTR regex missing"
    rx = re.compile(m.group(1), re.I if "i" in m.group(2) else 0)
    for attr in ("data-action", "data-args", "data-args-error", "data-on-error", "data-on-click", "DATA-ON-TOGGLE"):
        assert rx.search(attr), f"{attr} would survive the hook"
    for attr in ("data-title", "href", "class"):
        assert not rx.search(attr), f"{attr} is not a dispatcher attribute"
    # marked.parse only ever feeds the sanitizer
    for c in re.findall(r"[^\n]*marked\.parse\([^\n]*", code):
        assert "sanitizeHtml(" in c, f"marked.parse output not sanitized: {c.strip()}"


def test_action_paths_encode_their_arguments():
    """The two handlers the exploit used; a bare "+ id +" or "${taskId}" is back."""
    cur = _code(_UI.parent / "curation.js")
    act = _code(_UI.parent / "activity.js")
    assert "'/api/recommendations/principles/' + encodeURIComponent(id) + '/' + encodeURIComponent(action)" in cur
    assert "`/api/tasks/${encodeURIComponent(taskId)}/cancel`" in act


def test_esc_escapes_quotes():
    code = _UI.read_text(encoding="utf-8")
    m = re.search(r"^export function esc\(s\)\{.*\}$", code, re.M)
    assert m, "esc() not found as a one-liner in ui.js"
    src = m.group(0).removeprefix("export ")
    node = shutil.which("node")
    if not node:
        # Node-free fallback: the replacements are there.
        for needle in ("&quot;", "&#39;", "&amp;", "&lt;", "&gt;"):
            assert needle in src, f"esc() lost {needle}"
        print("  (node not installed - esc() checked statically)")
        return
    js = src + "\nprocess.stdout.write(esc(`a\"b'c<d>&e`));"
    r = subprocess.run([node, "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:500]
    assert r.stdout == "a&quot;b&#39;c&lt;d&gt;&amp;e", r.stdout


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
