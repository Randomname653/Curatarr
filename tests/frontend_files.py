import pathlib
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


_ROOT = pathlib.Path(__file__).resolve().parents[1]

def script() -> str:
    js_dir = _ROOT / "frontend" / "js"
    app_js_path = js_dir / "app.js"
    with open(app_js_path, 'r', encoding='utf-8') as f:
        content = [f.read()]

    other_files = sorted([f for f in js_dir.glob("*.js") if f.name != "app.js"])
    for file in other_files:
        with open(file, 'r', encoding='utf-8') as f:
            content.append(f.read())

    return "\n".join(content)

def everything() -> str:
    return markup() + "\n" + script()
