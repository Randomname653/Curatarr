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


def test_every_inline_handler_resolves_to_an_exported_global():
    """The modules have their own scope: nothing is global unless app.js hands
    it to window in the Object.assign(window, {...}) block. Handler code — the
    on*="..." attributes in markup and templates, the strings menuHtml,
    pagerHtml, emptyHtml and _errHtml turn into handlers — runs in the global
    scope, so EVERY identifier in it must be an exported function or a browser
    global. Not just the leading call: the first split missed the
    ontoggle="if (this.open) loadX()" sections and a "...; _syncDelPosterVisual(this)"
    second statement, and the second split wrote `state.` into a handler
    string. All three classes are caught here."""
    m, s = _regions()
    code = re.sub(r"(?m)^\s*//.*$", "", s)   # a comment may quote an old handler
    both = m + "\n" + code
    top_level = set(re.findall(r"^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\(", code, re.M))
    handlers = re.findall(r'\bon[a-z]+="([^"]*)"', both)
    handlers += [x[2] for x in re.findall(r"""(call|ctaCall)\s*[:=]\s*(['"`])(.*?)\2""", both)]
    handlers += re.findall(r"""_errHtml\([^,()]+,\s*['"]([^'"]*)['"]""", code)
    allowed = {"this", "event", "if", "else", "true", "false", "null", "undefined", "document",
               "window", "location", "return", "new", "typeof", "void", "offset"}
    def _strip_templates(text):
        """Replace every ${...} — nesting included, as in ${a ? `'${b}'` : 'x'} —
        with a literal 0: the expression runs at render time, not on click."""
        out, i, depth = [], 0, 0
        while i < len(text):
            if depth == 0 and text.startswith("${", i):
                depth, i = 1, i + 2
                out.append("0")
                continue
            if depth:
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                i += 1
                continue
            out.append(text[i])
            i += 1
        return "".join(out)

    needed, foreign = set(), set()
    for h in handlers:
        h = _strip_templates(h)
        h = re.sub(r"'[^']*'|&quot;[^&]*&quot;", "''", h)          # string literals are data
        h = re.sub(r"(?<=[{,])\s*[A-Za-z_$][\w$]*\s*:", ",", h)   # object-literal keys are not identifiers
        for ident in re.findall(r"(?<![\w$.])([A-Za-z_$][\w$]*)(?![\w$])", h):
            if ident in allowed:
                continue
            (needed if ident in top_level else foreign).add(ident)
    # calls spelled inside other strings reach a handler too (pagerHtml pages, CTAs)
    needed |= set(re.findall(r"""['"`]\s*([A-Za-z_$][\w$]*)\(""", code)) & top_level
    assert not foreign, f"handler code names something that is neither exported nor a browser global: {sorted(foreign)}"
    block = re.search(r"Object\.assign\(window,\s*\{(.*?)\}\s*\);", s, re.S)
    assert block, "app.js must carry the Object.assign(window, {...}) block"
    exposed = {n.strip() for n in block.group(1).split(",") if n.strip()}
    missing = sorted(needed - exposed)
    assert not missing, f"inline handlers reference functions app.js does not expose: {missing}"
    unused = sorted(exposed - needed)
    assert not unused, f"exposed but no handler names them (export only what is referenced): {unused}"
    print(f"  {len(exposed)} functions exposed for {len(needed)} handler references")





def test_no_inline_handlers_in_markup():
    m, _ = _regions()
    handlers = re.findall(r'\bon[a-z]+="[^"]*"', m)
    assert not handlers, f"markup still contains inline handlers: {handlers}"

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
