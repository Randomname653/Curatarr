"""Curatarr — the player of every Plex play, and the moods of every artist.

Two things the Wrap-Up's rhythm clock and "phone after midnight" need, and
nothing stored (2026-10-25, measured on the real server):

* Every Plex history event names its device (``deviceID``); Plex's
  ``/devices`` gives that device's name and platform ("iPhone" / "iOS",
  "Chromecast Google TV" / "Android"). The sync writes them from now on
  (plex_sync.player_of); ``run_player_backfill`` fills the plays already
  stored, once, from the whole history Plex still holds.
* Plex keeps moods and styles per ARTIST, not per track ("Atmospheric,
  Melodic, Warm" / "Indie Folk"), and only in an artist's own metadata, not
  in the listing. ``run_artist_tags`` reads them into the Plex music index
  (``plex_artists`` in lyrics.db), the most-played first, a budget a run,
  each artist again after 90 days.

No model in either.
"""
import asyncio
import calendar
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from src.database.connection import get_db_session

logger = logging.getLogger(__name__)

PLAYERS_DONE_KEY = "plex_players_backfilled"
_PAGE = 1000
_SAME_PLAY_S = 90
_TAGS_BUDGET = 300
_TAGS_MAX_AGE = timedelta(days=90)


def _plex():
    from src.config import settings
    url, token = settings.effective_plex_url, settings.effective_plex_token
    if not url or not token:
        return None, None
    return url, {"Accept": "application/json", "X-Plex-Token": token,
                 "X-Plex-Client-Identifier": settings.PLEX_CLIENT_ID}


# ── the player of the plays already stored ──────────────────────────────────

def fill_players(by_rk: dict, players: dict, by_ts: Optional[dict] = None) -> int:
    """Set player and platform on stored Plex plays that have none, from the
    history event of the same item within 90 s, else (``by_ts``: {second:
    [(device, title)]}) an event of the same title within 2 s: a track Plex
    re-added under a new key keeps its history under the new one (seen live
    on 15,149 of 27,505 music plays). Blocking — call via ``run_write``.
    Returns the rows filled."""
    from sqlalchemy import func
    from src.database.models import WatchHistoryEntry as W
    filled = 0
    with get_db_session() as db:
        rows = (db.query(W.id, W.plex_item_id, W.viewed_at, W.title)
                .filter(W.platform.is_(None), func.coalesce(W.source, "") != "spotify",
                        W.viewed_at.isnot(None)).all())
        updates = []
        for rid, rk, at, title in rows:
            ts = calendar.timegm(at.utctimetuple())
            near = [(abs(t - ts), d) for t, d in by_rk.get(str(rk), ()) if abs(t - ts) <= _SAME_PLAY_S]
            if not near and by_ts and title:
                want = title.strip().lower()
                near = [(abs(dt), d) for dt in (0, -1, 1, -2, 2)
                        for d, t in by_ts.get(ts + dt, ()) if t == want]
            if not near:
                continue
            name, platform = players.get(min(near)[1], (None, None))
            if name or platform:
                updates.append({"id": rid, "player": name, "platform": platform})
        for i in range(0, len(updates), 2000):
            db.bulk_update_mappings(W, updates[i:i + 2000])
            db.commit()
        filled = len(updates)
    return filled


async def run_player_backfill(task=None) -> bool:
    """Once: the whole history Plex holds, matched to the stored plays."""
    import httpx
    from src.database.runner import run_read, run_write
    from src.services.app_state import get_state, set_state
    from src.services.http_client import tls_context
    from src.services.plex_sync import fetch_players
    if await run_read(get_state, PLAYERS_DONE_KEY) == "1":
        return True
    url, headers = _plex()
    if not url:
        return True
    players = await fetch_players(url, headers)
    if not players:
        return False                 # Plex down or no devices: try again next tick
    by_rk: dict = {}
    by_ts: dict = {}
    start, complete = 0, False
    async with httpx.AsyncClient(verify=tls_context(), timeout=60) as client:
        while True:
            r = await client.get(f"{url}/status/sessions/history/all", params={"sort": "viewedAt:desc"},
                                 headers={**headers, "X-Plex-Container-Start": str(start),
                                          "X-Plex-Container-Size": str(_PAGE)})
            if r.status_code != 200:
                break
            items = (r.json().get("MediaContainer") or {}).get("Metadata") or []
            for h in items:
                if h.get("viewedAt") is None or h.get("deviceID") is None:
                    continue
                vts, dev = int(h["viewedAt"]), int(h["deviceID"])
                # Most track events of the real server have no ratingKey any
                # more (the track was re-added): they still name the second,
                # the title and the device.
                if h.get("ratingKey"):
                    by_rk.setdefault(str(h["ratingKey"]), []).append((vts, dev))
                by_ts.setdefault(vts, []).append((dev, (h.get("title") or "").strip().lower()))
            if len(items) < _PAGE:
                complete = True
                break
            start += len(items)
    if not complete:
        return False
    filled = await run_write(fill_players, by_rk, players, by_ts)
    await run_write(set_state, PLAYERS_DONE_KEY, "1")
    logger.info("[players] %d stored Plex play(s) got their player from %d history item(s)",
                filled, len(by_rk))
    return True


