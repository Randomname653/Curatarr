"""Curatarr — the language a user's Curatarr speaks.

Two sources, the setting first (UX design record, stage 3.8):

* ``users.ui_language`` - "Language: Automatic / English / Deutsch" under
  Settings → Account, for every user, the owner too;
* "Automatic" (NULL): the language the user writes to the curator in most
  (``wrapup_writer.main_language``, stopwords, no model), English without
  messages.

It decides the UI (``frontend/i18n/<lang>.json``), every text the server
writes (cards, chips, Tonight, the Wrap-Up, playlist names) and how numbers
and dates read (``ui.loc``). It never decides the language of a chat
answer: that follows each message (``chat._detect_user_language``).
"""
import time
from typing import Optional

from src.database.connection import get_db_session

_TTL_S = 3600
_cache: dict = {}            # user id -> (language, when)
# The languages the UI is written in; "auto" (stored as NULL) detects.
UI_LANGUAGES = ("en", "de")
SETTINGS = ("auto",) + UI_LANGUAGES
# BCP 47 tags for Intl: the language -> a locale.
LOCALES = {"en": "en-US", "de": "de-DE", "fr": "fr-FR", "es": "es-ES", "it": "it-IT", "nl": "nl-NL"}


def stored_setting(user_id: int) -> str:
    """"auto", "en" or "de" - what the user picked. Blocking."""
    from src.database.models import User
    with get_db_session() as db:
        row = db.query(User.ui_language).filter(User.id == user_id).first()
    value = row[0] if row else None
    return value if value in UI_LANGUAGES else "auto"


def set_setting(user_id: int, value: str) -> str:
    """Store the user's pick ("auto" clears it); the next read uses it.
    ValueError for anything else. Blocking."""
    if value not in SETTINGS:
        raise ValueError(value)
    from src.database.models import User
    with get_db_session() as db:
        row = db.get(User, user_id)
        if row is None:
            raise ValueError("no such user")
        row.ui_language = None if value == "auto" else value
        db.commit()
    _cache.pop(user_id, None)
    return value


def detected_language(user_id: int) -> str:
    """The language the user writes to the curator in ("Automatic"). Blocking."""
    from src.services.wrapup_writer import main_language
    try:
        with get_db_session() as db:
            return main_language(db, user_id)
    except Exception:  # noqa: BLE001 - English is the safe default
        return "en"


def user_language(user_id: int) -> str:
    """'en', 'de', … for this user: the setting, else the detected
    language; read at most once an hour (a change clears it). Blocking."""
    hit = _cache.get(user_id)
    if hit and time.monotonic() - hit[1] < _TTL_S:
        return hit[0]
    setting: Optional[str]
    try:
        setting = stored_setting(user_id)
    except Exception:  # noqa: BLE001 - an unreadable setting is "Automatic"
        setting = "auto"
    lang = setting if setting != "auto" else detected_language(user_id)
    _cache[user_id] = (lang, time.monotonic())
    return lang


def ui_language(user_id: int) -> str:
    """The language the UI is drawn in: ``user_language`` where the UI is
    written in it, else English. Blocking."""
    lang = user_language(user_id)
    return lang if lang in UI_LANGUAGES else "en"


def user_locale(user_id: int) -> str:
    """The Intl locale for this user's numbers and dates. Blocking."""
    return LOCALES.get(user_language(user_id), "en-US")
