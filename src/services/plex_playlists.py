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
from src.database.runner import run_read, run_write
from src.services.enrichment_state import log_topup_failure
from src.services.http_client import tls_context

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
        cached = await run_read(get_state, "plex_machine_identifier")
        if cached:
            return cached
    token = _owner_token()
    if not token:
        return None
    try:
        async with httpx.AsyncClient(verify=tls_context(), timeout=_TIMEOUT) as c:
            r = await c.get(f"{_base()}/identity", headers=_headers(token))
        mid = (r.json().get("MediaContainer") or {}).get("machineIdentifier")
        if mid:
            await run_write(set_state, "plex_machine_identifier", mid)
        return mid
    except Exception as e:
        logger.warning("[playlists] /identity failed: %s", e)
        return None


async def list_playlists(token: str, playlist_type: str = "video") -> list[dict]:
    async with httpx.AsyncClient(verify=tls_context(), timeout=_TIMEOUT) as c:
        r = await c.get(f"{_base()}/playlists", headers=_headers(token),
                        params={"playlistType": playlist_type})
    if r.status_code != 200:
        raise RuntimeError(f"playlist list HTTP {r.status_code}")
    return (r.json().get("MediaContainer") or {}).get("Metadata") or []


async def list_video_playlists(token: str) -> list[dict]:
    return await list_playlists(token, "video")


async def delete_playlist(token: str, rating_key: str) -> None:
    async with httpx.AsyncClient(verify=tls_context(), timeout=_TIMEOUT) as c:
        await c.delete(f"{_base()}/playlists/{rating_key}", headers=_headers(token))


async def create_playlist(token: str, title: str, metadata_keys: list[str],
                          playlist_type: str = "video") -> Optional[dict]:
    """Create a dumb (non-smart) playlist from item ratingKeys. Returns the
    playlist metadata (ratingKey, leafCount) or None for no keys. For audio
    playlists an ALBUM key expands to all its tracks (probe-verified).
    Raises when the server identity is unknown: a None here used to be
    stamped as a successful push after the old playlist had been deleted."""
    if not metadata_keys:
        return None
    mid = await get_machine_identifier()
    if not mid:
        raise RuntimeError("Plex machineIdentifier unavailable")
    async with httpx.AsyncClient(verify=tls_context(), timeout=_TIMEOUT) as c:
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


async def playlist_items(token: str, playlist_key: str) -> list[dict]:
    """The playlist's current entries: [{item_id, rating_key}] —
    playlistItemID is what the remove endpoint wants, NOT the ratingKey."""
    async with httpx.AsyncClient(verify=tls_context(), timeout=_TIMEOUT) as c:
        r = await c.get(f"{_base()}/playlists/{playlist_key}/items",
                        headers=_headers(token))
    if r.status_code != 200:
        raise RuntimeError(f"playlist items HTTP {r.status_code}")
    return [{"item_id": str(m.get("playlistItemID")),
             "rating_key": str(m.get("ratingKey"))}
            for m in ((r.json().get("MediaContainer") or {}).get("Metadata") or [])]


async def reconcile_playlist(token: str, playlist_key: str,
                             wanted_keys: list[str]) -> bool:
    """Edit the playlist IN PLACE (delta add/remove) so its identity —
    ratingKey, pins, custom art — survives the weekly refresh (ported from
    SoulSync's reconcile mode #792, MIT; our old find-delete-recreate broke
    every client-side pin weekly). Order note: existing items keep their
    position, new ones append — set-correctness over ranking-order, by
    design. Returns False on any error; the caller falls back to the
    destructive recreate exactly once."""
    try:
        current = await playlist_items(token, playlist_key)
        have = {it["rating_key"] for it in current}
        want = set(wanted_keys)
        async with httpx.AsyncClient(verify=tls_context(), timeout=_TIMEOUT) as c:
            for it in current:
                if it["rating_key"] not in want:
                    await c.delete(
                        f"{_base()}/playlists/{playlist_key}/items/{it['item_id']}",
                        headers=_headers(token))
            to_add = [k for k in wanted_keys if k not in have]
            if to_add:
                mid = await get_machine_identifier()
                if not mid:
                    return False
                uri = (f"server://{mid}/com.plexapp.plugins.library"
                       f"/library/metadata/{','.join(to_add)}")
                r = await c.put(f"{_base()}/playlists/{playlist_key}/items",
                                headers=_headers(token), params={"uri": uri})
                if r.status_code not in (200, 201):
                    return False
        return True
    except Exception as e:
        logger.warning("[playlists] reconcile failed for %s: %s", playlist_key, e)
        return False


