"""Curatarr — one-tap actions under a chat reply (UX audit 2026-11-08, C4/C5).

A chat reply was text: the curator could recommend a title and the member
could not open it, put it on their watchlist or keep it from deletion
without leaving the chat, and "please never delete this" worked only through
a background detector whose outcome arrived later in the bell.

After a reply is complete, its **bold** titles (the chat prompt's own
formatting rule: "Use **bold** for media titles") are matched against the
member's library - exact names from the arr caches, the same ground truth
the chat's title detection uses - and each match gets a small action row:
Open on the server, + Watchlist, Keep. Deterministic, no model, nothing
about the reply's wording: a title the reply did not set in bold, or one the
library does not hold, gets no row.
"""
import re
from typing import Callable

MAX_TITLES = 4
_BOLD = re.compile(r"\*\*([^*\n]{2,160})\*\*")


def _norm(s: str) -> str:
    s = re.sub(r"[‐‑‒–—−]", "-", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def bold_titles(reply: str) -> list[str]:
    """The reply's bold spans, in order, once each. Pure."""
    out: list[str] = []
    for m in _BOLD.finditer(reply or ""):
        t = m.group(1).strip().strip("\"'“”„«»").strip()
        if t and t.lower() not in (x.lower() for x in out):
            out.append(t)
    return out


def match(spans: list[str], index: list) -> list[tuple]:
    """[(library title, category)] for the spans that ARE a library title
    (normalized equality, not containment: "**Signal**" is not "North
    Signal"). ``index``: [(normalized, title, category)]. A name the library
    holds as a film AND a series gives both - the reply's bold does not say
    which, and picking the first index entry wrote the watchlist add and the
    keep under the wrong category. Pure."""
    by_norm: dict = {}
    for n, title, cat in index:
        hits = by_norm.setdefault(n, [])
        if (title, cat) not in hits:
            hits.append((title, cat))
    out = []
    for span in spans:
        for hit in by_norm.get(_norm(span), []):
            if hit not in out and len(out) < MAX_TITLES:
                out.append(hit)
    return out


def for_reply(user_id: int, reply: str, library_index: Callable[[], list]) -> list[dict]:
    """[{title, category, key, protected}] for the reply's bold library
    titles: ``key`` the server key the library ledger holds for it (None
    when it has none - music, or not walked yet), ``protected`` whether this
    member already keeps it. Blocking."""
    spans = bold_titles(reply)
    if not spans:
        return []
    hits = match(spans, library_index())
    if not hits:
        return []
    from sqlalchemy import func
    from src.database.connection import get_db_session
    from src.database.models import LibraryItem, ProtectedMedia
    out = []
    with get_db_session() as db:
        kept = {i for (i,) in db.query(ProtectedMedia.identifier).filter(
            ProtectedMedia.user_id == user_id,
            ProtectedMedia.identifier.in_([t for t, _ in hits]))}
        for title, cat in hits:
            key = None
            if cat in ("movie", "show", "anime"):
                row = (db.query(LibraryItem.plex_rating_key)
                       .filter(func.lower(LibraryItem.title) == title.lower(),
                               LibraryItem.media_type == cat, LibraryItem.gone_at.is_(None))
                       .first())
                key = row[0] if row else None
            out.append({"title": title, "category": cat, "key": key, "protected": title in kept})
    return out
