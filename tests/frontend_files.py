"""The frontend's files, for every test that reads them.

2026-09-11: index.html was split into markup (index.html), styles
(css/app.css) and the app module (js/app.js). 2026-09-13: app.js became an
entry module plus one module per view (js/*.js). Tests that look for a
label, a needle or a ceiling read through here, so the next change touches
one place. Always UTF-8: the files carry em dashes and arrows, and a bare
read_text() on Windows decodes cp1252.
"""
from pathlib import Path

_FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def markup() -> str:
    """frontend/index.html — the markup, inline handlers included."""
    return (_FRONTEND / "index.html").read_text(encoding="utf-8")


def styles() -> str:
    """frontend/css/app.css — the stylesheet, DESIGN LANGUAGE header first."""
    return (_FRONTEND / "css" / "app.css").read_text(encoding="utf-8")


def modules() -> list[Path]:
    """app.js first (it carries the window export block), then every other
    module in name order — a deterministic concatenation for needle tests."""
    js = _FRONTEND / "js"
    return [js / "app.js"] + sorted(p for p in js.glob("*.js") if p.name != "app.js")


def script() -> str:
    """Every module, concatenated in modules() order."""
    return "\n".join(p.read_text(encoding="utf-8") for p in modules())


def everything() -> str:
    """Markup + script, for needle tests that do not care where a string lives."""
    return markup() + "\n" + script()
