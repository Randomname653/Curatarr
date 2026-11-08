"""Curatarr — the Wrap-Up's small moments on the chat landing (UX design
record, stages 3.3 / 3.4).

The Wrap-Up had one door, the sidebar; a week or a month never came to the
member. These cards do, at the moments they belong to, on the member's
clock, and say only what the facts say:

* "Your week" - Sunday from 17:00 to the end of Monday: the week's hours,
  the days with something on, what most of it was, a genre new to them.
  Deterministic; no model; only when the week reached the Wrap-Up's own
  threshold (five plays, three hours), so a quiet week gets no card rather
  than a small number;
* "Your month is ready" - the first seven days of a month, once the month's
  story is written (the custodian writes it on the 1st; services/wrapup_story);
* "Your year is ready" - from 1 December, once the year's story is written.

No streaks, no "you missed", nothing about a gap: a card is either there to
enjoy or not there. Every card can be hidden. The words come in the language
the member writes to the curator in (en/de, services/user_language).
"""
import time
from datetime import datetime, timedelta
from typing import Optional

_WEEK_TTL_S = 1800
_week_cache: dict = {}          # (user id, week key) -> (monotonic, card or None)

_TEXT = {
    "en": {"week": "Your week", "week_line": "{hours} h · {days} days with something on",
           "most": "Most of it: **{title}** ({hours} h)", "new": "New for you: {genre}"},
    "de": {"week": "Deine Woche", "week_line": "{hours} Std · an {days} Tagen lief etwas",
           "most": "Am meisten: **{title}** ({hours} Std)", "new": "Neu für dich: {genre}"},
}


def _t(lang: str) -> dict:
    return _TEXT.get(lang) or _TEXT["en"]


def _num(h: float) -> str:
    return f"{h:.0f}" if h >= 10 or float(h).is_integer() else f"{h:.1f}"


def week_due(local: datetime) -> Optional[datetime]:
    """The week the card is about at this wall-clock moment: Sunday from
    17:00 the week now ending, Monday the week just ended; None otherwise."""
    wd = local.weekday()
    if wd == 6 and local.hour >= 17:
        return local
    if wd == 0:
        return local - timedelta(days=1)
    return None


def week_card(user_id: int, now: Optional[datetime] = None, zone=None,
              lang: Optional[str] = None) -> Optional[dict]:
    """{"kind": "week", "key", "label", "lines": [...]} or None. Blocking."""
    from src.services.chat_starters import local_now
    from src.services.user_language import user_language
    from src.services.user_time import user_zone_by_id
    from src.services.wrapup_facts import build_wrapup_facts, week_key
    now = now or datetime.utcnow()
    local = local_now(zone or user_zone_by_id(user_id), now)
    day = week_due(local)
    if day is None:
        return None
    key = week_key(day)
    hit = _week_cache.get((user_id, key))
    if hit and time.monotonic() - hit[0] < _WEEK_TTL_S:
        facts = hit[1]
    else:
        facts = build_wrapup_facts(user_id, "all", key, now=now)
        _week_cache[(user_id, key)] = (time.monotonic(), facts)
    if facts.get("empty") or not facts.get("eligible"):
        return None
    lang = lang or user_language(user_id)
    tx = _t(lang)
    tempo = facts.get("tempo") or {}
    lines = [tx["week_line"].format(hours=_num(tempo.get("hours") or 0), days=tempo.get("active_days") or 0)]
    top = (facts.get("top") or [None])[0]
    if top and top.get("hours"):
        lines.append(tx["most"].format(title=top["title"], hours=_num(top["hours"])))
    new = ((facts.get("genres") or {}).get("first_time") or [None])[0]
    if new:
        lines.append(tx["new"].format(genre=new))
    return {"kind": "week", "key": key, "label": tx["week"], "lines": lines}
