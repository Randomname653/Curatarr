"""
Curatarr — the owner's pins for the Shoko watched sync.

The sync (services/shoko_sync) places a play by its file, else by TMDB,
else by the current copy of its series. What it cannot place stays
"unmatched": a series Plex now calls differently ("Food Wars!" became
"Food Wars! Shokugeki no Soma"), one that left the library, or a western
cartoon filed as anime that AniDB does not know. A pin settles it per
series and season, as the watch history spells them:

- link: the season's plays are episodes of this AniDB anime; play
  episode N is the anime's regular episode N. Per season, because AniDB
  keeps one entry per season, where TVDB keeps one show; and from an
  episode on, because AniDB also splits a season into cours: a second pin
  "from episode 12" makes episode 12 the next entry's episode 1. Each pin
  covers its episodes up to the next pin's start.
- skip: not in Shoko; the plays stop being retried.

A pin wins over the automatic match. Setting or lifting one hands that
series' unmatched and skipped plays back to the next sync run; plays
already sent stay sent. Every function here is synchronous DB work: call
it through run_read / run_write.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from src.services.shoko_sync import _norm_title

ACTIONS = ("link", "skip")


def load_pins() -> dict:
    """{(series_key, season): [{"action", "anidb_id", "from_episode"}, ...]},
    each list by its first episode, latest start first."""
    from src.database.connection import get_db_session
    from src.database.models import ShokoPin
    out: dict = {}
    with get_db_session() as db:
        for p in db.query(ShokoPin).all():
            out.setdefault((p.series_key, p.season), []).append(
                {"action": p.action, "anidb_id": p.anidb_id, "from_episode": p.from_episode or 1})
    for lst in out.values():
        lst.sort(key=lambda x: -x["from_episode"])
    return out


def pin_for(pins: dict, series_key: str, season, episode) -> Optional[dict]:
    """The pin that covers this episode: of the season's pins, the one with
    the latest start at or before it."""
    ep = episode or 1
    for pin in pins.get((series_key, season)) or ():
        if pin["from_episode"] <= ep:
            return pin
    return None


def _reset(db, key: str, season: Optional[int]) -> int:
    """Drop the ledger rows of this series and season that never reached
    Shoko, so the next run tries them again under the new word."""
    from src.database.models import ShokoWatchSync, WatchHistoryEntry as W
    rows = (db.query(ShokoWatchSync, W.series_title, W.season)
            .join(W, W.id == ShokoWatchSync.watch_history_id)
            .filter(ShokoWatchSync.status.in_(("unmatched", "skipped"))).all())
    n = 0
    for led, title, s in rows:
        if _norm_title(title) == key and s == season:
            db.delete(led)
            n += 1
    return n


def set_pin(series_title: str, season: Optional[int], action: str, *,
            anidb_id: Optional[int] = None, shoko_title: Optional[str] = None,
            user_id: Optional[int] = None, from_episode: int = 1) -> dict:
    """Create or replace the pin of one series, season and first episode."""
    from src.database.connection import get_db_session
    from src.database.models import ShokoPin
    key = _norm_title(series_title)
    if not key:
        raise ValueError("a pin needs a series title")
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    if action == "link" and not anidb_id:
        raise ValueError("a link needs an AniDB id")
    from_episode = int(from_episode or 1)
    if from_episode < 1:
        raise ValueError("a pin starts at episode 1 or later")
    with get_db_session() as db:
        pin = (db.query(ShokoPin).filter(ShokoPin.series_key == key,
                                         ShokoPin.season.is_(None) if season is None
                                         else ShokoPin.season == season,
                                         ShokoPin.from_episode == from_episode).first())
        if pin is None:
            pin = ShokoPin(series_key=key, season=season, from_episode=from_episode)
            db.add(pin)
        pin.series_title = series_title
        pin.action = action
        pin.anidb_id = anidb_id if action == "link" else None
        pin.shoko_title = shoko_title if action == "link" else None
        pin.created_by = user_id
        pin.created_at = datetime.utcnow()
        requeued = _reset(db, key, season)
        db.commit()
        return {"id": pin.id, "requeued": requeued}


def remove_pin(pin_id: int) -> Optional[dict]:
    """Lift a pin; None when there is none with that id."""
    from src.database.connection import get_db_session
    from src.database.models import ShokoPin
    with get_db_session() as db:
        pin = db.get(ShokoPin, pin_id)
        if pin is None:
            return None
        requeued = _reset(db, pin.series_key, pin.season)
        db.delete(pin)
        db.commit()
        return {"id": pin_id, "requeued": requeued}


def overview(limit: int = 200) -> dict:
    """What the Settings card lists: the series and seasons whose plays
    found no Shoko episode (or are skipped), most plays first, and every
    pin. Each group: series, season, plays, last_watched, status, pin."""
    from src.database.connection import get_db_session
    from src.database.models import ShokoPin, ShokoWatchSync, WatchHistoryEntry as W
    with get_db_session() as db:
        pins = db.query(ShokoPin).order_by(ShokoPin.series_title, ShokoPin.season,
                                           ShokoPin.from_episode).all()
        by_key: dict = {}
        for p in pins:
            by_key.setdefault((p.series_key, p.season), []).append(p)
        rows = (db.query(ShokoWatchSync.status, W.series_title, W.title, W.season, W.viewed_at)
                .join(W, W.id == ShokoWatchSync.watch_history_id)
                .filter(ShokoWatchSync.status.in_(("unmatched", "skipped"))).all())
        groups: dict = {}
        for status, series, title, season, viewed in rows:
            name = series or title or "?"
            key = (_norm_title(name), season)
            g = groups.setdefault(key, {"series": name, "season": season, "plays": 0,
                                        "last_watched": None, "statuses": set()})
            g["plays"] += 1
            g["statuses"].add(status)
            if viewed and (g["last_watched"] is None or viewed > g["last_watched"]):
                g["last_watched"] = viewed
        out = []
        for key, g in groups.items():
            out.append({"series": g["series"], "season": g["season"], "plays": g["plays"],
                        "last_watched": g["last_watched"].isoformat() if g["last_watched"] else None,
                        "status": "skipped" if g["statuses"] == {"skipped"} else "unmatched",
                        "pins": [_pin_dict(p) for p in by_key.get(key, [])]})
        out.sort(key=lambda g: (-g["plays"], g["series"].casefold(), g["season"] or 0))
        return {"groups": out[:limit], "total_groups": len(out),
                "pins": [_pin_dict(p) for p in pins]}


def _pin_dict(p) -> dict:
    return {"id": p.id, "series": p.series_title, "season": p.season,
            "from_episode": p.from_episode or 1, "action": p.action,
            "anidb_id": p.anidb_id, "shoko_title": p.shoko_title,
            "created_at": p.created_at.isoformat() if p.created_at else None}
