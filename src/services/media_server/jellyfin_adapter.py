"""Jellyfin behind the media-server seam (docs/JELLYFIN_DESIGN.md, phase 1b).

The READ path for Jellyfin 10.9+ (design D4): server identity, libraries,
items with every identity a rebind needs, users, and what plays right now.
One API key authenticates every call, in the ``Authorization: MediaBrowser``
header; no per-user token and no 10.8 route (``/Users/{id}/Items``) is used.

Item ids leave this module in one spelling only, 32 lower-case hex
(``normalize_jellyfin_id``), whatever the payload carried.

Every read raises on a transport or HTTP failure: "Jellyfin did not answer"
must never read as "the library is empty" (design §6.2).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import AsyncIterator, Optional, Sequence

import httpx

from src.services.http_client import tls_context
from src.services.media_server.base import (
    Capabilities, ExternalIds, FileRef, ItemKind, Library, LiveSession, MediaItem, Play,
    ServerInfo, ServerUser, UserItemState, is_jellyfin_key, normalize_jellyfin_id,
)

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
_PAGE = 500
_FIELDS = "ProviderIds,Genres,DateCreated,MediaSources,Path,ParentId"
_TICKS_PER_MS = 10_000

_CLIENT_VERSION = "1"
_LIBRARY_KIND = {"movies": "movie", "tvshows": "show", "music": "music"}
_ITEM_TYPE = {"movie": "Movie", "series": "Series", "season": "Season", "episode": "Episode",
              "album": "MusicAlbum", "track": "Audio"}     # artists: /Artists/AlbumArtists
_KIND_OF_TYPE = {"Movie": "movie", "Series": "series", "Season": "season", "Episode": "episode",
                 "MusicArtist": "artist", "MusicAlbum": "album", "Audio": "track"}
_DEFAULT_KINDS = {"movie": ("movie",), "show": ("series", "season", "episode"),
                  "music": ("artist", "album", "track")}

_CAPABILITIES = Capabilities(
    per_play_history=False, now_playing=True, star_ratings=False,
    native_watchlist=False, music_moods=False, per_user_tokens=False,
)


class JellyfinError(RuntimeError):
    """Jellyfin did not answer, or answered with an error."""


class PluginMissing(JellyfinError):
    """The optional plugin a call needs is not installed (HTTP 404)."""


# What a play is on Jellyfin: films, episodes and tracks. The played state of
# a series or an album is derived from these, never counted itself.
_PLAYABLE = "Movie,Episode,Audio"
# The Playback Reporting plugin's own table (jellyfin-plugin-playbackreporting).
# A fixed string: nothing a caller passes ever reaches the query (design §4.4).
_REPORT_QUERY = ("SELECT DateCreated, UserId, ItemId, ItemType, ItemName, PlaybackMethod, "
                 "ClientName, DeviceName, PlayDuration FROM PlaybackActivity ORDER BY DateCreated")


def to_user_state(user_key: str, item: dict) -> Optional[UserItemState]:
    key = normalize_jellyfin_id(item.get("Id"))
    ud = item.get("UserData") or {}
    if not key:
        return None
    likes = ud.get("Likes")
    return UserItemState(
        user_key=user_key, item_key=key, played=bool(ud.get("Played")),
        play_count=_int(ud.get("PlayCount")) or 0, last_played_at=_ts(ud.get("LastPlayedDate")),
        position_ms=_ms(ud.get("PlaybackPositionTicks")),
        liked=None if likes is None else bool(likes), favorite=bool(ud.get("IsFavorite")))


def _int(value) -> Optional[int]:
    try:
        return int(value) if value is not None and str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _ts(value) -> Optional[datetime]:
    """Jellyfin's ISO dates carry seven fractional digits and a Z
    ("2023-11-14T22:13:20.0000000Z"); naive UTC like the rest of the DB."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip().rstrip("Z")
    head, _, frac = s.partition(".")
    try:
        dt = datetime.fromisoformat(head)
    except ValueError:
        return None
    if frac:
        digits = "".join(ch for ch in frac if ch.isdigit())[:6].ljust(6, "0")
        dt = dt.replace(microsecond=int(digits))
    return dt.replace(tzinfo=None)


