"""
Curatarr — Shoko Server client (LAN neighbour, the anime library manager).

Shoko (shokoanime.com) indexes an anime collection by file hash against
AniDB and keeps the AniDB data locally: tags with weights, ratings with
votes, cast, relations, episode-level titles. Curatarr reports the owner's
finished anime episodes there as watched (services/shoko_sync.py); reading
that metadata is the next phase.

API v3, read from the live server's own OpenAPI (/swagger/v3/swagger.json,
Shoko 6.0.0-dev.588, 2026-11-08):
- auth: header ``apikey``. A key acts as the Shoko user it was made for,
  and watched state is per Shoko user, so one key per user.
- ``GET /Init/Version`` needs no key.
- ``GET /User/Current``: the key's user, incl. ``PlexUsernames``.
- ``GET /File/PathEndsWith?path=&include=XRefs``: files whose path ends so;
  each file's ``SeriesIDs[].EpisodeIDs[].ID`` are the Shoko episodes it
  holds (null without the include).
- ``GET /Tmdb/Episode/{id}/Shoko/Episode``: Shoko episodes linked to a
  TMDB episode. TvDB is gone from Shoko since 5.1.
- ``GET|PUT /Episode/{id}/UserData``: ``PlaybackCount`` and
  ``LastPlayedAt`` - a play written with the day it happened, which
  ``POST /Episode/{id}/Watched/true`` (always "now") cannot.

Every call degrades to None / [] when Shoko is unconfigured or unreachable.
Writes never leave a test process (writes_allowed).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from src.config import settings
from src.services.http_client import tls_context

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0


def _configured() -> bool:
    return bool(settings.SHOKO_URL and api_keys())


def api_keys() -> list[str]:
    """The configured keys, one per Shoko user (comma-separated in .env)."""
    raw = settings.SHOKO_API_KEY or ""
    raw = raw.get_secret_value() if hasattr(raw, "get_secret_value") else raw
    return [k.strip() for k in str(raw).split(",") if k.strip()]


def _base(url: Optional[str] = None) -> str:
    return (url or settings.SHOKO_URL or "").rstrip("/") + "/api/v3"


def writes_allowed() -> bool:
    """Shoko is set up and this is the app, not a test: a battery run on a
    machine whose .env points at the real Shoko must never mark anything
    watched there (a test injects its own transport instead)."""
    from src.log_setup import is_test_process
    return _configured() and not is_test_process()


def parse_time(value) -> Optional[datetime]:
    """A Shoko timestamp as naive UTC (watch_history's convention); None
    for empty or unreadable values."""
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is not None:
        t = t.astimezone(timezone.utc).replace(tzinfo=None)
    return t


def format_time(t: datetime) -> str:
    """Naive UTC -> the ISO form Shoko reads as UTC."""
    return t.replace(microsecond=0).isoformat() + "Z"


def server_major(version: Optional[dict]) -> Optional[int]:
    """6 for {"Server": {"Version": "6.0.0-dev.588"}}; None when unknown."""
    v = str(((version or {}).get("Server") or {}).get("Version") or "")
    head = v.split(".", 1)[0]
    return int(head) if head.isdigit() else None


class ShokoSession:
    """One key's view of Shoko over one shared HTTP client. Every reader
    returns None / [] on failure and logs it; the writer reports a status."""

    def __init__(self, client: httpx.AsyncClient, key: str, url: Optional[str] = None):
        self._c = client
        self._key = key
        self._base = _base(url)

    async def _get(self, path: str, params: Optional[dict] = None, *, auth: bool = True):
        headers = {"apikey": self._key} if auth else {}
        try:
            r = await self._c.get(self._base + path, headers=headers, params=params)
        except httpx.HTTPError as e:
            logger.info("[shoko] %s: no answer (%s)", path, e)
            return None
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            logger.info("[shoko] %s -> HTTP %d", path, r.status_code)
            return None
        try:
            return r.json()
        except ValueError:
            logger.info("[shoko] %s: unreadable answer", path)
            return None

    async def version(self) -> Optional[dict]:
        return await self._get("/Init/Version", auth=False)

    async def current_user(self) -> Optional[dict]:
        u = await self._get("/User/Current")
        return u if isinstance(u, dict) else None

    async def files_by_name(self, name: str, limit: int = 20) -> list[dict]:
        """Files whose path ends with ``name`` (a file name, or the last
        folders plus the name)."""
        # The episode links (SeriesIDs) come only with include=XRefs: without
        # it the live server answered ``"SeriesIDs": null`` (2026-11-08).
        got = await self._get("/File/PathEndsWith",
                              {"path": name, "limit": limit, "include": "XRefs"})
        return [f for f in got if isinstance(f, dict)] if isinstance(got, list) else []

    async def episodes_by_tmdb_episode(self, tmdb_episode_id: int) -> list[int]:
        return await self._episode_ids(f"/Tmdb/Episode/{int(tmdb_episode_id)}/Shoko/Episode")

    async def episodes_by_tmdb_movie(self, tmdb_movie_id: int) -> list[int]:
        return await self._episode_ids(f"/Tmdb/Movie/{int(tmdb_movie_id)}/Shoko/Episode")

    async def _episode_ids(self, path: str) -> list[int]:
        got = await self._get(path)
        if not isinstance(got, list):
            return []
        return [i for i in ((e.get("IDs") or {}).get("ID") for e in got if isinstance(e, dict))
                if isinstance(i, int) and i > 0]

    async def episode_user_data(self, episode_id: int) -> Optional[dict]:
        d = await self._get(f"/Episode/{int(episode_id)}/UserData")
        return d if isinstance(d, dict) else None

    async def put_episode_user_data(self, episode_id: int, data: dict) -> int:
        """PUT the user data; Shoko's HTTP status, or 0 when it did not
        answer. The caller decides what a status means."""
        try:
            r = await self._c.put(f"{self._base}/Episode/{int(episode_id)}/UserData",
                                  headers={"apikey": self._key}, json=data)
        except httpx.HTTPError as e:
            logger.info("[shoko] user data of episode %s: no answer (%s)", episode_id, e)
            return 0
        if r.status_code >= 400:
            logger.info("[shoko] user data of episode %s -> HTTP %d", episode_id, r.status_code)
        return r.status_code


def episode_ids_of_file(f: dict) -> list[int]:
    """The Shoko episode ids a file record holds."""
    out: list[int] = []
    for xref in f.get("SeriesIDs") or []:
        for ep in (xref or {}).get("EpisodeIDs") or []:
            i = (ep or {}).get("ID")
            if isinstance(i, int) and i > 0 and i not in out:
                out.append(i)
    return out


def new_client(transport=None) -> httpx.AsyncClient:
    """The shared client of one run; ``transport`` is a test's fake server."""
    return httpx.AsyncClient(verify=tls_context(), timeout=_TIMEOUT, transport=transport)


async def test_connection(url: str, key: str, *, transport=None) -> dict:
    """The wizard's "Test connection": the version without a key, then the
    key's user. A list of keys tests each one."""
    keys = [k.strip() for k in (key or "").split(",") if k.strip()]
    if not url:
        return {"ok": False, "error": "no Shoko URL"}
    if not keys:
        return {"ok": False, "error": "no API key"}
    async with new_client(transport) as c:
        first = ShokoSession(c, keys[0], url)
        version = await first.version()
        if not version:
            return {"ok": False, "error": "Shoko did not answer"}
        users = []
        for k in keys:
            u = await ShokoSession(c, k, url).current_user()
            if not u:
                return {"ok": False, "error": f"key {len(users) + 1} was refused"}
            users.append(u.get("Username") or "?")
    ver = ((version.get("Server") or {}).get("Version")) or ""
    # The panel shows "connected · <version>": the users ride along there.
    return {"ok": True, "version": f"{ver} as {', '.join(users)}", "service": "shoko",
            "users": users}
