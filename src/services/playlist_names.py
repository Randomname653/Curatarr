"""The names of Curatarr's per-member playlists, in the member's language.

A playlist lives in one member's account, so unlike a collection it can
speak that member's language (services/user_language: the language they
write to the curator in). A playlist made under another language's name is
found by any of its names and renamed in place, so pins and ids survive a
language change. The Wrap-Up keeps its name: it is the feature's name in
the app too.
"""
from __future__ import annotations

NAMES = {
    "en": {"movie": "Curatarr Recommended · Movies", "show": "Curatarr Recommended · Shows",
           "anime": "Curatarr Recommended · Anime", "music": "Curatarr Recommended · Music",
           "watchlist": "Curatarr Watchlist", "because": "Curatarr · Because You Watched"},
    "de": {"movie": "Curatarr empfiehlt · Filme", "show": "Curatarr empfiehlt · Serien",
           "anime": "Curatarr empfiehlt · Anime", "music": "Curatarr empfiehlt · Musik",
           "watchlist": "Curatarr Merkliste", "because": "Curatarr · Weil du geschaut hast"},
}


def name(slot: str, lang: str = "en") -> str:
    """The playlist's name for a member speaking ``lang`` (English unless
    the language has its own names)."""
    return NAMES.get(lang, NAMES["en"]).get(slot) or NAMES["en"][slot]


def names(slot: str) -> set:
    """Every name the playlist may carry today, any language."""
    return {n[slot] for n in NAMES.values() if slot in n}
