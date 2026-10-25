"""The household in a wrap-up, only for those who said yes.

Owner decision (docs/WRAPUP_V2_BLUEPRINT.md §4.5): anything that reaches
other people is opt-in. A member who turns this on is compared with the
other members who turned it on, and with nobody else:

* their rank by hours among them;
* the household median on each axis of the taste radar (only with two or
  more others, so the line is never one person's shape);
* a title in their top five that no other of them ever played ("only you").

No other member's name, title or number is shown, only the rank and the
median. Nothing here is stored: it is worked out from the others' stored
wrap-ups each time the page is opened, so turning it off takes a member out
of everyone's comparison at once. The curator's story is not given it, so
no written text can keep it either.
"""
from __future__ import annotations

import logging
from statistics import median
from typing import Optional

from src.database.connection import get_db_session

logger = logging.getLogger(__name__)

KEY = "wrapup_household:{uid}"
MEDIAN_MIN_OTHERS = 2


def consented(user_id: int) -> bool:
    """Opt-in, off until the member turns it on. Blocking."""
    from src.services.app_state import get_state
    return get_state(KEY.format(uid=user_id)) == "1"


def set_consent(user_id: int, on: bool) -> None:
    from src.services.app_state import set_state
    set_state(KEY.format(uid=user_id), "1" if on else "")


def radar_axes(f: dict) -> dict:
    """The taste radar's axes, as frontend/js/wrapup_story.radarAxes draws
    them: {name: 0..1}, only the axes the facts have."""
    t, top = f.get("tempo") or {}, (f.get("top") or [None])[0]
    hc, c = f.get("highbrow_comfort"), f.get("contrarian")
    ax = {
        "Acclaim": (hc or {}).get("highbrow"),
        "Novelty": (f.get("discovery") or {}).get("new_share"),
        "Commitment": (f.get("completion") or {}).get("completion_rate"),
        "Catalogue": ((f.get("eras") or {}).get("split") or {}).get("catalogue"),
        "Contrarian": min(1, (c.get("gap") or 0) / 3) if c else (0 if hc else None),
        "Focus": (min(1, top["hours"] / t["hours"] * 2)
                  if top and top.get("hours") and t.get("hours") else None),
    }
    return {k: float(v) for k, v in ax.items() if isinstance(v, (int, float))}


def _others(user_id: int) -> list:
    from src.database.models import User
    with get_db_session() as db:
        ids = [u.id for u in db.query(User.id).filter(User.is_active == True,  # noqa: E712
                                                      User.id != user_id)]
    return [uid for uid in ids if consented(uid)]


def _played_by(db, user_ids: list, titles: list) -> set:
    from sqlalchemy import func
    from src.database import orphaned
    from src.database.models import WatchHistoryEntry as W
    orphaned.include(db)
    key = func.coalesce(W.series_title, W.title)
    return {k for (k,) in db.query(key).filter(W.user_id.in_(user_ids), key.in_(titles)).distinct()}


def compare(user_id: int, scope: str, period_key: str, facts: dict) -> Optional[dict]:
    """The household block for this wrap-up, or {"consented": False}.
    Blocking — call via ``run_read``."""
    from src.services.wrapup_facts import FACTS_VERSION
    from src.services.wrapup_store import load
    if not consented(user_id):
        return {"consented": False}
    out: dict = {"consented": True, "members": 1}
    if not facts or facts.get("empty") or not facts.get("eligible"):
        return out
    theirs = []
    for uid in _others(user_id):
        row = load(uid, scope, period_key) or {}
        f = row.get("facts") or {}
        if (f.get("facts_version") == FACTS_VERSION and not f.get("empty")
                and f.get("eligible") and (f.get("tempo") or {}).get("hours")):
            theirs.append((uid, f))
    if not theirs:
        return out
    out["members"] = len(theirs) + 1
    mine = (facts.get("tempo") or {}).get("hours") or 0
    out["rank_hours"] = 1 + sum(1 for _, f in theirs if f["tempo"]["hours"] > mine)
    if len(theirs) >= MEDIAN_MIN_OTHERS:
        axes = [radar_axes(f) for _, f in theirs]
        med = {}
        for name in radar_axes(facts):
            vals = [a[name] for a in axes if name in a]
            if len(vals) >= MEDIAN_MIN_OTHERS:
                med[name] = round(median(vals), 2)
        if med:
            out["radar_median"] = med
    titles = [t["title"] for t in (facts.get("top") or [])[:5] if t.get("title")]
    if titles:
        with get_db_session() as db:
            seen = _played_by(db, [uid for uid, _ in theirs], titles)
        only = next((t for t in titles if t not in seen), None)
        if only:
            out["only_you"] = only
    return out