def _ms(ticks) -> Optional[int]:
    t = _int(ticks)
    return t // _TICKS_PER_MS if t is not None else None


def _external_ids(kind: str, item: dict) -> ExternalIds:
    """ProviderIds → ExternalIds. On an episode Tvdb is the EPISODE's id.
    MusicBrainzTrack is the tag Picard writes as "MusicBrainz Track Id",
    which holds the RECORDING id."""
    p = item.get("ProviderIds") or {}
    tvdb = _int(p.get("Tvdb"))
    return ExternalIds(
        tmdb_id=_int(p.get("Tmdb")),
        tvdb_id=None if kind == "episode" else tvdb,
        tvdb_episode_id=tvdb if kind == "episode" else None,
        imdb_id=(p.get("Imdb") or None),
        anilist_id=_int(p.get("AniList")), anidb_id=_int(p.get("AniDB")),
        mb_artist_id=(p.get("MusicBrainzArtist") or None) if kind == "artist" else None,
        mb_release_group_id=(p.get("MusicBrainzReleaseGroup") or None) if kind == "album" else None,
        mb_release_id=(p.get("MusicBrainzAlbum") or None) if kind in ("album", "track") else None,
        mb_recording_id=(p.get("MusicBrainzTrack") or None) if kind == "track" else None,
    )


def _first_artist(item: dict) -> tuple:
    arts = item.get("AlbumArtists") or item.get("ArtistItems") or []
    if not arts:
        return None, item.get("AlbumArtist")
    return normalize_jellyfin_id(arts[0].get("Id")), arts[0].get("Name") or item.get("AlbumArtist")


def to_media_item(item: dict, library_key: Optional[str] = None) -> Optional[MediaItem]:
    """One BaseItemDto as a MediaItem; None for types we do not model."""
    kind = _KIND_OF_TYPE.get(item.get("Type") or "")
    key = normalize_jellyfin_id(item.get("Id"))
    if not kind or not key:
        return None
    # Only real media files: a series' or season's Path is a folder, and
    # Plex reports none for them either, so both sides stay comparable.
    files = tuple(FileRef(path=str(s["Path"]), size_bytes=_int(s.get("Size")))
                  for s in item.get("MediaSources") or [] if s.get("Path"))
    parent = grandparent = parent_title = grandparent_title = None
    season = episode = episode_end = disc = track = None
    if kind == "season":
        season = _int(item.get("IndexNumber"))
        parent, parent_title = normalize_jellyfin_id(item.get("SeriesId")), item.get("SeriesName")
    elif kind == "episode":
        season, episode = _int(item.get("ParentIndexNumber")), _int(item.get("IndexNumber"))
        episode_end = _int(item.get("IndexNumberEnd"))
        parent, parent_title = normalize_jellyfin_id(item.get("SeasonId")), item.get("SeasonName")
        grandparent, grandparent_title = normalize_jellyfin_id(item.get("SeriesId")), item.get("SeriesName")
    elif kind == "album":
        parent, parent_title = _first_artist(item)
    elif kind == "track":
        disc, track = _int(item.get("ParentIndexNumber")), _int(item.get("IndexNumber"))
        parent, parent_title = normalize_jellyfin_id(item.get("AlbumId")), item.get("Album")
        grandparent, grandparent_title = _first_artist(item)
    return MediaItem(
        key=key, kind=kind, title=str(item.get("Name") or ""), library_key=library_key,
        year=_int(item.get("ProductionYear")),
        parent_key=parent, grandparent_key=grandparent,
        parent_title=parent_title, grandparent_title=grandparent_title,
        season_number=season, episode_number=episode,
        episode_number_end=episode_end if episode_end and episode and episode_end > episode else None,
        disc_number=disc, track_number=track,
        external_ids=_external_ids(kind, item), files=files,
        genres=tuple(str(g) for g in item.get("Genres") or [] if g),
        added_at=_ts(item.get("DateCreated")), duration_ms=_ms(item.get("RunTimeTicks")),
    )


