"""
Curatarr — "Curatarr Recommended" playlists inside Plex.

ALL Plex WRITE code lives in this one module (the rest of the codebase is
strictly read-only against Plex). Per user and per video category, a playlist
with the library-lane recommendations — the curator's picks appear where the
household actually watches, per-account private:

  Curatarr Recommended · Movies / · Shows / · Anime   (music excluded v1 —
  artist recs don't map to playable playlist items)

Mechanics, probe-verified against the live server (2026-08-16):
- playlists are ACCOUNT-private → created with the USER's own token
  (users.plex_token, captured at PIN login; NULL → user is skipped until
  their next login).
- create: POST /playlists?type=video&smart=0&title=…&uri=server://{machine
  Identifier}/com.plexapp.plugins.library/library/metadata/{k1,k2,…}
- a SERIES ratingKey in the uri expands to ALL its episodes (12/12 in the
  probe) → shows/anime are pushed as the user's FIRST UNWATCHED EPISODE
  (allLeaves?unwatched=1, container-size 1, user token — view state is
  per-account), falling back to the plain first leaf.
- duplicate create makes a SECOND same-title playlist → refresh is always
  find-by-title → delete → recreate.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0

PLAYLIST_TITLES = {
    "movie": "Curatarr Recommended · Movies",
    "show": "Curatarr Recommended · Shows",
    "anime": "Curatarr Recommended · Anime",
}

# How long a push stays fresh without new recs before we re-push anyway
# (safety net against manual playlist deletion / server restores).
_REPUSH_HOURS = 168


def _base() -> str:
    return str(settings.effective_plex_url).rstrip("/")


def _headers(token: str) -> dict:
    return {"Accept": "application/json", "X-Plex-Token": token}


def _owner_token() -> Optional[str]:
    tok = settings.effective_plex_token
    return tok.get_secret_value() if hasattr(tok, "get_secret_value") else tok


async def get_machine_identifier(force: bool = False) -> Optional[str]:
    """The server's machineIdentifier, cached in app_state (survives restarts).
    A server reinstall changes it — callers that get a 400 from playlist
    create should re-fetch once with force=True and retry."""
    from src.services.app_state import get_state, set_state
    if not force:
        cached = get_state("plex_machine_identifier")
        if cached:
            return cached
    token = _owner_token()
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_base()}/identity", headers=_headers(token))
        mid = (r.json().get("MediaContainer") or {}).get("machineIdentifier")
        if mid:
            set_state("plex_machine_identifier", mid)
        return mid
    except Exception as e:
        logger.warning("[playlists] /identity failed: %s", e)
        return None


async def list_video_playlists(token: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(f"{_base()}/playlists", headers=_headers(token),
                        params={"playlistType": "video"})
    if r.status_code != 200:
        raise RuntimeError(f"playlist list HTTP {r.status_code}")
    return (r.json().get("MediaContainer") or {}).get("Metadata") or []


async def delete_playlist(token: str, rating_key: str) -> None:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        await c.delete(f"{_base()}/playlists/{rating_key}", headers=_headers(token))


async def create_playlist(token: str, title: str, metadata_keys: list[str]) -> Optional[dict]:
    """Create a dumb (non-smart) playlist from item ratingKeys. Returns the
    playlist metadata (ratingKey, leafCount) or None."""
    mid = await get_machine_identifier()
    if not mid or not metadata_keys:
        return None
    uri = (f"server://{mid}/com.plexapp.plugins.library"
           f"/library/metadata/{','.join(metadata_keys)}")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(f"{_base()}/playlists", headers=_headers(token),
                         params={"type": "video", "smart": "0",
                                 "title": title, "uri": uri})
        if r.status_code == 400:
            # stale machineIdentifier after a server reinstall — refresh once
            mid = await get_machine_identifier(force=True)
            if mid:
                uri = (f"server://{mid}/com.plexapp.plugins.library"
                       f"/library/metadata/{','.join(metadata_keys)}")
                r = await c.post(f"{_base()}/playlists", headers=_headers(token),
                                 params={"type": "video", "smart": "0",
                                         "title": title, "uri": uri})
    if r.status_code != 200:
        raise RuntimeError(f"playlist create HTTP {r.status_code}")
    meta = (r.json().get("MediaContainer") or {}).get("Metadata") or [{}]
    return meta[0]


async def first_unwatched_episode_key(token: str, series_key: str) -> Optional[str]:
    """The user's next episode of a series (view state is per-account, hence
    the USER token). Falls back to the plain first leaf, then to None —
    a None drops the series from this push rather than exploding the
    playlist into every episode (probe: a series key expands to ALL leaves)."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        for params in ({"unwatched": 1}, {}):
            try:
                r = await c.get(
                    f"{_base()}/library/metadata/{series_key}/allLeaves",
                    headers={**_headers(token),
                             "X-Plex-Container-Start": "0",
                             "X-Plex-Container-Size": "1"},
                    params=params)
                leaves = (r.json().get("MediaContainer") or {}).get("Metadata") or []
                if leaves:
                    return str(leaves[0].get("ratingKey"))
            except Exception as e:
                logger.debug("[playlists] allLeaves(%s) failed: %s", series_key, e)
    return None


