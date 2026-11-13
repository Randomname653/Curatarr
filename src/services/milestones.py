"""Milestones: the 100th film, 1,000 hours, the first anime finished.

Fixed rules over the watch history decide what a milestone is; no model
decides or invents one, at most it phrases one. Every milestone carries the
day it was reached and the title that crossed it ("your 100th film: Heat,
3 May 2024"), and is computed back over the whole history, so the first
run already shows everything reached so far.

Where they show: a timeline on the Wrap-Up page, a slide in a Wrap-Up
story for the ones reached in its period, and a chat opener ONCE for one
newly reached (the ones the first run finds are history, never announced).
A member can switch kinds off.

Kept per member in app_state ``milestones:user_id=N`` (the custodian's
daily ``milestones`` job, no model).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

RULES = {
    "films":    (10, 50, 100, 250, 500, 1000),
    "series":   (1, 10, 25, 50, 100),
    "anime":    (1, 10, 25, 50, 100),
    "episodes": (100, 500, 1000, 5000, 10000),
    "hours":    (100, 500, 1000, 2500, 5000, 10000),
    "artists":  (100, 500, 1000, 5000),
    "years":    tuple(range(1, 51)),
}
KINDS = tuple(RULES)
ANNOUNCE_DAYS = 14          # a milestone older than this when first seen is history
_KEY = "milestones:user_id={uid}"
_OFF = "milestones_off:user_id={uid}"


def _crossings(events: list, kind: str) -> list:
    """events: [(when, title, amount)] in time order -> the milestones of
    ``kind`` they cross, each at the event that crossed it."""
    out, total, steps = [], 0.0, list(RULES[kind])
    for when, title, amount in events:
        total += amount
        while steps and total >= steps[0]:
            out.append({"kind": kind, "value": steps.pop(0), "reached_at": when, "title": title})
    return out


def compute(user_id: int, sonarr_index: Optional[dict] = None,
            now: Optional[datetime] = None) -> list:
    """Every milestone this member reached, oldest first. ``sonarr_index``:
    proactive_messages._get_sonarr_index() (series totals; without it no
    series milestones). Blocking."""
    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry as W
    from src.services.library_memory import normalize_title
    now = now or datetime.utcnow()
    with get_db_session() as db:
        rows = (db.query(W.viewed_at, W.media_type, W.title, W.series_title, W.completed,
                         W.duration_ms, W.view_offset_ms, W.season, W.episode)
                .filter(W.user_id == user_id, W.viewed_at.isnot(None))
                .order_by(W.viewed_at).all())
    if not rows:
        return []
    films, episodes, hours, artists = [], [], [], []
    seen_films, seen_artists = set(), set()
    eps_of: dict = {}            # (category, series) -> {(season, episode)}
    finished: list = []          # (when, series, category)
    done_series = set()
    for at, cat, title, series, completed, dur, off, season, ep in rows:
        ms = (dur if completed and dur else off) or 0
        if ms > 0:
            hours.append((at, series if cat in ("show", "anime", "music") and series else title, ms / 3_600_000))
        if cat == "movie" and completed:
            k = normalize_title(title)
            if k and k not in seen_films:
                seen_films.add(k)
                films.append((at, title, 1))
        elif cat in ("show", "anime") and completed:
            episodes.append((at, series or title, 1))
            if series and season and season >= 1 and ep:
                key = (cat, series)
                eps_of.setdefault(key, set()).add((season, ep))
                if key not in done_series and sonarr_index is not None:
                    from src.services.proactive_messages import _resolve_sonarr_stats
                    st = _resolve_sonarr_stats(sonarr_index, None, series, cat)
                    if (st and st.get("status") == "ended" and st.get("total_episodes")
                            and len(eps_of[key]) >= st["total_episodes"]):
                        done_series.add(key)
                        finished.append((at, series, cat))
        elif cat == "music" and series:
            k = series.casefold()
            if k not in seen_artists:
                seen_artists.add(k)
                artists.append((at, series, 1))
    out = (_crossings(films, "films") + _crossings(episodes, "episodes")
           + _crossings(hours, "hours") + _crossings(artists, "artists")
           + _crossings([(a, s, 1) for a, s, c in finished if c == "show"], "series")
           + _crossings([(a, s, 1) for a, s, c in finished if c == "anime"], "anime"))
    first = rows[0][0]
    for n in RULES["years"]:
        try:
            day = first.replace(year=first.year + n)
        except ValueError:                       # 29 February
            day = first.replace(year=first.year + n, day=28)
        if day > now:
            break
        out.append({"kind": "years", "value": n, "reached_at": day, "title": None})
    out.sort(key=lambda m: m["reached_at"])
    return out


def _mkey(m: dict) -> str:
    return f"{m['kind']}:{m['value']}"


def _ordinal(n: int) -> str:
    if n == 1:
        return "first"
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n:,}{suf}"


def phrase(m: dict, lang: str = "en") -> str:
    """'100th film', '1,000 hours watched', 'first anime series finished'.
    English for the facts the curator reads; the page asks for the user's
    language (``lang``, frontend/i18n "milestone.*": "100. Film geschaut")."""
    k, v = m.get("kind"), int(m.get("value") or 0)
    if lang != "en" and k in _PHRASE_KINDS:
        from src.services.i18n import LANGUAGES, t
        if lang in LANGUAGES:
            num = f"{v:,}".replace(",", ".") if lang == "de" else f"{v:,}"
            return t(f"milestone.{k}", lang, n=v, num=num, ord=f"{num}." if lang == "de" else _ordinal(v))
    return {"films": f"{_ordinal(v)} film watched",
            "series": f"{_ordinal(v)} series finished",
            "anime": f"{_ordinal(v)} anime series finished",
            "episodes": f"{v:,} episodes watched",
            "hours": f"{v:,} hours played",
            "artists": f"{v:,} different artists heard",
            "years": f"{v} year{'s' if v != 1 else ''} since the first play"}.get(k, f"{k} {v}")


_PHRASE_KINDS = ("films", "series", "anime", "episodes", "hours", "artists", "years")


def _load(user_id: int) -> dict:
    from src.services.app_state import get_state
    try:
        d = json.loads(get_state(_KEY.format(uid=user_id)) or "{}")
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}


def store(user_id: int, items: list, now: Optional[datetime] = None) -> list:
    """Save the member's milestones; returns the newly reached ones worth
    announcing (not seen before, reached in the last ANNOUNCE_DAYS, of a
    kind not switched off). The first run announces nothing. Blocking."""
    from src.services.app_state import set_state
    now = now or datetime.utcnow()
    prev = _load(user_id)
    known = {_mkey(m) for m in prev.get("items") or []}
    off = kinds_off(user_id)
    fresh = []
    if prev:
        fresh = [m for m in items if _mkey(m) not in known and m["kind"] not in off
                 and (now - m["reached_at"]).days <= ANNOUNCE_DAYS]
    pending = [k for k in prev.get("pending") or [] if k in {_mkey(m) for m in items}]
    pending += [_mkey(m) for m in fresh if _mkey(m) not in pending]
    set_state(_KEY.format(uid=user_id), json.dumps({
        "items": [{**m, "reached_at": m["reached_at"].isoformat()} for m in items],
        "pending": pending, "computed_at": now.isoformat()}))
    return fresh


def history(user_id: int) -> list:
    """The stored milestones, newest first, kinds switched off left out. Blocking."""
    off = kinds_off(user_id)
    items = [m for m in _load(user_id).get("items") or [] if m.get("kind") not in off]
    return sorted(items, key=lambda m: m["reached_at"], reverse=True)


def pending_announcements(user_id: int) -> list:
    """Newly reached milestones the curator has not offered yet. Blocking."""
    d = _load(user_id)
    keys = set(d.get("pending") or [])
    off = kinds_off(user_id)
    now = datetime.utcnow()
    out = []
    for m in d.get("items") or []:
        if _mkey(m) not in keys or m.get("kind") in off:
            continue
        try:
            if (now - datetime.fromisoformat(m["reached_at"])).days > ANNOUNCE_DAYS:
                continue                 # not offered in time: history now, not news
        except (ValueError, TypeError, KeyError):
            continue
        out.append(m)
    return out


def mark_announced(user_id: int, keys: list) -> None:
    """These milestones were offered once; never again. Blocking."""
    from src.services.app_state import set_state
    d = _load(user_id)
    if not d:
        return
    d["pending"] = [k for k in d.get("pending") or [] if k not in set(keys)]
    set_state(_KEY.format(uid=user_id), json.dumps(d))


def kinds_off(user_id: int) -> set:
    from src.services.app_state import get_state
    try:
        return set(json.loads(get_state(_OFF.format(uid=user_id)) or "[]")) & set(KINDS)
    except (ValueError, TypeError):
        return set()


def set_kind(user_id: int, kind: str, on: bool) -> set:
    """Switch one kind on or off for this member; returns the kinds off. Blocking."""
    from src.services.app_state import set_state
    if kind not in KINDS:
        raise ValueError(f"unknown milestone kind {kind!r}")
    off = kinds_off(user_id)
    off = (off - {kind}) if on else (off | {kind})
    set_state(_OFF.format(uid=user_id), json.dumps(sorted(off)))
    return off


def in_period(user_id: int, start: datetime, end: datetime) -> list:
    """The member's milestones reached in [start, end), oldest first (the
    Wrap-Up slide). Blocking."""
    out = []
    for m in history(user_id):
        try:
            at = datetime.fromisoformat(m["reached_at"])
        except (ValueError, TypeError, KeyError):
            continue
        if start <= at < end:
            out.append(m)
    return sorted(out, key=lambda m: m["reached_at"])


async def run_milestones(task=None) -> bool:
    """The custodian job: every active member's milestones, recomputed."""
    from src.database.connection import get_db_session
    from src.database.models import User
    from src.database.runner import run_read, run_write
    from src.services.proactive_messages import _get_sonarr_index

    def _users():
        with get_db_session() as db:
            return [u for (u,) in db.query(User.id).filter(User.is_active == True).all()]  # noqa: E712
    index = await _get_sonarr_index()
    for uid in await run_read(_users):
        items = await run_read(compute, uid, index)
        fresh = await run_write(store, uid, items)
        if fresh:
            logger.info("[milestones] user %d reached %s", uid, ", ".join(_mkey(m) for m in fresh))
    return True
