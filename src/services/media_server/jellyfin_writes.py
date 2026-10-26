"""Curatarr's writes into Jellyfin (docs/JELLYFIN_DESIGN.md §5, phase 4).

The Jellyfin side of ``plex_playlists``, ``plex_collections`` and
``plex_watchlist``: those modules hand over here on a Jellyfin install, with
the same inputs and the same answers, so the custodian jobs, the Wrap-Up
page and the chat's protection hook work unchanged.

What differs from Plex:
- one API key writes every member's playlists (``UserId`` owns them): no
  per-user token, so nobody is skipped for lacking one;
- playlists and collections are edited in place, never deleted and
  recreated, so their ids (and a member's pins) survive a refresh;
- collections span libraries; the "Curatarr · " prefix still marks ours;
- the watchlist is a per-user playlist, "Curatarr Watchlist" (design D7).

Keys a recommendation row carries are re-checked and, when gone (or never
resolved), found again by title in the category's libraries, then healed
onto the row - the same self-heal the Plex push does.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from src.services.media_server.base import ServerUser, is_jellyfin_key

logger = logging.getLogger(__name__)

WATCHLIST_TITLE = "Curatarr Watchlist"
_KIND = {"movie": "movie", "show": "series", "anime": "series"}


def _adapter(adapter=None):
    if adapter is not None:
        return adapter
    from src.services.media_server import get_adapter
    return get_adapter()


def _server_user_row(user_id: int):
    from src.database.connection import get_db_session
    from src.database.models import User
    with get_db_session() as db:
        u = db.query(User.plex_user_id, User.plex_username).filter(User.id == user_id).first()
        return (u.plex_user_id, u.plex_username) if u else (None, None)


async def server_user(user_id: int) -> Optional[ServerUser]:
    from src.database.runner import run_read
    key, name = await run_read(_server_user_row, user_id)
    return ServerUser(key=key, name=name or "") if is_jellyfin_key(key) else None


def _due(prev: dict, rows: list) -> bool:
    """The Plex push's freshness rule: never pushed, recommendations newer
    than the last push, or the last push older than a week."""
    from src.services.plex_playlists import _REPUSH_HOURS
    pushed_at = (prev or {}).get("pushed_at")
    if not pushed_at:
        return True
    try:
        pushed = datetime.fromisoformat(pushed_at)
    except (ValueError, TypeError):
        return True
    newest = max((r["cached_at"] for r in rows if r.get("cached_at")), default=None)
    fresh = newest is None or newest <= pushed
    recent = (datetime.utcnow() - pushed).total_seconds() < _REPUSH_HOURS * 3600
    return not (fresh and recent)


async def _resolve(adapter, title: str, category: str, key: Optional[str], sections: list,
                   *, user_id: Optional[int] = None, year: Optional[int] = None) -> Optional[str]:
    """A rec row's key if Jellyfin still has it, else found again by title
    (and healed onto the row when ``user_id`` is given)."""
    from src.database.runner import run_write
    from src.services.plex_playlists import _heal_rec_key
    if key and is_jellyfin_key(key) and await adapter.key_exists(key):
        return key
    found = await adapter.search(title, _KIND.get(category, "movie"), library_keys=sections, year=year)
    if found and user_id is not None and found != key:
        await run_write(_heal_rec_key, user_id, category, title, found)
    return found


async def push_user_playlists(user, *, adapter=None) -> dict:
    """The "Curatarr Recommended · Movies / Shows / Anime" playlists."""
    from src.database.runner import run_read, run_write
    from src.services.plex_playlists import (PLAYLIST_TITLES, _library_recs, _load_snapshot,
                                             _save_snapshot, _section_keys)
    who = await server_user(user.id)
    if who is None:
        return {"pushed": [], "skipped": "no_jellyfin_account"}
    adapter = _adapter(adapter)
    snap = await run_read(_load_snapshot, user.id)
    pushed = []
    for category, title in PLAYLIST_TITLES.items():
        rows = await run_read(_library_recs, user.id, category, 10)
        if not rows or not _due(snap.get(category), rows):
            continue
        sections = await run_read(_section_keys, category)
        keys, titles = [], []
        for r in rows:
            key = await _resolve(adapter, r["title"], category, r["key"], sections, user_id=user.id)
            if key and category in ("show", "anime"):
                key = await adapter.next_episode(who, key)   # a series id expands to every episode
            if key:
                keys.append(key)
                titles.append(r["title"])
        if not keys:
            logger.info("[playlists] %s/%s: nothing resolvable in Jellyfin - skipped", who.name, category)
            continue
        pid = await adapter.upsert_playlist(who, title, keys, "Video")
        snap[category] = {"pushed_at": datetime.utcnow().isoformat(), "playlist_key": pid, "titles": titles}
        pushed.append(category)
        logger.info("[playlists] %s: pushed '%s' (%d items, Jellyfin)", who.name, title, len(keys))
    await run_write(_save_snapshot, user.id, snap)
    return {"pushed": pushed}


async def push_user_music_playlist(user, *, adapter=None) -> dict:
    """One unheard album per recommended artist (an album id adds its tracks)."""
    from src.database.runner import run_read, run_write
    from src.services.plex_playlists import (MUSIC_PLAYLIST_TITLE, _MAX_MUSIC_ARTISTS, _library_recs,
                                             _load_snapshot, _save_snapshot, _section_keys)
    who = await server_user(user.id)
    if who is None:
        return {"pushed": [], "skipped": "no_jellyfin_account"}
    adapter = _adapter(adapter)
    rows = await run_read(_library_recs, user.id, "music", _MAX_MUSIC_ARTISTS)
    snap = await run_read(_load_snapshot, user.id)
    if not rows or not _due(snap.get("music"), rows):
        return {"pushed": []}
    sections = await run_read(_section_keys, "music")
    keys, titles = [], []
    for r in rows:
        artist = await adapter.search(r["title"], "artist", library_keys=sections)
        album = await adapter.unheard_album(who, artist) if artist else None
        if album:
            keys.append(album)
            titles.append(r["title"])
        else:
            logger.info("[playlists] %s/music: %r not in Jellyfin - skipped", who.name, r["title"])
    if not keys:
        return {"pushed": []}
    pid = await adapter.upsert_playlist(who, MUSIC_PLAYLIST_TITLE, keys, "Audio")
    snap["music"] = {"pushed_at": datetime.utcnow().isoformat(), "playlist_key": pid, "titles": titles}
    await run_write(_save_snapshot, user.id, snap)
    return {"pushed": ["music"]}


async def push_wrapup_playlist(user, label: str, videos: list, tracks: list, *, adapter=None) -> dict:
    """The Wrap-Up's titles one tap away: ``{"pushed": [{title, items}],
    "missing": [title]}``, as the Plex push answers."""
    from src.database.runner import run_read
    from src.services.plex_playlists import _MAX_WRAPUP_ITEMS, WRAPUP_PLAYLIST_PREFIX, _section_keys
    who = await server_user(user.id)
    if who is None:
        return {"skipped": "no_jellyfin_account"}
    adapter = _adapter(adapter)
    title = WRAPUP_PLAYLIST_PREFIX + label
    missing, video_keys, track_keys, sections = [], [], [], {}
    for v in videos[:_MAX_WRAPUP_ITEMS]:
        cat = v.get("media_type")
        if cat not in _KIND:
            continue
        if cat not in sections:
            sections[cat] = await run_read(_section_keys, cat)
        key = await adapter.search(v["title"], _KIND[cat], library_keys=sections[cat])
        if key and cat in ("show", "anime"):
            key = await adapter.next_episode(who, key)
        (video_keys.append(key) if key else missing.append(v["title"]))
    music = await run_read(_section_keys, "music") if tracks else []
    for t in tracks[:_MAX_WRAPUP_ITEMS]:
        key = await adapter.search(t["track"], "track", library_keys=music, artist=t.get("artist"))
        (track_keys.append(key) if key else missing.append(t["track"]))
    pushed = []
    for media, keys, suffix in (("Video", video_keys, ""), ("Audio", track_keys, " · Music")):
        if not keys:
            continue
        name = title + (suffix if video_keys and track_keys else "")
        await adapter.upsert_playlist(who, name, keys, media)
        pushed.append({"title": name, "items": len(keys)})
    return {"pushed": pushed, "missing": missing}


async def push_collections(designs: list, *, adapter=None) -> dict:
    """The designer's shelves as Jellyfin collections: each one created or
    edited in place; our earlier shelves not in this rotation removed (the
    titles in them stay). Same snapshot as the Plex push."""
    from src.database.runner import run_write
    from src.services.app_state import set_state
    from src.services.plex_collections import COLLECTION_PREFIX
    adapter = _adapter(adapter)
    created = failed = deleted = 0
    results, keep = [], set()
    for d in designs:
        try:
            cid = await adapter.upsert_collection(None, d["title"], d["keys"])
        except Exception as e:  # noqa: BLE001 - one shelf's failure must not cost the others; logged
            failed += 1
            logger.warning("[collections] %r not written to Jellyfin: %s", d.get("title"), e)
            continue
        created += 1
        keep.add(d["title"])
        results.append({"title": d["title"], "section_key": str(d.get("section_key")),
                        "count": len(d["keys"]), "rating_key": cid})
    for coll in await adapter.collections(COLLECTION_PREFIX):
        if coll["title"] not in keep:
            await adapter.remove_collection(coll["key"])
            deleted += 1
    await run_write(set_state, "curatarr_collections", json.dumps({
        "pushed_at": datetime.utcnow().isoformat(), "designs": results}))
    return {"created": created, "deleted": deleted, "failed": failed}


async def add_to_watchlist(user_id: int, title: str, year: Optional[int] = None,
                           media_type: Optional[str] = None, *, adapter=None) -> dict:
    """``title`` onto this user's "Curatarr Watchlist" playlist. Only titles
    in the library: Jellyfin has no catalogue of the rest. A series goes on
    as its next unwatched episode. Never raises (the Plex function's contract)."""
    from src.database.runner import run_read
    from src.services.plex_playlists import _section_keys
    try:
        who = await server_user(user_id)
        if who is None:
            return {"ok": False, "error": "user has no Jellyfin account on file"}
        adapter = _adapter(adapter)
        cats = [media_type] if media_type in _KIND else ["movie", "show", "anime"]
        key = cat = None
        for c in cats:
            key = await adapter.search(title, _KIND[c], library_keys=await run_read(_section_keys, c), year=year)
            if key:
                cat = c
                break
        if not key:
            return {"ok": False, "error": f"{title!r} is not in the Jellyfin library"}
        if cat in ("show", "anime"):
            key = await adapter.next_episode(who, key) or key
        current = await adapter.playlist_keys(who, WATCHLIST_TITLE)
        if key not in current:
            await adapter.upsert_playlist(who, WATCHLIST_TITLE, current + [key], "Video")
        logger.info("[watchlist] user %d: %s on the Curatarr Watchlist (Jellyfin)", user_id, title)
        return {"ok": True, "matched": f"{title} ({year})" if year else title}
    except Exception as e:  # noqa: BLE001 - the contract is "never raises"; the caller logs the error text
        return {"ok": False, "error": str(e)}
