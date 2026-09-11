"""The UI grammar holds — and the old mechanisms only ever go down.

2026-09-06: the frontend had four ways to confirm or report (native
alert()/confirm()/prompt(), hand-built style.cssText overlays, button-text
mutation) plus a showToast() that was called but never defined, and ~830
inline style="…" attributes standing in for classes. The shared helpers
(toast, openModal, confirmDialog, menuHtml, pagerHtml, btnBusy/btnDone,
setBadge, emptyHtml) replace them view by view. This suite pins the counts
so they can only fall: lower a ceiling when you retire a call, never raise
one.

2026-09-11: index.html became three files (markup, css/app.css, js/app.js
as an ES module). The regions are the files now; the ceilings did not move.
A module has its own scope, so every function an inline on*="…" handler
names must be handed to window explicitly — the last test pins that list.

    python tests/test_frontend_hygiene.py
"""
import re
import sys

import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))   # `tests` is a package only from the repo root
from tests.frontend_files import markup, script, styles

# Ceilings, not targets. Step 1 of the UI-grammar work pinned the numbers as
# they stood after the foundation landed; every later step lowers them
# (Step 2, the Knowledge Base: 635 -> 468 inline styles in JS, 198 -> 153 static;
# Step 3, Deletions/Curation/Recs/Report: 468 -> 388 / 153 -> 140, alert 16 -> 7;
# Step 4, Activity/Admin/Settings/Libraries/arr/Reclassify: 388 -> 218 / 140 -> 60;
# Step 5, the sweep: alert/confirm/prompt/cssText 0 — the floor, not a ceiling).
CEILINGS = {
    "alert(": 0,
    "confirm(": 0,
    "prompt(": 0,
    "style.cssText": 0,
    'style= in JS templates': 218,
    'style= in static markup': 60,
}


def _regions():
    """(static markup, app module) — the two places an inline style can hide."""
    return markup(), script()


def _count(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text))


def test_old_mechanisms_only_go_down():
    static, js = _regions()
    code = re.sub(r"(?m)^\s*//.*$", "", js)   # comments may still name the retired calls
    seen = {
        "alert(": _count(r"(?<![\w.])alert\(", code),
        "confirm(": _count(r"(?<![\w.])confirm\(", code),       # confirmDialog( does not match
        "prompt(": _count(r"(?<![\w.])prompt\(", code),
        "style.cssText": _count(r"\.style\.cssText", code),
        'style= in JS templates': _count(r'style="', js),
        'style= in static markup': _count(r'style="', static),
    }
    over = {k: (v, CEILINGS[k]) for k, v in seen.items() if v > CEILINGS[k]}
    assert not over, f"ceiling exceeded (seen, ceiling): {over}"
    print("  counts:", ", ".join(f"{k}={v}/{CEILINGS[k]}" for k, v in seen.items()))


def test_shared_helpers_exist_and_every_toast_call_resolves():
    _, js = _regions()
    for needle in ("function toast(", "const showToast = toast", "function openModal(",
                   "function closeModal(", "function confirmDialog(", "function menuHtml(",
                   "function pagerHtml(", "function btnBusy(", "function btnDone(",
                   "function setBadge(", "function emptyHtml(", "function _fmtRel("):
        assert needle in js, f"missing shared helper: {needle}"
    # showToast was called from the coverage banner for months without a
    # definition — a ReferenceError after the API call had already succeeded.
    if "showToast(" in js:
        assert "const showToast = toast" in js or "function showToast(" in js
    for gone in ("showDeleteConfirmModal", "showBulkDeleteConfirmModal", "_getOrCreateToastContainer",
                 "game-toast", "btn-yes-game"):
        assert gone not in js, f"retired mechanism is back: {gone}"


def test_no_second_overlay_or_toast_system():
    _, js = _regions()
    css = styles()
    # Only the shared root may be positioned as a full-screen overlay from JS.
    assert _count(r"position:fixed;inset:0", js) == 0, "an overlay is being hand-built in JS again"
    for cls in (".modal ", ".toast ", ".menu-list ", ".select-bar ", ".empty ", ".pager ", ".toolbar ", ".section-head"):
        assert cls in css, f"grammar class missing from the stylesheet: {cls.strip()}"
    assert "DESIGN LANGUAGE" in css, "the design-language header must travel with the stylesheet"


def test_every_inline_handler_resolves_to_an_exported_global():
    """app.js is an ES module: nothing in it is global unless the
    Object.assign(window, {...}) block at its end says so. A handler that
    names a function outside that block is a ReferenceError on the first
    click — and no Python test would notice without this one."""
    m, s = _regions()
    both = m + "\n" + s
    needed = set(re.findall(r'\bon[a-z]+="([A-Za-z_$][\w$]*)\(', both))
    needed |= set(re.findall(r"""(?:setTimeout|setInterval)\(\s*['"]([A-Za-z_$][\w$]*)\(""", both))
    needed.discard("if")   # onclick="if (...) ..." is a statement, not a call
    # Calls spelled inside strings reach a handler at runtime too: _errHtml's
    # retry button, menuHtml items ({call: 'onFixMatch(this)'}), pagerHtml,
    # emptyHtml's CTA. Ten of those were missing on the first split.
    code = re.sub(r"(?m)^\s*//.*$", "", s)
    top_level = set(re.findall(r"^(?:async )?function ([A-Za-z_$][\w$]*)\(", code, re.M))
    in_strings = set(re.findall(r"""['"`]\s*([A-Za-z_$][\w$]*)\(""", code))
    needed |= in_strings & top_level
    block = re.search(r"Object\.assign\(window,\s*\{(.*?)\}\s*\);", s, re.S)
    assert block, "app.js must end with the Object.assign(window, {...}) block"
    exposed = {n.strip() for n in block.group(1).split(",") if n.strip()}
    missing = sorted(needed - exposed)
    assert not missing, f"inline handlers reference functions app.js does not expose: {missing}"
    unused = sorted(exposed - needed)
    assert not unused, f"exposed but no handler names them (export only what is referenced): {unused}"
    print(f"  {len(exposed)} functions exposed for {len(needed)} handler references")


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
