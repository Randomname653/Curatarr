"""Curatarr — the language a user speaks to Curatarr in.

The UI's numbers and dates followed the browser's locale, the curator's text
the user's own language: a German browser read "1.055GB" next to the
roast's "3,152 GB". Both follow one language now: the one the user writes to
the curator in most (``wrapup_writer.main_language``, stopwords, no model),
English without messages. The front end formats with it (``ui.loc``).
"""
import time

from src.database.connection import get_db_session

_TTL_S = 3600
_cache: dict = {}            # user id -> (language, when)
# BCP 47 tags for Intl: the language the curator detects -> a locale.
LOCALES = {"en": "en-US", "de": "de-DE", "fr": "fr-FR", "es": "es-ES", "it": "it-IT", "nl": "nl-NL"}


def user_language(user_id: int) -> str:
    """'en', 'de', … for this user; read at most once an hour. Blocking."""
    hit = _cache.get(user_id)
    if hit and time.monotonic() - hit[1] < _TTL_S:
        return hit[0]
    from src.services.wrapup_writer import main_language
    try:
        with get_db_session() as db:
            lang = main_language(db, user_id)
    except Exception:  # noqa: BLE001 - English is the safe default for formatting
        lang = "en"
    _cache[user_id] = (lang, time.monotonic())
    return lang


def user_locale(user_id: int) -> str:
    """The Intl locale for this user's numbers and dates. Blocking."""
    return LOCALES.get(user_language(user_id), "en-US")
