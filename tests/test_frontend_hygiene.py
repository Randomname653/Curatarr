"""The UI grammar holds — and the old mechanisms only ever go down.

2026-09-06: the frontend had four ways to confirm or report (native
alert()/confirm()/prompt(), hand-built style.cssText overlays, button-text
mutation) plus a showToast() that was called but never defined, and ~830
inline style="…" attributes standing in for classes. The shared helpers
(toast, openModal, confirmDialog, menuHtml, pagerHtml, btnBusy/btnDone,
setBadge, emptyHtml) replace them view by view. This suite pins the counts
so they can only fall: lower a ceiling when you retire a call, never raise
one.

    python tests/test_frontend_hygiene.py
"""
import pathlib
import re
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INDEX = _ROOT / "frontend" / "index.html"

# Ceilings, not targets. Step 1 of the UI-grammar work pinned the numbers as
# they stood after the foundation landed; every later step lowers them
# (Step 2, the Knowledge Base: 635 -> 468 inline styles in JS, 198 -> 153 static;
# Step 3, Deletions/Curation/Recs/Report: 468 -> 388 / 153 -> 140, alert 16 -> 7).
CEILINGS = {
    "alert(": 7,
    "confirm(": 6,
    "prompt(": 2,
    "style.cssText": 11,
    'style= in JS templates': 388,
    'style= in static markup': 140,
}


def _regions():
    html = _INDEX.read_text(encoding="utf-8")
    start = html.index("<script>\n", html.index("</head>"))
    return html[:start], html[start:]


def _count(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text))


def test_old_mechanisms_only_go_down():
    static, js = _regions()
    seen = {
        "alert(": _count(r"(?<![\w.])alert\(", js),
        "confirm(": _count(r"(?<![\w.])confirm\(", js),       # confirmDialog( does not match
        "prompt(": _count(r"(?<![\w.])prompt\(", js),
        "style.cssText": _count(r"\.style\.cssText", js),
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
    static, js = _regions()
    # Only the shared root may be positioned as a full-screen overlay from JS.
    assert _count(r"position:fixed;inset:0", js) == 0, "an overlay is being hand-built in JS again"
    for cls in (".modal ", ".toast ", ".menu-list ", ".select-bar ", ".empty ", ".pager ", ".toolbar ", ".section-head"):
        assert cls in static, f"grammar class missing from the stylesheet: {cls.strip()}"


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
