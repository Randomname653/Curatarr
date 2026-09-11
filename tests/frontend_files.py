"""The frontend's three files, for every test that reads them.

2026-09-11: index.html was split into markup (index.html), styles
(css/app.css) and the app module (js/app.js). Tests that look for a label,
a needle or a ceiling read through here, so the next split touches one
place. Always UTF-8: the files carry em dashes and arrows, and a bare
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


def script() -> str:
    """frontend/js/app.js — the app module (one file until PR 2 splits it)."""
    return (_FRONTEND / "js" / "app.js").read_text(encoding="utf-8")


def everything() -> str:
    """Markup + script, for needle tests that do not care where a string lives."""
    return markup() + "\n" + script()