async def key_exists(token: str, rating_key: str) -> bool:
    """One cheap metadata GET — the stale-key probe for the self-heal.

    Only a 404 means "stale". Any other failure raises: it used to return
    False, so with Plex down every item looked stale, the push came back
    {"pushed": []} and the custodian stamped the task done for a week.
    """
    async with httpx.AsyncClient(verify=tls_context(), timeout=_TIMEOUT) as c:
        r = await c.get(f"{_base()}/library/metadata/{rating_key}",
                        headers=_headers(token))
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    raise RuntimeError(f"Plex metadata probe HTTP {r.status_code}")


async def _search_sections(token: str, sections: list, params: dict, what: str) -> Optional[str]:
    """First ratingKey a title search finds across ``sections``, or None.

    A section that fails is skipped, but if NO section answered with a hit
    and one of them failed, the failure is raised: "Plex unreachable" is not
    "not in Plex" (the old lookups returned None for both, at DEBUG).
    """
    err: Optional[Exception] = None
    async with httpx.AsyncClient(verify=tls_context(), timeout=_TIMEOUT) as c:
        for sec in sections:
            try:
                r = await c.get(f"{_base()}/library/sections/{sec}/all",
                                headers=_headers(token), params=params)
                if r.status_code != 200:
                    raise RuntimeError(f"section {sec} search HTTP {r.status_code}")
                meta = (r.json().get("MediaContainer") or {}).get("Metadata") or []
                if meta:
                    return str(meta[0].get("ratingKey"))
            except Exception as e:  # noqa: BLE001 - kept in err and re-raised below when no section matched
                logger.debug("[playlists] %s lookup in %s failed: %s", what, sec, e)
                err = e
    if err is not None:
        raise err
    return None


_VIDEO_TYPE = {"movie": "1", "show": "2", "anime": "2"}


def _section_keys(category: str) -> list[str]:
    """The Plex section keys configured for one media category."""
    from src.database.connection import get_db_session
    from src.database.models import LibraryConfig
    with get_db_session() as db:
        return [r[0] for r in
                db.query(LibraryConfig.plex_section_key)
                .filter(LibraryConfig.media_category == category).all()]


async def resolve_video_key(title: str, category: str, sections: Optional[list[str]] = None) -> Optional[str]:
    """Title → ratingKey via the category's sections (owner token — the
    video mirror of resolve_artist_key). The self-heal for keys Plex
    re-issued on a metadata refresh/optimize (SoulSync rescue pattern)."""
    token = _owner_token()
    if not token or not title:
        return None

    if sections is None:
        sections = await run_read(_section_keys, category)

    t = _VIDEO_TYPE.get(category, "1")
    return await _search_sections(token, sections, {"type": t, "title": title},
                                  f"video {title!r}")


def _heal_rec_key(user_id: int, category: str, title: str, new_key: str) -> None:
    """Persist a re-resolved ratingKey onto the cached rec row so the next
    push skips the live search (SoulSync heal-the-stored-id pattern)."""
    try:
        from src.database.connection import get_db_session
        from src.database.models import CachedRecommendation
        with get_db_session() as db:
            db.query(CachedRecommendation).filter(
                CachedRecommendation.user_id == user_id,
                CachedRecommendation.category == category,
                CachedRecommendation.lane == "library",
                CachedRecommendation.title == title,
            ).update({"plex_rating_key": new_key}, synchronize_session=False)
            db.commit()
    except Exception as e:
        logger.warning("[playlists] key heal failed for %r: %s", title, e)


async def first_unwatched_episode_key(token: str, series_key: str) -> Optional[str]:
    """The user's next episode of a series (view state is per-account, hence
    the USER token). Falls back to the plain first leaf, then to None —
    a None drops the series from this push rather than exploding the
    playlist into every episode (probe: a series key expands to ALL leaves)."""
    async with httpx.AsyncClient(verify=tls_context(), timeout=_TIMEOUT) as c:
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
                log_topup_failure(logger, "[playlists] allLeaves", series_key, e)
    return None


def _push_snapshot_key(user_id: int) -> str:
    return f"rec_playlist_push:user_id={user_id}"


