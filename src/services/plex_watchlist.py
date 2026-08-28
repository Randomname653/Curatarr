"""Per-user Plex-native watchlist writes (plex.tv Discover API).

A declared intention to watch — "sounds promising, I shall put it on my
watchlist" — ends a deletion discussion in favour of keeping the title
(chat doctrine: EXPRESSED INTEREST — DECLARATION vs QUESTION). This module
makes the watchlist part TRUE instead of prose: the protection-intent hook
calls ``add_to_watchlist`` for the DISCUSSING user, whose own plex.tv
account token (stored at Plex-OAuth login) receives the entry — so it shows
up on their phone/TV Plex clients, per user, not on the server owner's.

The write path is deliberately deterministic backend code: the curator LLM
never executes it (``no_library_actions`` stands); the post-turn scanner
detects the declaration and this module acts on it.

Discover, not the PMS: the watchlist lives on plex.tv, keyed by Discover
metadata ids — so the title is first resolved via Discover search and only
an unambiguous match is written. No match → honest failure, no guessing
(the Momoiro lesson applies to watchlists too).
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_DISCOVER = "https://discover.provider.plex.tv"

# Curatarr category → Discover metadata type. Anime series live as "show".
_TYPE_FOR = {"movie": "movie", "show": "show", "anime": "show"}


def _norm_title(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def pick_discover_match(results: list[dict], title: str,
                        year: Optional[int] = None,
                        media_type: Optional[str] = None) -> Optional[dict]:
    """The one unambiguous Discover hit for ``title`` — or ``None``.

    Word-multiset title equality (the Kishibe-Rohan lesson: romanised
    Japanese titles swap name order between sources), then the expected
    Discover type, then the year as tiebreaker. Two surviving candidates
    with different years and no year to decide → None, never a guess.
    """
    want_type = _TYPE_FOR.get(media_type or "")
    nq = sorted(_norm_title(title).split())
    cands = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        if sorted(_norm_title(r.get("title") or "").split()) != nq:
            continue
        if want_type and r.get("type") and r.get("type") != want_type:
            continue
        cands.append(r)
    if not cands:
        return None
    if year:
        dated = [c for c in cands if c.get("year") == year]
        if len(dated) == 1:
            return dated[0]
        if dated:
            cands = dated
    if len(cands) == 1:
        return cands[0]
    years = {c.get("year") for c in cands}
    if len(years) > 1:
        return None          # same name, different works — refuse to guess
    return cands[0]          # duplicates of one work (editions) — any is fine


async def add_to_watchlist(user_id: int, title: str,
                           year: Optional[int] = None,
                           media_type: Optional[str] = None) -> dict:
    """Add ``title`` to THIS user's plex.tv watchlist. Never raises.

    Returns ``{"ok": True, "matched": <discover title/year>}`` or
    ``{"ok": False, "error": ...}`` — callers log, they don't retry.
    """
    try:
        from src.database.connection import get_db_session
        from src.database.models import User
        with get_db_session() as db:
            user = db.query(User).filter(User.id == user_id).first()
            token = (user.plex_token or "").strip() if user else ""
        if not token:
            return {"ok": False, "error": "user has no plex.tv token on file"}

        headers = {"X-Plex-Token": token, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{_DISCOVER}/library/search", headers=headers,
                                 params={"query": title, "limit": 10,
                                         "searchTypes": "movies,tv"})
            r.raise_for_status()
            container = (r.json() or {}).get("MediaContainer", {})
            results = []
            for group in container.get("SearchResults", []) or []:
                for hit in group.get("SearchResult", []) or []:
                    md = hit.get("Metadata")
                    if md:
                        results.append(md)
            if not results:
                results = container.get("Metadata", []) or []

            match = pick_discover_match(results, title, year, media_type)
            if not match or not match.get("ratingKey"):
                return {"ok": False,
                        "error": f"no unambiguous Discover match for {title!r}"}

            rk = str(match["ratingKey"]).rsplit("/", 1)[-1]
            w = await client.put(f"{_DISCOVER}/actions/addToWatchlist",
                                 headers=headers, params={"ratingKey": rk})
            w.raise_for_status()
            matched = f"{match.get('title')} ({match.get('year')})"
            logger.info("[watchlist] user %d: %s added to plex.tv watchlist",
                        user_id, matched)
            return {"ok": True, "matched": matched}
    except httpx.HTTPStatusError as e:
        return {"ok": False, "error": f"plex.tv returned {e.response.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
