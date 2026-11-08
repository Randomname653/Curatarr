"""Rediscover: titles a member loved and has not touched for two years.

The library replaces streaming, and a library is also what was once loved
and then forgotten. This lane takes, per member and category, the owned
titles they finished (a film) or watched in earnest (a series: six
finished episodes or more), with no play at all in the last two years,
and ranks them by today's taste fit (the library lane's ranking,
recommendations_engine.score_arr_items). The line on each card is the
fact, in the member's language: when they last watched it and how often.
No model writes anything here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

AWAY = timedelta(days=730)
SERIES_MIN_EPISODES = 6
PICKS = 8


def loved_long_ago(user_id: int, category: str, now: Optional[datetime] = None) -> dict:
    """{normalised title: {"title", "last", "times"}} of what this member
    finished or watched in earnest and last played over two years ago.
    ``times``: finished plays of a film, finished episodes of a series.
    Blocking."""
    from sqlalchemy import case, func
    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry as W
    from src.services.library_memory import normalize_title
    now = now or datetime.utcnow()
    series = category in ("show", "anime")
    key = W.series_title if series else W.title
    with get_db_session() as db:
        rows = (db.query(key, func.max(W.viewed_at),
                         func.sum(case((W.completed.is_(True), 1), else_=0)),
                         func.count(func.distinct(func.coalesce(W.season, 0) * 10000 + func.coalesce(W.episode, 0))))
                .filter(W.user_id == user_id, W.media_type == category, key.isnot(None))
                .group_by(key).all())
    out = {}
    for title, last, finished, episodes in rows:
        if not title or not last or now - last < AWAY:
            continue
        times = int(finished or 0)
        if series and min(times, int(episodes or 0)) < SERIES_MIN_EPISODES:
            continue
        if not series and times < 1:
            continue
        out[normalize_title(title)] = {"title": title, "last": last, "times": times}
    return out


_STOP = {"the", "a", "an", "and", "of", "to", "in", "on", "der", "die", "das", "und", "von", "part", "chapter"}


def _family(title: str) -> set:
    """The words that tie a series of films together ("fast", "furious")."""
    import re
    return {w for w in re.findall(r"[a-z0-9]+", (title or "").lower())
            if w not in _STOP and not w.isdigit() and len(w) > 1}


def one_per_family(items: list, n: int) -> list:
    """The first ``n`` items, a film series once: an item sharing two title
    words with one already kept is skipped (live: four films of one series
    in a row)."""
    kept, fams = [], []
    for it in items:
        f = _family(it.get("title") or "")
        if any(len(f & g) >= 2 or (f and f == g) for g in fams):
            continue
        kept.append(it)
        fams.append(f)
        if len(kept) == n:
            break
    return kept


def fact_line(last: datetime, times: int, category: str, lang: str, now: datetime) -> str:
    """'Last watched 3 years ago · seen twice' in the member's language."""
    years = max(2, round((now - last).days / 365))       # 2.9 years read as three
    de = lang == "de"
    if category in ("show", "anime"):
        what = f"{times} Folgen gesehen" if de else f"{times} episodes watched"
    elif times > 1:
        what = f"{times}× gesehen" if de else f"seen {times} times"
    else:
        what = "einmal gesehen" if de else "seen once"
    when = f"Zuletzt vor {years} Jahren" if de else f"Last watched {years} years ago"
    return f"{when} · {what}"


async def rediscover_recs(user_id: int, category: str, owned: list,
                          now: Optional[datetime] = None) -> list:
    """The lane's recs for one member and category, ranked by today's
    taste fit; [] when nothing qualifies."""
    from src.database.runner import run_read
    from src.services.library_memory import normalize_title
    from src.services.recommendations_engine import score_arr_items
    from src.services.user_language import user_language
    now = now or datetime.utcnow()
    loved = await run_read(loved_long_ago, user_id, category, now)
    if not loved or not owned:
        return []
    pool = [i for i in owned if normalize_title(i.get("title") or "") in loved]
    if not pool:
        return []
    ranked = one_per_family(await score_arr_items(user_id, category, pool, top_n=PICKS * 4), PICKS)
    lang = await run_read(user_language, user_id)
    out = []
    for item in ranked:
        hit = loved[normalize_title(item.get("title") or "")]
        out.append({
            "title": item["title"], "genres": item.get("genres") or "", "year": item.get("year"),
            "category": category, "lane": "rediscover",
            "reason": fact_line(hit["last"], hit["times"], category, lang, now),
            "confidence": round(1.0 - (len(out) / max(1, len(ranked))) * 0.4, 3),
            "score_factors": {"kind": "recommendation", "lane": "rediscover", "factors": [
                {"key": "rediscover", "label": fact_line(hit["last"], hit["times"], category, "en", now)},
                {"key": "taste", "label": "Ranked by how well it fits your taste today"},
                {"key": "lane", "label": "In your library"},
            ]},
        })
    logger.info("[recs] rediscover/%s: %d of %d loved-long-ago titles in the library", category, len(out), len(loved))
    return out
