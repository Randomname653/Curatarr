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
The window block is gone; all actions are registered in a single dictionary.

    python tests/test_frontend_hygiene.py
"""
import json
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



def test_no_inline_handlers_or_window_assignments():
    m, s = _regions()
    code = re.sub(r"(?m)^\s*//.*$", "", s)
    handlers_m = re.findall(r'\bon[a-z]+="[^"]*"', m)
    handlers_s = re.findall(r'\bon[a-z]+="[^"]*"', code)
    assert not handlers_m, f"markup contains inline handlers: {handlers_m}"
    assert not handlers_s, f"script contains inline handlers: {handlers_s}"

    assert "Object.assign(window" not in code, "Object.assign(window still present"
    window_assignments = [m for m in re.findall(r'window\.[a-zA-Z_$][\w$]*\s*=', code) if 'window.onload' not in m and 'window._notifPollTimer' not in m]
    assert not window_assignments, f"window.* assignments found: {window_assignments}"

def test_referenced_actions_match_registry():
    m, s = _regions()
    code = re.sub(r"(?m)^\s*//.*$", "", s)

    referenced = set()
    for match in re.finditer(r'data-action=["\'](.*?)["\']', m + "\n" + code):
        referenced.add(match.group(1))
    for match in re.finditer(r'data-on-[a-z]+=["\'](.*?)["\']', m + "\n" + code):
        referenced.add(match.group(1))
    for match in re.finditer(r"act\(\s*['\"](.*?)['\"]", code):
        referenced.add(match.group(1))
    for match in re.finditer(r"actOn\(\s*['\"][^'\"]*?['\"]\s*,\s*['\"](.*?)['\"]", code):
        referenced.add(match.group(1))
    for match in re.finditer(r"action:\s*['\"](.*?)['\"]", code):
        referenced.add(match.group(1))

    if '${name}' in referenced:
        referenced.remove('${name}')

    actions_match = re.search(r"const actions = \{(.*?)\}", s, re.S)
    assert actions_match, "app.js must carry the const actions = {...} block"
    registry = {n.strip() for n in actions_match.group(1).split(",") if n.strip()}

    missing = registry - referenced
    surplus = referenced - registry
    print(f"  registry sizes: referenced={len(referenced)}, registry={len(registry)}")
    assert not missing, f"dead entry in registry (no references): {sorted(missing)}"
    assert not surplus, f"unregistered action referenced: {sorted(surplus)}"

def test_act_calls_use_string_literals():
    _, s = _regions()
    code = re.sub(r"(?m)^\s*//.*$", "", s)

    for match in re.finditer(r"\bact\(\s*([^,)]+)", code):
        arg = match.group(1).strip()
        if arg in ["name", "p.action"]: continue
        if not ((arg.startswith("'") and arg.endswith("'")) or (arg.startswith('\"') and arg.endswith('\"'))):
            assert False, f"act() name position must be a string literal, found: {arg}"

    for match in re.finditer(r"\bactOn\(\s*[^,]+,\s*([^,)]+)", code):
        arg = match.group(1).strip()
        if arg == "name": continue
        if not ((arg.startswith("'") and arg.endswith("'")) or (arg.startswith('\"') and arg.endswith('\"'))):
            assert False, f"actOn() name position must be a string literal, found: {arg}"


def test_data_attributes_in_markup():
    m, s = _regions()
    actions_match = re.search(r"const actions = \{(.*?)\}", s, re.S)
    assert actions_match, "app.js must carry the const actions = {...} block"
    registry = {n.strip() for n in actions_match.group(1).split(",") if n.strip()}

    # Check data-action and data-on-*
    attributes = re.findall(r'data(?:-action|-on-[a-z]+)="([^"]+)"', m)
    for attr in attributes:
        assert attr in registry, f"markup references {attr} which is not in the actions registry"

    # Check data-args validity
    args = re.findall(r'data-args=\'([^\']*)\'', m) + re.findall(r'data-args="([^"]*)"', m)
    for arg_str in args:
        try:
            parsed = json.loads(arg_str.replace('&quot;', '"').replace('&amp;', '&'))
            assert isinstance(parsed, list), f"data-args must be a JSON array, got {type(parsed)}"
        except Exception as e:
            assert False, f"invalid JSON in data-args: {arg_str} -> {e}"


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
