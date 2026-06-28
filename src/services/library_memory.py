"""Recommendation memory: what has the user *seen* and what do they *own*.

The shared truth layer behind the two recommendation lanes. Before this,
the discovery path leaked already-watched / already-owned titles (the
"Ex Machina" bug): its only guard was a soft prompt line truncated to the
first 15 watched titles (`list(watched)[:15]`) — useless against a 32-movie
or 18 000-track history, and ignorable by the model anyway. There was no
owned-awareness at all.

This module replaces that with two **complete** indices and a **hard,
deterministic** classifier:

* ``seen_index``  — everything the user has played (full watch_history, not
  capped): a set of normalised titles PLUS the set of TMDB ids. Same source
  as :mod:`src.services.watch_status` (the Plex-synced ``watch_history``),
  but shaped for fast bulk membership instead of per-title status dicts.
* ``owned_index`` — everything in the ARR libraries (Radarr/Sonarr/Lidarr):
  normalised titles PLUS tmdb_id PLUS tvdb_id.
* ``classify`` / ``partition`` — label each candidate ``seen`` / ``owned`` /
  ``new`` by matching on a stable id first (tmdb_id/tvdb_id — survives any
  title spelling) and a normalised title second (catches LLM-suggested
  titles, which carry no id).

The library lane keeps only ``new`` relative to ``seen`` (owned-but-unwatched);
the discovery lane keeps only ``new`` relative to ``seen`` ∪ ``owned``
(genuinely new to acquire). Either way a watched title can no longer surface.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("curatarr")

# Trailing disambiguator year in PARENTHESES only — "Ex Machina (2014)" →
# "Ex Machina". Deliberately NOT a bare trailing year: "Blade Runner 2049"
# and "Akira" → "Akira 2019"-style real titles must survive intact, and a
# bare-year strip would collapse "Blade Runner 2049" onto "Blade Runner".
_PAREN_YEAR_RE = re.compile(r"\s*\((?:19|20)\d{2}\)\s*$")
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_title(title) -> str:
    """Canonicalise a title for cross-source matching.

    lowercase · strip a trailing "(YYYY)" · punctuation → space · collapse
    whitespace. So "Ex Machina", "ex machina", "Ex Machina (2014)" and
    "Ex-Machina" all collapse to ``"ex machina"``.

    Intentionally conservative: no leading-article stripping (would risk
    merging distinct titles like "The Thing" vs "Thing"), no bare-year
    stripping (see ``_PAREN_YEAR_RE``). The goal is to absorb formatting
    drift between Plex/ARR/LLM, not to fuzzy-match different works together.
    """
    if not title:
        return ""
    s = str(title).strip().lower()
    s = _PAREN_YEAR_RE.sub("", s)
    s = _NON_WORD_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _as_int(val):
    """Coerce an id to a positive int, or None. ARR/TMDB ids arrive as int,
    str, "", 0, or None depending on the source."""
    if val in (None, "", 0, "0"):
        return None
    try:
        n = int(val)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def seen_index(user_id: int, category: str | None = None) -> dict:
    """Full watched/listened memory for the user as fast membership sets.

    Returns ``{"titles": set[str], "tmdb_ids": set[int]}`` — every play in
    ``watch_history`` (optionally scoped to one ``media_type``), NOT capped.
    Both the item title and the series/artist title are folded into
    ``titles`` (normalised), so a music artist (stored as ``series_title``)
    is matched as well as a movie title.

    ``media_type`` values in the table are exactly the recommendation
    categories — ``movie`` / ``show`` / ``anime`` / ``music`` — so the
    optional ``category`` filter lines up 1:1.
    """
    titles: set[str] = set()
    tmdb_ids: set[int] = set()
    if not user_id:
        return {"titles": titles, "tmdb_ids": tmdb_ids}

    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry
    try:
        with get_db_session() as db:
            q = db.query(
                WatchHistoryEntry.title,
                WatchHistoryEntry.series_title,
                WatchHistoryEntry.tmdb_id,
            ).filter(WatchHistoryEntry.user_id == user_id)
            if category:
                q = q.filter(WatchHistoryEntry.media_type == category)
            for title, series_title, tmdb_id in q.all():
                for t in (title, series_title):
                    n = normalize_title(t)
                    if n:
                        titles.add(n)
                tid = _as_int(tmdb_id)
                if tid:
                    tmdb_ids.add(tid)
    except Exception as e:
        logger.debug("[memory] seen_index failed: %s", e)

    return {"titles": titles, "tmdb_ids": tmdb_ids}


async def owned_index(category: str | None = None) -> dict:
    """Everything the user already owns, from the live ARR libraries.

    Returns ``{"titles": set[str], "tmdb_ids": set[int], "tvdb_ids": set[int]}``.
    Reuses the router's cached ``_fetch_arr_candidates`` (5-min TTL, stale-on-
    error) so this adds no extra ARR load on a warm cache. Imported lazily to
    avoid the ``engine → library_memory → router → engine`` import cycle.
    """
    titles: set[str] = set()
    tmdb_ids: set[int] = set()
    tvdb_ids: set[int] = set()
    try:
        from src.routers.recommendations import _fetch_arr_candidates
        items = await _fetch_arr_candidates(category)
    except Exception as e:
        logger.debug("[memory] owned_index fetch failed: %s", e)
        return {"titles": titles, "tmdb_ids": tmdb_ids, "tvdb_ids": tvdb_ids}

    for it in items or []:
        n = normalize_title(it.get("title"))
        if n:
            titles.add(n)
        tid = _as_int(it.get("tmdb_id"))
        if tid:
            tmdb_ids.add(tid)
        vid = _as_int(it.get("tvdb_id"))
        if vid:
            tvdb_ids.add(vid)
    return {"titles": titles, "tmdb_ids": tmdb_ids, "tvdb_ids": tvdb_ids}


def _matches(item, index: dict) -> bool:
    """True if ``item`` (a rec/ARR dict) is present in ``index``.

    Id match first (tmdb_id, then tvdb_id) — survives any title spelling;
    normalised-title match second — the only signal available for an
    LLM-suggested title (which carries no id).
    """
    if not index:
        return False
    tid = _as_int(item.get("tmdb_id"))
    if tid and tid in index.get("tmdb_ids", ()):
        return True
    vid = _as_int(item.get("tvdb_id"))
    if vid and vid in index.get("tvdb_ids", ()):
        return True
    n = normalize_title(item.get("title"))
    if n and n in index.get("titles", ()):
        return True
    return False


def is_seen(item, seen: dict) -> bool:
    """Has the user already watched/listened to this item? Used by the
    library lane to drop watched items from the owned candidate pool."""
    return _matches(item, seen)


def classify(item, seen: dict, owned: dict) -> str:
    """Label one candidate: ``"seen"`` › ``"owned"`` › ``"new"`` (in that
    precedence — seen wins over owned because a watched title is the strongest
    "don't surface this" signal even though the user also owns it)."""
    if _matches(item, seen):
        return "seen"
    if _matches(item, owned):
        return "owned"
    return "new"


def partition(recs: list, seen: dict, owned: dict | None = None) -> dict:
    """Split a list of recs into ``{"seen": [...], "owned": [...], "new": [...]}``.

    Each rec is annotated in place with ``rec["provenance"]`` = its bucket, so
    the surviving recs carry their own label for the UI ("woher kommt das").
    ``owned`` defaults to empty → everything is just ``seen`` vs ``new``
    (the library lane only cares about the watched filter).
    """
    owned = owned or {}
    out = {"seen": [], "owned": [], "new": []}
    for r in recs or []:
        bucket = classify(r, seen, owned)
        r["provenance"] = bucket
        out[bucket].append(r)
    return out
