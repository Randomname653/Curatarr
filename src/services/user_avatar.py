"""The signed-in user's own picture, for the badge beside their chat
bubbles and in the sidebar (2026-11-01).

Plex: the account picture plex.tv names for the user's OWN sign-in token
(``users.plex_token``). The server's token is no help here - plex.tv
answers it 401 while the server accepts it. Jellyfin: the user's primary
image from the server, with the server's API key.

The picture comes back through Curatarr, never as a third-party URL in the
page, so the browser never tells plex.tv or Gravatar who is looking. A
plex.tv avatar redirects (measured 2026-11-01): plex.tv -> assets.plex.tv
for an uploaded picture, plex.tv -> secure.gravatar.com -> i0.wp.com for
an account without one. Only those hosts, only https, every hop checked
(no automatic redirects), raster images only, 2 MB at most.

Kept in memory per user: a household is a handful of accounts and a
picture is tens of KB. "No picture" is remembered too, for less time, so a
chat reload does not ask plex.tv again each time.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from src.services.http_client import tls_context

logger = logging.getLogger(__name__)

_HOSTS = ("plex.tv", "gravatar.com", "wp.com")      # and their subdomains
_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
_MAX_BYTES = 2 * 1024 * 1024
_MAX_HOPS = 4
_TTL_FOUND = 12 * 3600
_TTL_NONE = 3600
_TTL_FAILED = 300          # an outage is not "no picture": ask again soon
_TIMEOUT = 10.0

_cache: dict[int, tuple[float, Optional[tuple[bytes, str]]]] = {}


def _host_ok(url: httpx.URL) -> bool:
    host = (url.host or "").lower()
    return url.scheme == "https" and any(host == h or host.endswith("." + h) for h in _HOSTS)


def _image(content: bytes, content_type: str) -> Optional[tuple[bytes, str]]:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct not in _TYPES or not content or len(content) > _MAX_BYTES:
        return None
    return content, ct


async def _plex_picture(token: str, *, transport=None) -> Optional[tuple[bytes, str]]:
    from src.services.media_server.plex_adapter import account_thumb
    thumb = await account_thumb(token, transport=transport)
    if not thumb:
        return None
    url = httpx.URL(thumb)
    async with httpx.AsyncClient(verify=tls_context(), timeout=_TIMEOUT, transport=transport,
                                 follow_redirects=False) as c:
        for _hop in range(_MAX_HOPS):
            if not _host_ok(url):
                logger.info("[avatar] plex.tv picture refused: %s", url.host or "no address")
                return None
            r = await c.get(url)
            if r.status_code in (301, 302, 303, 307, 308):
                url = url.join(r.headers.get("location", ""))
                continue
            return _image(r.content, r.headers.get("content-type", "")) if r.status_code == 200 else None
    return None


async def _jellyfin_picture(user_key: str, *, transport=None) -> Optional[tuple[bytes, str]]:
    from src.services.media_server import get_adapter
    found = await get_adapter(transport=transport, kind="jellyfin").user_image(user_key)
    return _image(*found) if found else None


async def picture(user_id: int, *, transport=None) -> Optional[tuple[bytes, str]]:
    """(image bytes, content type) for this user, or None: no picture, no
    Plex sign-in token, or the server could not be asked."""
    hit = _cache.get(user_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]

    from src.database.models import User
    from src.database.runner import run_read
    from src.database.connection import get_db_session
    from src.services.media_server import active_kind

    def _load():
        with get_db_session() as db:
            u = db.get(User, user_id)
            return (u.plex_user_id, u.plex_token) if u else (None, None)

    key, token = await run_read(_load)
    ttl = _TTL_NONE
    try:
        if active_kind() == "jellyfin":
            found = await _jellyfin_picture(key, transport=transport) if key else None
        else:
            found = await _plex_picture(token, transport=transport) if token else None
    except Exception as e:  # noqa: BLE001 - a missing picture falls back to the initial
        logger.info("[avatar] user %s: %s: %s", user_id, type(e).__name__, e)
        found, ttl = None, _TTL_FAILED
    _cache[user_id] = (time.monotonic() + (_TTL_FOUND if found else ttl), found)
    return found


def forget(user_id: Optional[int] = None) -> None:
    """Drop the remembered picture (one user, or all)."""
    if user_id is None:
        _cache.clear()
    else:
        _cache.pop(user_id, None)