def _load_snapshot(user_id: int) -> dict:
    from src.services.app_state import get_state
    try:
        snap = json.loads(get_state(_push_snapshot_key(user_id)) or "{}")
        if not isinstance(snap, dict):
            raise ValueError(f"not an object: {type(snap).__name__}")
        return snap
    except Exception as e:
        # {} only costs a re-push the freshness guard would have skipped
        logger.warning("[playlists] push snapshot for user %d unreadable - "
                       "re-pushing: %s", user_id, e)
        return {}


def _save_snapshot(user_id: int, snap: dict) -> None:
    from src.services.app_state import set_state
    set_state(_push_snapshot_key(user_id), json.dumps(snap))


def _library_recs(user_id: int, category: str, limit: int) -> list[dict]:
    """The user's top library-lane recs for one category, as plain dicts."""
    from src.database.connection import get_db_session
    from src.database.models import CachedRecommendation
    with get_db_session() as db:
        rows = (db.query(CachedRecommendation)
                .filter(CachedRecommendation.user_id == user_id,
                        CachedRecommendation.category == category,
                        CachedRecommendation.lane == "library")
                .order_by(CachedRecommendation.confidence.desc())
                .limit(limit).all())
        return [{"title": r.title, "key": r.plex_rating_key,
                 "cached_at": r.cached_at} for r in rows]


async def push_user_playlists(user) -> dict:
    """Refresh one user's Curatarr-Recommended playlists from their cached
    LIBRARY-lane recommendations. Returns {"pushed": [cat…], "skipped": str?}.
    Raises on transient Plex errors so the custodian task stays due."""
    from src.services.media_server import active_kind
    if active_kind() == "jellyfin":
        from src.services.media_server import jellyfin_writes
        return await jellyfin_writes.push_user_playlists(user)
    if not user.plex_token:
        logger.info("[playlists] %s: no stored Plex token — playlist appears "
                    "after their next login; skipped.", user.plex_username)
        return {"pushed": [], "skipped": "no_token"}

    snap = await run_read(_load_snapshot, user.id)
    pushed = []
    for category, title in PLAYLIST_TITLES.items():
        rows = await run_read(_library_recs, user.id, category, 10)
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
            except (ValueError, TypeError):
                pass

        video_sections = await run_read(_section_keys, category)

        keys: list[str] = []
        titles: list[str] = []
        for r in rows:
            if not r["key"]:
                continue   # resolver couldn't place it — logged at cache time
            key = r["key"]
            # Stale-key self-heal (SoulSync rescue pattern): Plex re-keys
            # items on a metadata refresh/optimize, and the cached key then
            # 404s — the item silently vanished from the push. Probe (≤10
            # cheap GETs per push) and re-resolve by title, healing the
            # cached row so the next push is fast again.
            if not await key_exists(_owner_token() or user.plex_token, key):
                healed = await resolve_video_key(r["title"], category, sections=video_sections)
                if healed:
                    logger.info("[playlists] %r: stale key %s → %s (healed)",
                                r["title"], key, healed)
                    await run_write(_heal_rec_key, user.id, category, r["title"], healed)
                    key = healed
                else:
                    logger.info("[playlists] %r: key %s stale, no re-resolve "
                                "— skipped this push", r["title"], key)
                    continue
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

        # Reconcile-first (SoulSync #792): keep the playlist OBJECT and edit
        # the delta — its identity (ratingKey, client pins, custom art)
        # survives the weekly refresh. Extra same-title copies from the old
        # delete+recreate era are removed; a failed reconcile falls back to
        # the destructive recreate exactly once, loudly.
        # Server identity first: without it reconcile fails, the fallback
        # deletes the playlist and the recreate cannot run.
        if not await get_machine_identifier():
            raise RuntimeError("Plex machineIdentifier unavailable")
        existing = [pl for pl in await list_video_playlists(user.plex_token)
                    if pl.get("title") == title]
        meta = None
        if existing:
            keep_key = str(existing[0].get("ratingKey"))
            for pl in existing[1:]:
                await delete_playlist(user.plex_token, str(pl.get("ratingKey")))
            if await reconcile_playlist(user.plex_token, keep_key, keys):
                meta = {"ratingKey": keep_key, "leafCount": len(keys)}
                try:
                    meta["leafCount"] = len(
                        await playlist_items(user.plex_token, keep_key))
                except Exception:  # noqa: BLE001 - count only; falls back to the pushed key count
                    pass
            else:
                logger.warning("[playlists] reconcile failed for '%s' — "
                               "recreating this once", title)
                await delete_playlist(user.plex_token, keep_key)
        if meta is None:
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

    await run_write(_save_snapshot, user.id, snap)
    return {"pushed": pushed}