# ── the moods and styles of every artist ────────────────────────────────────

def _due_artists(limit: int, now: Optional[datetime] = None, db_path=None) -> list:
    """Artist keys without tags, or with tags older than 90 days, the most
    tracks first. Blocking."""
    from src.services import lyrics
    cutoff = ((now or datetime.utcnow()) - _TAGS_MAX_AGE).isoformat()
    con = lyrics._connect(db_path)
    try:
        return [r[0] for r in con.execute(
            "SELECT artist_key FROM plex_artists WHERE tracks > 0 AND (tags_at IS NULL OR tags_at < ?) "
            "ORDER BY tracks DESC LIMIT ?", (cutoff, limit))]
    finally:
        con.close()


def save_artist_tags(tags: dict, now: Optional[datetime] = None, db_path=None) -> int:
    """tags: {artist key: {moods, styles, genres, country}}. Blocking."""
    from src.services import lyrics
    stamp = (now or datetime.utcnow()).isoformat()
    con = lyrics._connect(db_path)
    try:
        for key, t in tags.items():
            con.execute("UPDATE plex_artists SET moods=?, styles=?, genres=?, country=?, tags_at=? "
                        "WHERE artist_key=?",
                        (json.dumps(t.get("moods") or []), json.dumps(t.get("styles") or []),
                         json.dumps(t.get("genres") or []), t.get("country") or None, stamp, str(key)))
        con.commit()
    finally:
        con.close()
    return len(tags)


def tags_of(md: dict) -> dict:
    """The tags of one artist's Plex metadata."""
    def names(k):
        return [x.get("tag") for x in md.get(k) or [] if x.get("tag")]
    return {"moods": names("Mood"), "styles": names("Style"), "genres": names("Genre"),
            "country": (names("Country") or [None])[0]}


async def run_artist_tags(task=None) -> bool:
    """A budget of artists a run; True when none is due any more."""
    import httpx
    from src.database.runner import run_read, run_write
    from src.services.http_client import tls_context
    url, headers = _plex()
    if not url:
        return True
    try:
        keys = await run_read(_due_artists, _TAGS_BUDGET)
    except Exception as e:  # noqa: BLE001 - no music index yet
        logger.debug("[artist-tags] no Plex music index: %s", e)
        return True
    if not keys:
        return True
    sem = asyncio.Semaphore(4)
    tags: dict = {}

    async with httpx.AsyncClient(verify=tls_context(), timeout=30) as client:
        async def one(key):
            async with sem:
                try:
                    r = await client.get(f"{url}/library/metadata/{key}", headers=headers)
                except Exception as e:  # noqa: BLE001 - this artist waits for the next run
                    logger.debug("[artist-tags] %s: %s", key, e)
                    return
                if r.status_code == 200:
                    md = ((r.json().get("MediaContainer") or {}).get("Metadata") or [{}])[0]
                    tags[key] = tags_of(md)
                elif r.status_code == 404:
                    tags[key] = {}       # gone from Plex: the index walk drops it; no retry storm
        await asyncio.gather(*(one(k) for k in keys))
    await run_write(save_artist_tags, tags)
    with_moods = sum(1 for t in tags.values() if t.get("moods"))
    logger.info("[artist-tags] %d artist(s) read, %d with moods", len(tags), with_moods)
    return len(keys) < _TAGS_BUDGET
