"""Curatarr — the UI's words, read by the server too (UX design record 3.8).

The dictionaries are ``frontend/i18n/en.json`` and ``de.json``: flat keys
("nav.deletions") to text, a plural as {"one": ..., "other": ...}, ``{name}``
for a value. The front end draws the UI from them (``js/i18n.js``); the
server reads the same files where it names the UI - the curator's app map
(services/app_context) says the button the user sees, in the user's
language, and the two can never drift apart. Pure reads, cached per process.
"""
import json
import re
from functools import lru_cache
from typing import Optional

LANGUAGES = ("en", "de")
_VAR = re.compile(r"\{(\w+)\}")


@lru_cache(maxsize=None)
def dictionary(lang: str) -> dict:
    """The dictionary for ``lang`` ({} for a language without one)."""
    from src.paths import frontend_root
    if lang not in LANGUAGES:
        return {}
    path = frontend_root() / "i18n" / f"{lang}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _plural(value, n) -> str:
    if isinstance(value, dict):
        return value.get("one" if n == 1 else "other") or value.get("other") or ""
    return value


def t(key: str, lang: str = "en", **values) -> str:
    """The text of ``key`` in ``lang``, English where that language lacks it,
    the key itself where both do. ``n`` picks a plural form; ``{name}`` slots
    take ``values``."""
    raw: Optional[object] = dictionary(lang).get(key)
    if raw is None:
        raw = dictionary("en").get(key, key)
    text = _plural(raw, values.get("n"))
    return _VAR.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)