# ── MUSIC ─────────────────────────────────────────────────────────────────────

MUSIC_PLAYLIST_TITLE = "Curatarr Recommended · Music"
_MAX_MUSIC_ARTISTS = 5


async def resolve_artist_key(name: str, sections: Optional[list[str]] = None) -> Optional[str]:
    """Artist name → Plex artist ratingKey via the music sections' title
    filter (owner token — the library is shared). Music rec rows carry no
    plex_rating_key (artists live outside MediaTechProfile), hence by-name.
    None = not in Plex (yet) — caller skips and logs."""
    token = _owner_token()
    if not token or not name:
        return None

    if sections is None:
        sections = await run_read(_section_keys, "music")

    return await _search_sections(token, sections, {"type": "8", "title": name},
                                  f"artist {name!r}")


async def pick_album_key(user_token: str, artist_key: str,
                         check_first: int = 4) -> Optional[str]:
    """The artist's first UNHEARD album for this account (viewedLeafCount is
    absent from the children listing, so the first few albums get a single
    metadata GET each with the USER token = their listen state). Falls back
    to the plain first album."""
    async with httpx.AsyncClient(verify=tls_context(), timeout=_TIMEOUT) as c:
        try:
            r = await c.get(f"{_base()}/library/metadata/{artist_key}/children",
                            headers=_headers(user_token))
            albums = (r.json().get("MediaContainer") or {}).get("Metadata") or []
        except Exception as e:
            log_topup_failure(logger, "[playlists] albums", artist_key, e)
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
            except Exception as e:
                log_topup_failure(logger, "[playlists] album", key, e)
                continue
        return str(albums[0].get("ratingKey"))


async def push_user_music_playlist(user) -> dict:
    """One audio playlist per user: one unheard album for each of their top
    library-lane music recommendations (album keys expand to their tracks).
    Same freshness guard as the video push, but DELIBERATELY still
    find-delete-recreate: we push ALBUM keys and Plex stores TRACK items,
    so a set-delta reconcile would compare tracks against albums and
    remove/re-add everything anyway — identity preservation buys nothing
    here."""
    from src.services.media_server import active_kind
    if active_kind() == "jellyfin":
        from src.services.media_server import jellyfin_writes
        return await jellyfin_writes.push_user_music_playlist(user)
    if not user.plex_token:
        logger.info("[playlists] %s: no stored Plex token — music playlist "
                    "appears after their next login; skipped.", user.plex_username)
        return {"pushed": [], "skipped": "no_token"}

    artists = [{"title": r["title"], "cached_at": r["cached_at"]} for r in
               await run_read(_library_recs, user.id, "music", _MAX_MUSIC_ARTISTS)]
    if not artists:
        return {"pushed": []}

    snap = await run_read(_load_snapshot, user.id)
    prev = snap.get("music") or {}
    newest = max((a["cached_at"] for a in artists if a["cached_at"]), default=None)
    if prev.get("pushed_at"):
        try:
            pushed_dt = datetime.fromisoformat(prev["pushed_at"])
            fresh = newest is None or newest <= pushed_dt
            recent = (datetime.utcnow() - pushed_dt).total_seconds() < _REPUSH_HOURS * 3600
            if fresh and recent:
                return {"pushed": []}
        except (ValueError, TypeError):
            pass

    music_sections = await run_read(_section_keys, "music")

    keys, titles = [], []
    for a in artists:
        artist_key = await resolve_artist_key(a["title"], sections=music_sections)
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

    # Identity before the delete: a create that cannot run must not follow it.
    if not await get_machine_identifier():
        raise RuntimeError("Plex machineIdentifier unavailable")
    for pl in await list_playlists(user.plex_token, "audio"):
        if pl.get("title") == MUSIC_PLAYLIST_TITLE:
            await delete_playlist(user.plex_token, str(pl.get("ratingKey")))
    meta = await create_playlist(user.plex_token, MUSIC_PLAYLIST_TITLE, keys,
                                 playlist_type="audio")
    snap["music"] = {"pushed_at": datetime.utcnow().isoformat(),
                     "playlist_key": (meta or {}).get("ratingKey"),
                     "titles": titles}
    await run_write(_save_snapshot, user.id, snap)
    logger.info("[playlists] %s: pushed '%s' (%d albums, %s tracks)",
                user.plex_username, MUSIC_PLAYLIST_TITLE, len(keys),
                (meta or {}).get("leafCount"))
    return {"pushed": ["music"]}


