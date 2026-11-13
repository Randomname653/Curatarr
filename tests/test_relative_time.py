"""Relative times ("in 3 d", "2 h ago") and the UI's words function t()
(UX design record 3.8).

_fmtRel declared a local ``const t = new Date(iso).getTime()`` and then
called ``t('time.now')``: the number shadowed the imported t(), and every
relative time with a valid date threw "t is not a function" - Activity's
Scheduled and History tabs, the KB, Deletions, the arr browsers, nodes.
This runs _fmtRel in node in both languages, and guards every module
that imports t(): a local named ``t`` must not be followed by a t('…')
call in its own block.

    python tests/test_relative_time.py
"""
from _harness import check  # noqa: I001 - first: temp stores
from _i18n_js import i18n_js

import json
import re
import shutil
import subprocess
from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "frontend" / "js"
UI = (JS / "ui.js").read_text(encoding="utf-8")


def _fn(src, name):
    m = re.search(r"export function " + name + r"\(", src)
    end = src.index("\n}\n", m.start()) + 3
    return src[m.start():end].removeprefix("export ")


node = shutil.which("node")
if node:
    def run(lang):
        body = i18n_js(lang) + _fn(UI, "_fmtRel") + """
const now = Date.now(), iso = ms => new Date(now + ms).toISOString();
console.log(JSON.stringify([_fmtRel(iso(-2 * 3600e3)), _fmtRel(iso(3 * 86400e3)), _fmtRel(iso(5e3)),
                            _fmtRel(iso(-10 * 60e3)), _fmtRel(''), _fmtRel('not a date')]));
"""
        r = subprocess.run([node, "-"], input=body, capture_output=True, text=True, encoding="utf-8")
        if r.returncode:
            print(r.stderr[:600])
            return None
        return json.loads(r.stdout.strip().splitlines()[-1])

    en, de = run("en"), run("de")
    check("a relative time is drawn, not thrown (was: 't is not a function')", en is not None and de is not None)
    check("...in English: '2 h ago', 'in 3 d', 'now', '10 min ago'; nothing for no date",
          en == ["2 h ago", "in 3 d", "now", "10 min ago", "", ""], en)
    check("...and in German", de is not None and de[0] != en[0] and de[4:] == ["", ""] and all(de[:4]), de)
else:
    print("  (node not installed - the relative-time rig is skipped)")


def shadowed(src):
    """(line, text) of each local named t followed, in its own block, by a t('…') call."""
    lines = src.splitlines()
    out = []
    local = re.compile(r"\b(?:const|let|var)\s+t\b(?!\w)")         # to the end of its block
    param = re.compile(r"\(\s*t\s*[,)]|(?<![\w.])t\s*=>")              # an arrow's: its own line
    ask = re.compile(r"(?<![\w.])t\(\s*['`]")
    for i, line in enumerate(lines):
        code = re.sub(r"//.*$", "", line)
        m = param.search(code)
        if m and ask.search(code[m.end():]):
            out.append((i + 1, line.strip()))
            continue
        m = local.search(code)
        if not m:
            continue
        depth, rest = 0, code[m.end():]
        for j in range(i, min(len(lines), i + 400)):
            seg = rest if j == i else re.sub(r"//.*$", "", lines[j])
            if ask.search(seg):
                out.append((i + 1, line.strip()))
                break
            depth += seg.count("{") - seg.count("}")
            if depth < 0:
                break
    return out


# the modules that import t() (a selection, not a check)
_IMPORTS_T = re.compile(r"import \{[^}]*\bt\b[^}]*\} from '\./i18n\.js'")
sources = {p.name: p.read_text(encoding="utf-8") for p in sorted(JS.glob("*.js"))}
hits = {name: shadowed(src) for name, src in sources.items() if _IMPORTS_T.search(src)}
bad = {k: v for k, v in hits.items() if v}
check("no module shadows t() with a local t and then asks it for words", not bad, bad)
check("...the guard sees the old bug", shadowed(
    "function f(iso) {\n  const t = new Date(iso).getTime();\n  if (a) return t('time.now');\n}\n") != [])
check("...and leaves a harmless local t alone", shadowed(
    "function g(e) {\n  const t = e.touches[0];\n  x = t.clientX;\n}\nfunction h() { return t('a.b'); }\n") == [])
check.done()
