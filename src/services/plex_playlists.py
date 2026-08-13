"""
Curatarr — "Curatarr Recommended" playlists inside Plex.

ALL Plex WRITE code lives in this one module (the rest of the codebase is
strictly read-only against Plex). Per user and per video category, a playlist
with the library-lane recommendations — the curator's picks appear where the
household actually watches, per-account private:

  Curatarr Recommended · Movies / · Shows / · Anime / · Music

Music (probe-verified 2026-08-13, scripts/dev/probe_plex_music_playlist.py):
artist recs resolve by name in the music sections, and an ALBUM ratingKey in
an audio-playlist uri expands to all its tracks — so the music playlist is
"one unheard album per recommended artist" (viewedLeafCount via the USER
token = their listen state).

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


async def list_playlists(token: str, playlist_type: str = "video") -> list[dict]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(f"{_base()}/playlists", headers=_headers(token),
                        params={"playlistType": playlist_type})
    if r.status_code != 200:
        raise RuntimeError(f"playlist list HTTP {r.status_code}")
    return (r.json().get("MediaContainer") or {}).get("Metadata") or []


async def list_video_playlists(token: str) -> list[dict]:
    return await list_playlists(token, "video")


async def delete_playlist(token: str, rating_key: str) -> None:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        await c.delete(f"{_base()}/playlists/{rating_key}", headers=_headers(token))


async def create_playlist(token: str, title: str, metadata_keys: list[str],
                          playlist_type: str = "video") -> Optional[dict]:
    """Create a dumb (non-smart) playlist from item ratingKeys. Returns the
    playlist metadata (ratingKey, leafCount) or None. For audio playlists an
    ALBUM key expands to all its tracks (probe-verified)."""
    mid = await get_machine_identifier()
    if not mid or not metadata_keys:
        return None
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        def params(m):
            uri = (f"server://{m}/com.plexapp.plugins.library"
                   f"/library/metadata/{','.join(metadata_keys)}")
            return {"type": playlist_type, "smart": "0",
                    "title": title, "uri": uri}
        r = await c.post(f"{_base()}/playlists", headers=_headers(token),
                         params=params(mid))
        if r.status_code == 400:
            # stale machineIdentifier after a server reinstall — refresh once
            mid = await get_machine_identifier(force=True)
            if mid:
                r = await c.post(f"{_base()}/playlists", headers=_headers(token),
                                 params=params(mid))
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


# ── MUSIC ─────────────────────────────────────────────────────────────────────

MUSIC_PLAYLIST_TITLE = "Curatarr Recommended · Music"
_MAX_MUSIC_ARTISTS = 5


async def resolve_artist_key(name: str) -> Optional[str]:
    """Artist name → Plex artist ratingKey via the music sections' title
    filter (owner token — the library is shared). Music rec rows carry no
    plex_rating_key (artists live outside MediaTechProfile), hence by-name.
    None = not in Plex (yet) — caller skips and logs."""
    token = _owner_token()
    if not token or not name:
        return None
    from src.database.connection import get_db_session
    from src.database.models import LibraryConfig
    with get_db_session() as db:
        sections = [lc.plex_section_key for lc in
                    db.query(LibraryConfig)
                    .filter(LibraryConfig.media_category == "music").all()]
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        for sec in sections:
            try:
                r = await c.get(f"{_base()}/library/sections/{sec}/all",
                                headers=_headers(token),
                                params={"type": "8", "title": name})
                meta = (r.json().get("MediaContainer") or {}).get("Metadata") or []
                if meta:
                    return str(meta[0].get("ratingKey"))
            except Exception as e:
                logger.debug("[playlists] artist lookup %r in %s failed: %s",
                             name, sec, e)
    return None


async def pick_album_key(user_token: str, artist_key: str,
                         check_first: int = 4) -> Optional[str]:
    """The artist's first UNHEARD album for this account (viewedLeafCount is
    absent from the children listing, so the first few albums get a single
    metadata GET each with the USER token = their listen state). Falls back
    to the plain first album."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        try:
            r = await c.get(f"{_base()}/library/metadata/{artist_key}/children",
                            headers=_headers(user_token))
            albums = (r.json().get("MediaContainer") or {}).get("Metadata") or []
        except Exception as e:
            logger.debug("[playlists] albums of %s failed: %s", artist_key, e)
            return None
        if not albums:
            return None
        for a in albums[:check_first]:
            key = str(a.get("ratingKey"))
            try:
                r = await c.get(f"{_base()}/library/metadata/{key}",
                                headers=_headers(user_token))
                m = ((r.json().get("MediaContainer") or {}).get("Metadata") or [{}])[0]
                if int(m.get("viewedLeafCount") or 0) == 0:
                    return key
            except Exception:
                continue
        return str(albums[0].get("ratingKey"))


async def push_user_music_playlist(user) -> dict:
    """One audio playlist per user: one unheard album for each of their top
    library-lane music recommendations (album keys expand to their tracks).
    Same freshness guard + find-delete-recreate as the video push."""
    from src.database.connection import get_db_session
    from src.database.models import CachedRecommendation

    if not user.plex_token:
        logger.info("[playlists] %s: no stored Plex token — music playlist "
                    "appears after their next login; skipped.", user.plex_username)
        return {"pushed": [], "skipped": "no_token"}

    with get_db_session() as db:
        rows = (db.query(CachedRecommendation)
                .filter(CachedRecommendation.user_id == user.id,
                        CachedRecommendation.category == "music",
                        CachedRecommendation.lane == "library")
                .order_by(CachedRecommendation.confidence.desc())
                .limit(_MAX_MUSIC_ARTISTS).all())
        artists = [{"title": r.title, "cached_at": r.cached_at} for r in rows]
    if not artists:
        return {"pushed": []}

    snap = _load_snapshot(user.id)
    prev = snap.get("music") or {}
    newest = max((a["cached_at"] for a in artists if a["cached_at"]), default=None)
    if prev.get("pushed_at"):
        try:
            pushed_dt = datetime.fromisoformat(prev["pushed_at"])
            fresh = newest is None or newest <= pushed_dt
            recent = (datetime.utcnow() - pushed_dt).total_seconds() < _REPUSH_HOURS * 3600
            if fresh and recent:
                return {"pushed": []}
        except Exception:
            pass

    keys, titles = [], []
    for a in artists:
        artist_key = await resolve_artist_key(a["title"])
        if not artist_key:
            logger.info("[playlists] %s/music: %r not found in Plex — skipped.",
                        user.plex_username, a["title"])
            continue
        album_key = await pick_album_key(user.plex_token, artist_key)
        if album_key:
            keys.append(album_key)
            titles.append(a["title"])
    if not keys:
        logger.info("[playlists] %s/music: no resolvable artists — skipped.",
                    user.plex_username)
        return {"pushed": []}

    for pl in await list_playlists(user.plex_token, "audio"):
        if pl.get("title") == MUSIC_PLAYLIST_TITLE:
            await delete_playlist(user.plex_token, str(pl.get("ratingKey")))
    meta = await create_playlist(user.plex_token, MUSIC_PLAYLIST_TITLE, keys,
                                 playlist_type="audio")
    snap["music"] = {"pushed_at": datetime.utcnow().isoformat(),
                     "playlist_key": (meta or {}).get("ratingKey"),
                     "titles": titles}
    _save_snapshot(user.id, snap)
    logger.info("[playlists] %s: pushed '%s' (%d albums, %s tracks)",
                user.plex_username, MUSIC_PLAYLIST_TITLE, len(keys),
                (meta or {}).get("leafCount"))
    return {"pushed": ["music"]}