# ── WRAP-UP ───────────────────────────────────────────────────────────────────
# "Your period on Plex" (PlayStation Wrap-Up's playlist, everyone's top-songs
# list): the titles a wrap-up is about, one tap from the page. Video: a film
# is its own key, a series its user's next unwatched episode (a series key
# expands to every episode). Music: the top tracks, matched by title AND
# artist so a common track name never lands another artist's song.

WRAPUP_PLAYLIST_PREFIX = "Curatarr Wrap-Up · "
_MAX_WRAPUP_ITEMS = 12


async def resolve_track_key(track: str, artist: str, sections: Optional[list[str]] = None) -> Optional[str]:
    token = _owner_token()
    if not token or not track:
        return None
    if sections is None:
        sections = await run_read(_section_keys, "music")
    want = (artist or "").casefold()
    async with httpx.AsyncClient(verify=tls_context(), timeout=_TIMEOUT) as c:
        for sec in sections:
            r = await c.get(f"{_base()}/library/sections/{sec}/all", headers=_headers(token),
                            params={"type": "10", "title": track})
            if r.status_code != 200:
                raise RuntimeError(f"section {sec} track search HTTP {r.status_code}")
            for m in (r.json().get("MediaContainer") or {}).get("Metadata") or []:
                if not want or (m.get("grandparentTitle") or "").casefold() == want:
                    return str(m.get("ratingKey"))
    return None


async def push_wrapup_playlist(user, label: str, videos: list[dict], tracks: list[dict]) -> dict:
    """Create (or replace) the user's wrap-up playlist(s). ``videos``:
    [{title, media_type}], ``tracks``: [{track, artist}]. Returns
    {"pushed": [{title, items}], "missing": [title…]} or {"skipped": reason}."""
    from src.services.media_server import active_kind
    if active_kind() == "jellyfin":
        from src.services.media_server import jellyfin_writes
        return await jellyfin_writes.push_wrapup_playlist(user, label, videos, tracks)
    if not user.plex_token:
        return {"skipped": "no_token"}
    if not await get_machine_identifier():
        raise RuntimeError("Plex machineIdentifier unavailable")
    title = WRAPUP_PLAYLIST_PREFIX + label
    pushed, missing = [], []

    video_keys = []
    sections = {}
    for v in videos[:_MAX_WRAPUP_ITEMS]:
        cat = v.get("media_type")
        if cat not in _VIDEO_TYPE:
            continue
        if cat not in sections:
            sections[cat] = await run_read(_section_keys, cat)
        key = await resolve_video_key(v["title"], cat, sections=sections[cat])
        if key and cat in ("show", "anime"):
            key = await first_unwatched_episode_key(user.plex_token, key)
        if key:
            video_keys.append(key)
        else:
            missing.append(v["title"])
    track_keys = []
    music_sections = await run_read(_section_keys, "music") if tracks else []
    for t in tracks[:_MAX_WRAPUP_ITEMS]:
        key = await resolve_track_key(t["track"], t.get("artist"), sections=music_sections)
        if key:
            track_keys.append(key)
        else:
            missing.append(t["track"])

    for kind, keys, suffix in (("video", video_keys, ""), ("audio", track_keys, " · Music")):
        if not keys:
            continue
        name = title + (suffix if video_keys and track_keys else "")
        for pl in await list_playlists(user.plex_token, kind):
            if pl.get("title") == name:
                await delete_playlist(user.plex_token, str(pl.get("ratingKey")))
        meta = await create_playlist(user.plex_token, name, keys, playlist_type=kind)
        pushed.append({"title": name, "items": int((meta or {}).get("leafCount") or len(keys))})
        logger.info("[playlists] %s: pushed wrap-up '%s' (%d items)", user.plex_username, name, len(keys))
    return {"pushed": pushed, "missing": missing}