def _push_snapshot_key(user_id: int) -> str:
    return f"rec_playlist_push:user_id={user_id}"


def _load_snapshot(user_id: int) -> dict:
    from src.services.app_state import get_state
    try:
        return json.loads(get_state(_push_snapshot_key(user_id)) or "{}")
    except Exception:
        return {}


def _save_snapshot(user_id: int, snap: dict) -> None:
    from src.services.app_state import set_state
    set_state(_push_snapshot_key(user_id), json.dumps(snap))


async def push_user_playlists(user) -> dict:
    """Refresh one user's Curatarr-Recommended playlists from their cached
    LIBRARY-lane recommendations. Returns {"pushed": [cat…], "skipped": str?}.
    Raises on transient Plex errors so the custodian task stays due."""
    from src.database.connection import get_db_session
    from src.database.models import CachedRecommendation

    if not user.plex_token:
        logger.info("[playlists] %s: no stored Plex token — playlist appears "
                    "after their next login; skipped.", user.plex_username)
        return {"pushed": [], "skipped": "no_token"}

    snap = _load_snapshot(user.id)
    pushed = []
    for category, title in PLAYLIST_TITLES.items():
        with get_db_session() as db:
            rows = (db.query(CachedRecommendation)
                    .filter(CachedRecommendation.user_id == user.id,
                            CachedRecommendation.category == category,
                            CachedRecommendation.lane == "library")
                    .order_by(CachedRecommendation.confidence.desc())
                    .limit(10).all())
            rows = [{"title": r.title, "key": r.plex_rating_key,
                     "cached_at": r.cached_at} for r in rows]
        if not rows:
            continue

        # Push only when there is something new (or the safety window lapsed):
        # never pushed, recs newer than the last push, or push older than 7d.
        prev = snap.get(category) or {}
        newest = max((r["cached_at"] for r in rows if r["cached_at"]),
                     default=None)
        pushed_at = prev.get("pushed_at")
        if pushed_at:
            try:
                pushed_dt = datetime.fromisoformat(pushed_at)
                fresh = newest is None or newest <= pushed_dt
                recent = (datetime.utcnow() - pushed_dt).total_seconds() < _REPUSH_HOURS * 3600
                if fresh and recent:
                    continue
            except Exception:
                pass

        keys: list[str] = []
        titles: list[str] = []
        for r in rows:
            if not r["key"]:
                continue   # resolver couldn't place it — logged at cache time
            key = r["key"]
            if category in ("show", "anime"):
                # series keys expand to ALL episodes — push the user's next
                # unwatched episode instead (their token = their view state)
                key = await first_unwatched_episode_key(user.plex_token, key)
                if not key:
                    continue
            keys.append(key)
            titles.append(r["title"])
        if not keys:
            logger.info("[playlists] %s/%s: no resolvable items — skipped.",
                        user.plex_username, category)
            continue

        # find-delete-recreate: duplicate titles are allowed by Plex, so a
        # bare create would pile up copies week after week.
        for pl in await list_video_playlists(user.plex_token):
            if pl.get("title") == title:
                await delete_playlist(user.plex_token, str(pl.get("ratingKey")))
        meta = await create_playlist(user.plex_token, title, keys)
        leaf = (meta or {}).get("leafCount")
        if leaf is not None and int(leaf) < len(keys):
            # restricted library sharing can silently drop items for
            # non-owner accounts — surface it instead of wondering later
            logger.info("[playlists] %s/%s: %s/%d items visible to this "
                        "account (restricted sharing?)",
                        user.plex_username, category, leaf, len(keys))
        snap[category] = {"pushed_at": datetime.utcnow().isoformat(),
                          "playlist_key": (meta or {}).get("ratingKey"),
                          "titles": titles}
        pushed.append(category)
        logger.info("[playlists] %s: pushed '%s' (%d items)",
                    user.plex_username, title, len(keys))

    _save_snapshot(user.id, snap)
    return {"pushed": pushed}
