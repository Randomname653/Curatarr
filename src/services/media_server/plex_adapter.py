"""Plex behind the media-server seam (docs/JELLYFIN_DESIGN.md §6.3).

Phase 1 is the READ path: server identity, libraries, items with every
identity a rebind needs (external ids, file path + size, parent keys and
numbers), users. Today's Plex modules (plex_sync, plex_playlists,
plex_collections) keep running unchanged; callers move onto this adapter
step by step, and the history and write methods arrive with phases 3 and 4.

Every read raises on a transport or HTTP failure. ``plex_sync`` swallows a
failed page and returns what it had; an adapter must not, because callers
of ``items()`` will prune on what is missing (design §6.2).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import AsyncIterator, Optional, Sequence

import httpx

from src.services.http_client import tls_context
from src.services.media_server.base import (
    Capabilities, ExternalIds, FileRef, ItemKind, Library, MediaItem, ServerInfo,
    ServerUser, is_plex_key,
)

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0
_PAGE = 400

# Plex's own type numbers and names → ours. "show" is a series.
_TYPE_NUM = {"movie": 1, "series": 2, "season": 3, "episode": 4,
             "artist": 8, "album": 9, "track": 10}
_KIND_OF_TYPE = {"movie": "movie", "show": "series", "season": "season",
                 "episode": "episode", "artist": "artist", "album": "album",
                 "track": "track"}
_LIBRARY_KIND = {"movie": "movie", "show": "show", "artist": "music"}
# What items() walks per library kind when the caller names no kinds.
_DEFAULT_KINDS = {"movie": ("movie",), "show": ("series", "season", "episode"),
                  "music": ("artist", "album", "track")}

_CAPABILITIES = Capabilities(
    per_play_history=True, now_playing=False, star_ratings=True,
    native_watchlist=True, music_moods=True, per_user_tokens=True,
)


class PlexError(RuntimeError):
    """Plex did not answer, or answered with an error."""


def _ts(value) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).replace(tzinfo=None) if value else None
    except (TypeError, ValueError, OverflowError):
        return None


def _int(value) -> Optional[int]:
    try:
        return int(value) if value is not None and str(value).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _external_ids(kind: str, item: dict) -> ExternalIds:
    """Every id in a Plex item's Guid[] (``includeGuids=1``). On an episode
    the tvdb guid is the EPISODE's id; on music items mbid:// is filed per
    level as Plex's music agent writes it (artist, release group, recording)."""
    ids: dict = {}
    for g in item.get("Guid") or []:
        scheme, _, value = str(g.get("id") or "").partition("://")
        value = value.split("?")[0].strip()
        if not value:
            continue
        if scheme == "tmdb" and value.isdigit():
            ids["tmdb_id"] = int(value)
        elif scheme == "tvdb" and value.isdigit():
            ids["tvdb_episode_id" if kind == "episode" else "tvdb_id"] = int(value)
        elif scheme == "imdb":
            ids["imdb_id"] = value
        elif scheme == "mbid":
            field = {"artist": "mb_artist_id", "album": "mb_release_group_id",
                     "track": "mb_recording_id"}.get(kind)
            if field:
                ids[field] = value
    return ExternalIds(**ids)


def to_media_item(item: dict, library_key: Optional[str] = None) -> Optional[MediaItem]:
    """One Plex metadata dict as a MediaItem; None for types we do not model
    (clips, photos, collections)."""
    kind = _KIND_OF_TYPE.get(item.get("type") or "")
    key = str(item.get("ratingKey") or "")
    if not kind or not key:
        return None
    files = tuple(
        FileRef(path=str(p["file"]), size_bytes=_int(p.get("size")))
        for m in item.get("Media") or [] for p in m.get("Part") or [] if p.get("file"))
    season = episode = disc = track = None
    if kind == "season":
        season = _int(item.get("index"))
    elif kind == "episode":
        season, episode = _int(item.get("parentIndex")), _int(item.get("index"))
    elif kind == "track":
        disc, track = _int(item.get("parentIndex")), _int(item.get("index"))
    lib = item.get("librarySectionID") or library_key
    return MediaItem(
        key=key, kind=kind, title=str(item.get("title") or ""),
        library_key=str(lib) if lib is not None else None,
        year=_int(item.get("year")),
        parent_key=str(item["parentRatingKey"]) if item.get("parentRatingKey") else None,
        grandparent_key=str(item["grandparentRatingKey"]) if item.get("grandparentRatingKey") else None,
        parent_title=item.get("parentTitle"), grandparent_title=item.get("grandparentTitle"),
        season_number=season, episode_number=episode, disc_number=disc, track_number=track,
        external_ids=_external_ids(kind, item), files=files,
        genres=tuple(str(g.get("tag")) for g in item.get("Genre") or [] if g.get("tag")),
        added_at=_ts(item.get("addedAt")), duration_ms=_int(item.get("duration")),
    )


