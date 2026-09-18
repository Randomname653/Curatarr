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
2026-09-13: every handler is a data attribute (PR 3a the markup, PR 3b the
templates and the four string-to-handler helpers). The window block is gone;
`const actions = {...}` in app.js is the only table, and the tests below pin
its symmetry with every reference, the literal names, and the variable-in-
quotes trap the 3b review found.

    python tests/test_frontend_hygiene.py
"""
import json
import re
import sys

import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))   # `tests` is a package only from the repo root
from tests.frontend_files import markup, modules, script, styles

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



IDENT = r"[A-Za-z_$][\w$]*"


def _code():
    """The modules with full-line comments removed: a comment may quote an old
    handler or an example attribute."""
    return re.sub(r"(?m)^\s*//.*$", "", script())


def test_no_inline_handlers_or_window_globals():
    """Nothing needs the global scope any more: no on*="…" anywhere, no
    Object.assign(window, …), no window.<name> = smuggling a global back in.
    Event properties (window.onload) are not globals."""
    m, code = markup(), _code()
    handlers = re.findall(r'\bon[a-z]+="[^"]*"', m) + re.findall(r'\bon[a-z]+="[^"]*"', code)
    assert not handlers, f"inline handlers are back: {handlers}"
    assert "Object.assign(window" not in code, "the window export block is back"
    smuggled = re.findall(r"window\.(?!on[a-z]+\b)(%s)\s*=[^=]" % IDENT, code)
    assert not smuggled, f"a module writes a global through window: {smuggled}"


def _referenced_actions(m, code):
    """Every action name an attribute can fire: literal data-action / data-on-*
    values in markup and templates, plus the name argument of act() / actOn()."""
    refs = set(re.findall(r'data(?:-action|-on-[a-z]+)="(%s)"' % IDENT, m + "\n" + code))
    refs |= set(re.findall(r"""(?<![\w$.])act\(\s*['"](%s)['"]""" % IDENT, code))
    refs |= set(re.findall(r"""(?<![\w$.])actOn\(\s*['"][a-z]+['"]\s*,\s*['"](%s)['"]""" % IDENT, code))
    return refs


def _registry(s):
    block = re.search(r"const actions = \{(.*?)\n\};", s, re.S)
    assert block, "app.js must carry the const actions = {...} block"
    lines = [l.strip() for l in block.group(1).splitlines() if l.strip()]
    bad = [l for l in lines if not re.fullmatch(r"%s,?" % IDENT, l)]
    assert not bad, f"registry entries must be bare names, one per line: {bad}"
    return {l.rstrip(",") for l in lines}


def test_referenced_actions_match_registry():
    """Both directions: a name an attribute fires but the registry lacks is a
    click that logs "Unknown action"; a registry key nothing fires is dead."""
    m, s = _regions()
    referenced, registry = _referenced_actions(m, _code()), _registry(s)
    assert not referenced - registry, f"unregistered action referenced: {sorted(referenced - registry)}"
    assert not registry - referenced, f"dead registry entry: {sorted(registry - referenced)}"
    print(f"  {len(registry)} registered actions, all referenced")


def test_action_names_are_literals():
    """act()/actOn() take the name as a string literal so the test above can
    see it; the only non-literal name positions are the two definitions."""
    code = _code()
    for mm in re.finditer(r"(?<!function )(?<![\w$.])act\(\s*([^,)]+)", code):
        assert re.fullmatch(r"""['"]%s['"]""" % IDENT, mm.group(1).strip()), f"act() name is not a literal: {mm.group(0)!r}"
    for mm in re.finditer(r"(?<!function )(?<![\w$.])actOn\(\s*[^,]+,\s*([^,)]+)", code):
        assert re.fullmatch(r"""['"]%s['"]""" % IDENT, mm.group(1).strip()), f"actOn() name is not a literal: {mm.group(0)!r}"


def test_no_variable_names_in_quotes():
    """The 3b review found actOn('change', 'onBrowserSort', 'svc', 'tabId', EL)
    and act('goToView', 't.view'): variables in quotes, which the dispatcher
    passes on as the strings "svc" and "t.view". A quoted argument that is
    also interpolated as ${name} in the same module, or that looks like a
    member expression, is that mistake."""
    hits = []
    for path in modules():
        code = re.sub(r"(?m)^\s*//.*$", "", path.read_text(encoding="utf-8"))
        interpolated = set(re.findall(r"\$\{(%s)[.\[}]" % IDENT, code))
        for call in re.finditer(r"(?<![\w$.])(act(?:On)?)\(([^)]*)\)", code):
            skip = 2 if call.group(1) == "actOn" else 1          # the event and/or the name
            for arg in re.findall(r"""['"]([^'"]*)['"]""", call.group(2))[skip:]:
                if re.fullmatch(r"%s\.[\w$.]+" % IDENT, arg) or (re.fullmatch(IDENT, arg) and arg in interpolated):
                    hits.append(f"{path.name}: {call.group(0)}")
    assert not hits, "a variable name in quotes is passed on as a string:\n  " + "\n  ".join(hits)


def test_data_attributes_in_markup():
    m, s = _regions()
    registry = _registry(s)

    # Check data-action and data-on-*
    attributes = re.findall(r'data(?:-action|-on-[a-z]+)="([^"]+)"', m)
    for attr in attributes:
        assert attr in registry, f"markup references {attr} which is not in the actions registry"

    # Check data-args validity
    args = re.findall(r"data-args(?:-[a-z]+)?='([^']*)'", m) + re.findall(r'data-args(?:-[a-z]+)?="([^"]*)"', m)
    for arg_str in args:
        try:
            parsed = json.loads(arg_str.replace('&quot;', '"').replace('&amp;', '&'))
            assert isinstance(parsed, list), f"data-args must be a JSON array, got {type(parsed)}"
        except Exception as e:
            assert False, f"invalid JSON in data-args: {arg_str} -> {e}"

    # One element, several handlers: a data-on-<event> handler reads
    # data-args-<event>, falling back to data-args. Hand-written markup that
    # puts data-action and data-on-* on one tag must therefore give the event
    # handler its own arguments (or none) — otherwise it fires with the click's.
    for tag in re.findall(r"<[a-z][^>]*>", m, re.S):
        events = re.findall(r"data-on-([a-z]+)=", tag)
        if "data-action=" in tag and events and "data-args=" in tag:
            for evt in events:
                assert f"data-args-{evt}=" in tag, f"data-on-{evt} would fire with the click's data-args: {' '.join(tag.split())[:160]}"


def test_no_handler_strings_left():
    """menuHtml and emptyHtml ignore the old `call` / `ctaCall` keys; an item
    that still carries one renders a button that does nothing (the 3b review
    found Fix match, Summary and Both that way)."""
    code = _code()
    left = re.findall(r"\b(?:call|ctaCall)\s*:\s*['\"`][^\n]{0,60}", code)
    assert not left, f"handler strings nothing reads any more: {left}"


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