class JellyfinAdapter:
    kind = "jellyfin"

    def __init__(self, base_url: str, api_key: str, *,
                 transport: Optional[httpx.AsyncBaseTransport] = None,
                 device_id: str = "curatarr"):
        self._base = str(base_url).rstrip("/")
        self._key = api_key
        self._transport = transport          # tests pass an httpx.MockTransport
        self._device_id = device_id
        self._server_id: Optional[str] = None

    # ── plumbing ────────────────────────────────────────────────────────────
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(verify=tls_context(), timeout=_TIMEOUT, transport=self._transport)

    def _headers(self) -> dict:
        auth = (f'MediaBrowser Client="Curatarr", Device="Curatarr", '
                f'DeviceId="{self._device_id}", Version="{_CLIENT_VERSION}", Token="{self._key}"')
        return {"Accept": "application/json", "Authorization": auth}

    async def _get(self, client: httpx.AsyncClient, path: str, *,
                   params: Optional[dict] = None):
        return await self._send(client, "GET", path, params=params)

    async def _send(self, client: httpx.AsyncClient, method: str, path: str, *,
                    params: Optional[dict] = None, json=None):
        try:
            r = await client.request(method, f"{self._base}{path}", params=params, json=json,
                                     headers=self._headers())
        except httpx.HTTPError as e:
            raise JellyfinError(f"Jellyfin {path}: {type(e).__name__}") from e
        if r.status_code == 404 and path.startswith("/user_usage_stats/"):
            raise PluginMissing(f"Jellyfin {path}: the Playback Reporting plugin is not installed")
        if r.status_code != 200:
            raise JellyfinError(f"Jellyfin {path}: HTTP {r.status_code}")
        return r.json()

    async def _pages(self, path: str, params: dict) -> AsyncIterator[dict]:
        start = 0
        async with self._client() as c:
            while True:
                body = await self._get(c, path, params={**params, "StartIndex": start, "Limit": _PAGE})
                page = body.get("Items") or []
                for raw in page:
                    yield raw
                start += len(page)
                total = _int(body.get("TotalRecordCount"))
                if not page or (total is not None and start >= total) or len(page) < _PAGE:
                    break

    # ── facts ───────────────────────────────────────────────────────────────
    def capabilities(self) -> Capabilities:
        return _CAPABILITIES

    def is_server_key(self, value) -> bool:
        return is_jellyfin_key(value)

    def deep_link(self, key: str) -> Optional[str]:
        if not is_jellyfin_key(key):
            return None
        server = f"&serverId={self._server_id}" if self._server_id else ""
        return f"{self._base}/web/#/details?id={key}{server}"

    async def ping(self) -> ServerInfo:
        async with self._client() as c:
            info = await self._get(c, "/System/Info")
        self._server_id = normalize_jellyfin_id(info.get("Id")) or None
        return ServerInfo(kind="jellyfin", name=str(info.get("ServerName") or ""),
                          version=str(info.get("Version") or ""), server_id=self._server_id or "")

    # ── library ─────────────────────────────────────────────────────────────
    async def libraries(self) -> list[Library]:
        """Movie, show and music libraries. A mixed library has no
        CollectionType and is skipped, like Plex's "other" sections."""
        async with self._client() as c:
            folders = await self._get(c, "/Library/VirtualFolders")
        out = []
        for f in folders or []:
            kind = _LIBRARY_KIND.get(f.get("CollectionType") or "")
            key = normalize_jellyfin_id(f.get("ItemId"))
            if kind and key:
                out.append(Library(key=key, title=str(f.get("Name") or ""), kind=kind,
                                   roots=tuple(str(p) for p in f.get("Locations") or [] if p)))
        return out

    async def items(self, library: Library, *, kinds: Sequence[ItemKind] = (),
                    since: Optional[datetime] = None) -> AsyncIterator[MediaItem]:
        """Every item of ``kinds`` in ``library``, paged. Artists come from
        /Artists/AlbumArtists: /Items lists only artists that own a folder.
        ``since`` filters on the added date after the fetch."""
        for kind in kinds or _DEFAULT_KINDS.get(library.kind, ()):
            if kind == "artist":
                pages = self._pages("/Artists/AlbumArtists",
                                    {"ParentId": library.key, "Fields": _FIELDS})
            elif kind in _ITEM_TYPE:
                pages = self._pages("/Items", {"ParentId": library.key, "Recursive": "true",
                                               "IncludeItemTypes": _ITEM_TYPE[kind],
                                               "Fields": _FIELDS})
            else:
                continue
            async for raw in pages:
                it = to_media_item(raw, library.key)
                if it is None or (since and it.added_at and it.added_at < since):
                    continue
                yield it

    async def count_items(self, library: Library) -> int:
        """How many top-level titles a library holds (films, series, album
        artists) - the number the library mapping shows. One request, no
        items (``Limit=0``)."""
        if library.kind == "music":
            path, params = "/Artists/AlbumArtists", {"ParentId": library.key}
        else:
            path = "/Items"
            params = {"ParentId": library.key, "Recursive": "true",
                      "IncludeItemTypes": "Movie" if library.kind == "movie" else "Series"}
        async with self._client() as c:
            body = await self._get(c, path, params={**params, "Limit": 0})
        return _int(body.get("TotalRecordCount")) or 0

    async def _by_ids(self, keys: Sequence[str]) -> list[dict]:
        async with self._client() as c:
            body = await self._get(c, "/Items", params={"Ids": ",".join(keys), "Fields": _FIELDS})
        return body.get("Items") or []

    async def item(self, key: str) -> Optional[MediaItem]:
        """By ``/Items?Ids=``: it answers with the API key alone, where
        ``/Items/{id}`` wants a user on some 10.9 builds."""
        k = normalize_jellyfin_id(key)
        if not k:
            return None
        found = await self._by_ids([k])
        return to_media_item(found[0]) if found else None

    async def children(self, key: str) -> list[MediaItem]:
        k = normalize_jellyfin_id(key)
        if not k:
            return []
        async with self._client() as c:
            body = await self._get(c, "/Items", params={"ParentId": k, "Fields": _FIELDS})
        return [it for it in (to_media_item(m) for m in body.get("Items") or []) if it]

    async def key_exists(self, key: str) -> bool:
        k = normalize_jellyfin_id(key)
        if not k:
            return False
        return bool(await self._by_ids([k]))

    # ── people ──────────────────────────────────────────────────────────────
    async def users(self) -> list[ServerUser]:
        """Every account on the server, disabled ones included: a disabled
        member's plays still belong to them. Jellyfin users carry no e-mail."""
        async with self._client() as c:
            users = await self._get(c, "/Users")
        out = []
        for u in users or []:
            key, name = normalize_jellyfin_id(u.get("Id")), str(u.get("Name") or "").strip()
            if key and name:
                out.append(ServerUser(key=key, name=name,
                                      is_admin=bool((u.get("Policy") or {}).get("IsAdministrator"))))
        return out

    async def live_sessions(self) -> list[LiveSession]:
        """What plays right now (history tier A reads this). Sessions with
        nothing playing are skipped."""
        async with self._client() as c:
            sessions = await self._get(c, "/Sessions")
        out = []
        for s in sessions or []:
            item = s.get("NowPlayingItem") or {}
            sk, uk, ik = (str(s.get("Id") or ""), normalize_jellyfin_id(s.get("UserId")),
                          normalize_jellyfin_id(item.get("Id")))
            if not (sk and uk and ik):
                continue
            ps = s.get("PlayState") or {}
            out.append(LiveSession(
                session_key=sk, user_key=uk, item_key=ik,
                position_ms=_ms(ps.get("PositionTicks")) or 0,
                duration_ms=_ms(item.get("RunTimeTicks")), paused=bool(ps.get("IsPaused")),
                device=s.get("DeviceName") or None, client=s.get("Client") or None))
        return out

    # ── what people played (design §4) ──────────────────────────────────────
    async def user_states(self, user: ServerUser, *,
                          since: Optional[datetime] = None) -> list[UserItemState]:
        """Every film, episode and track ``user`` has played, newest first, with
        play count and last-played date (history tier C). With ``since`` the
        walk stops at the first item last played before it."""
        out = []
        params = {"userId": user.key, "Recursive": "true", "Filters": "IsPlayed",
                  "IncludeItemTypes": _PLAYABLE, "SortBy": "DatePlayed", "SortOrder": "Descending",
                  "Fields": "ParentId", "EnableUserData": "true"}
        pages = self._pages("/Items", params)
        try:
            async for raw in pages:
                st = to_user_state(user.key, raw)
                if st is None:
                    continue
                if since and st.last_played_at and st.last_played_at < since:
                    break
                out.append(st)
        finally:
            await pages.aclose()         # an early stop closes the client now, not at GC
        return out

    async def user_state(self, user_key: str, item_key: str) -> Optional[UserItemState]:
        """One user's state of one item - the webhook's re-read (tier B)."""
        u, i = normalize_jellyfin_id(user_key), normalize_jellyfin_id(item_key)
        if not (u and i):
            return None
        async with self._client() as c:
            body = await self._get(c, "/Items", params={"userId": u, "Ids": i,
                                                        "EnableUserData": "true"})
        found = body.get("Items") or []
        return to_user_state(u, found[0]) if found else None

    async def plays(self, user: ServerUser, *, since: Optional[datetime] = None) -> list[Play]:
        """Per-play history from the Playback Reporting plugin (tier D), oldest
        first. Raises PluginMissing without the plugin. ``position_ms`` is how
        long it played; whether that counts as a play is the caller's call,
        against the item's runtime."""
        async with self._client() as c:
            body = await self._send(c, "POST", "/user_usage_stats/submit_custom_query",
                                    json={"CustomQueryString": _REPORT_QUERY, "ReplaceUserId": False})
        cols = [str(x) for x in (body.get("colums") or body.get("columns") or [])]   # the plugin spells it "colums"
        out = []
        for row in body.get("results") or []:
            rec = dict(zip(cols, row))
            if normalize_jellyfin_id(rec.get("UserId")) != user.key:
                continue
            item, at = normalize_jellyfin_id(rec.get("ItemId")), _ts(str(rec.get("DateCreated") or "").replace(" ", "T"))
            if not item or not at or (since and at < since):
                continue
            secs = _int(rec.get("PlayDuration"))
            out.append(Play(user_key=user.key, item_key=item, played_at=at, source="jellyfin_report",
                            position_ms=secs * 1000 if secs is not None else None,
                            device=rec.get("DeviceName") or None, client=rec.get("ClientName") or None))
        return out

    # ── phase 4 ─────────────────────────────────────────────────────────────

    async def upsert_playlist(self, user, title, keys):
        raise NotImplementedError("Jellyfin writes arrive with phase 4")

    async def upsert_collection(self, library, title, keys):
        raise NotImplementedError("Jellyfin writes arrive with phase 4")

    async def mark_played(self, user, key):
        raise NotImplementedError("Jellyfin writes arrive with phase 4")

    async def delete_item(self, key):
        raise NotImplementedError("Jellyfin deletes arrive with phase 4")