class PlexAdapter:
    kind = "plex"

    def __init__(self, base_url: str, token: str, *,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self._base = str(base_url).rstrip("/")
        self._token = token
        self._transport = transport          # tests pass an httpx.MockTransport
        self._server_id: Optional[str] = None

    # ── plumbing ────────────────────────────────────────────────────────────
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(verify=tls_context(), timeout=_TIMEOUT, transport=self._transport)

    def _headers(self, extra: Optional[dict] = None) -> dict:
        return {"Accept": "application/json", "X-Plex-Token": self._token, **(extra or {})}

    async def _get(self, client: httpx.AsyncClient, path: str, *, params: Optional[dict] = None,
                   headers: Optional[dict] = None, allow_404: bool = False) -> Optional[dict]:
        try:
            r = await client.get(f"{self._base}{path}", params=params, headers=self._headers(headers))
        except httpx.HTTPError as e:
            raise PlexError(f"Plex {path}: {type(e).__name__}") from e
        if allow_404 and r.status_code == 404:
            return None
        if r.status_code != 200:
            raise PlexError(f"Plex {path}: HTTP {r.status_code}")
        return r.json().get("MediaContainer") or {}

    # ── facts ───────────────────────────────────────────────────────────────
    def capabilities(self) -> Capabilities:
        return _CAPABILITIES

    def is_server_key(self, value) -> bool:
        return is_plex_key(value)

    def deep_link(self, key: str) -> Optional[str]:
        """The item in Plex Web. Needs the machine id from ``ping()``; without
        it, the server's web root (the same fallback the proposals use)."""
        if not is_plex_key(key):
            return None
        if not self._server_id:
            return f"{self._base}/web/index.html"
        return (f"{self._base}/web/index.html#!/server/{self._server_id}"
                f"/details?key=%2Flibrary%2Fmetadata%2F{key}")

    async def ping(self) -> ServerInfo:
        async with self._client() as c:
            mc = await self._get(c, "/")
        self._server_id = str(mc.get("machineIdentifier") or "") or None
        return ServerInfo(kind="plex", name=str(mc.get("friendlyName") or ""),
                          version=str(mc.get("version") or ""), server_id=self._server_id or "")

    # ── library ─────────────────────────────────────────────────────────────
    async def libraries(self) -> list[Library]:
        async with self._client() as c:
            mc = await self._get(c, "/library/sections")
        out = []
        for d in mc.get("Directory") or []:
            kind = _LIBRARY_KIND.get(d.get("type") or "")
            if not kind or d.get("key") is None:
                continue
            out.append(Library(key=str(d["key"]), title=str(d.get("title") or ""), kind=kind,
                               roots=tuple(str(loc["path"]) for loc in d.get("Location") or []
                                           if loc.get("path"))))
        return out

    async def items(self, library: Library, *, kinds: Sequence[ItemKind] = (),
                    since: Optional[datetime] = None) -> AsyncIterator[MediaItem]:
        """Every item of ``kinds`` in ``library``, paged. ``since`` filters on
        the added date here, after the fetch: Plex's own addedAt filter is not
        pinned by a fixture yet."""
        for kind in kinds or _DEFAULT_KINDS.get(library.kind, ()):
            type_num = _TYPE_NUM.get(kind)
            if type_num is None:
                continue
            start = 0
            async with self._client() as c:
                while True:
                    mc = await self._get(
                        c, f"/library/sections/{library.key}/all",
                        params={"type": type_num, "includeGuids": 1},
                        headers={"X-Plex-Container-Start": str(start),
                                 "X-Plex-Container-Size": str(_PAGE)})
                    page = mc.get("Metadata") or []
                    for raw in page:
                        it = to_media_item(raw, library.key)
                        if it is None or (since and it.added_at and it.added_at < since):
                            continue
                        yield it
                    if len(page) < _PAGE:
                        break
                    start += len(page)

    async def item(self, key: str) -> Optional[MediaItem]:
        if not is_plex_key(key):
            return None
        async with self._client() as c:
            mc = await self._get(c, f"/library/metadata/{key}", params={"includeGuids": 1},
                                 allow_404=True)
        if mc is None:
            return None
        meta = mc.get("Metadata") or []
        return to_media_item(meta[0]) if meta else None

    async def children(self, key: str) -> list[MediaItem]:
        if not is_plex_key(key):
            return []
        async with self._client() as c:
            mc = await self._get(c, f"/library/metadata/{key}/children",
                                 params={"includeGuids": 1}, allow_404=True)
        if mc is None:
            return []
        return [it for it in (to_media_item(m) for m in mc.get("Metadata") or []) if it]

    async def key_exists(self, key: str) -> bool:
        """Only a 404 means gone; any other failure raises (the rule
        plex_playlists.key_exists learned the hard way)."""
        if not is_plex_key(key):
            return False
        async with self._client() as c:
            mc = await self._get(c, f"/library/metadata/{key}", allow_404=True)
        return mc is not None

    # ── people ──────────────────────────────────────────────────────────────
    async def users(self) -> list[ServerUser]:
        """The server's local accounts. accountID 1 is always the owner
        (plex_sync relies on the same rule); 0 is Plex's nameless system
        entry."""
        async with self._client() as c:
            mc = await self._get(c, "/accounts")
        out = []
        for a in mc.get("Account") or []:
            aid, name = str(a.get("id") or ""), str(a.get("name") or "").strip()
            if not aid or aid == "0" or not name:
                continue
            out.append(ServerUser(key=aid, name=name, is_admin=(aid == "1")))
        return out

    # ── phases 3 and 4 ──────────────────────────────────────────────────────
    async def user_states(self, user, *, since=None):
        raise NotImplementedError("Plex user states move behind the seam in phase 3")

    async def live_sessions(self):
        return []                            # capabilities.now_playing is False

    async def plays(self, user, *, since=None):
        raise NotImplementedError("Plex plays move behind the seam in phase 3")

    async def upsert_playlist(self, user, title, keys):
        raise NotImplementedError("Plex writes move behind the seam in phase 4")

    async def upsert_collection(self, library, title, keys):
        raise NotImplementedError("Plex writes move behind the seam in phase 4")

    async def mark_played(self, user, key):
        raise NotImplementedError("Plex writes move behind the seam in phase 4")

    async def delete_item(self, key):
        raise NotImplementedError("Plex deletes move behind the seam in phase 4")
