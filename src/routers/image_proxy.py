"""
Curatarr — image proxy (Pass 97).

Why this exists
---------------
The frontend used to render external poster URLs directly into ``<img>``
tags — every recommendation/proposal/search view sent a request to
``image.tmdb.org``, ``e-cdns-images.dzcdn.net``, or one of the *arr
remoteUrl hosts with the user's IP + User-Agent + Referer. TMDB/Deezer
could see in real time *which* movie/show/artist you were looking at.

That broke the README's "nothing about your library leaves the machine"
promise — the *content* never left, but the access pattern did.

This proxy fixes it: the frontend calls ``/api/image/proxy?src=<url>``,
the server validates the host against a strict whitelist, fetches the
image once, caches it on disk under ``data/cache/images/``, and streams
it back. Upstream sees the server's IP one time per image (cached
forever after), not the user clicking around every few seconds.

Security
--------
- ``Depends(get_current_user)`` — authenticated users only, no open
  proxy for anonymous LAN visitors.
- Host whitelist (exact match + suffix match for the dzcdn.net family).
  Rejects IP literals, file://, anything outside the list.
- Only ``http://`` and ``https://`` schemes accepted.
- Upstream Content-Type must start with ``image/`` — no HTML / JS / etc
  smuggled through.
- 5 MB upper bound — posters are tens of KB; cap protects disk + memory.
- Cache key is SHA-256 of the canonical URL → 16-char hex prefix so cache
  files are stable across restarts and safe to enumerate on disk.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response

from src.database.models import User
from src.routers.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Whitelist ────────────────────────────────────────────────────────────────
#
# Exact-host matches first, then suffix matches for CDN families that hand
# out unpredictable subdomains. Keep this list tight: every entry is a
# domain the user's IP is allowed to touch by clicking around the UI.

_ALLOWED_HOSTS_EXACT: frozenset[str] = frozenset({
    "image.tmdb.org",              # TMDB posters
    "api.deezer.com",              # Deezer ``/img/...`` paths
    "coverartarchive.org",         # MusicBrainz CAA
    "lastfm.freetls.fastly.net",   # Last.fm artist images
    "artworks.thetvdb.com",        # TVDB cover art (Sonarr remoteUrl)
    "assets.fanart.tv",            # Fanart.tv (Sonarr/Radarr remoteUrl)
})

_ALLOWED_HOST_SUFFIXES: tuple[str, ...] = (
    ".dzcdn.net",                  # Deezer CDN (e-cdns-images.dzcdn.net, etc.)
)


def _host_allowed(host: str) -> bool:
    if not host:
        return False
    host = host.lower()
    if host in _ALLOWED_HOSTS_EXACT:
        return True
    return any(host.endswith(suf) for suf in _ALLOWED_HOST_SUFFIXES)


# ── Disk cache ───────────────────────────────────────────────────────────────

_CACHE_DIR = Path("data/cache/images")
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_MAX_BYTES = 5 * 1024 * 1024   # 5 MB per image — posters are 10-80 KB typically
_FETCH_TIMEOUT = 10             # seconds — upstream CDNs are fast


def _cache_path(url: str, content_type: Optional[str] = None) -> Path:
    """SHA-256(url)[:16] + extension. Stable across restarts."""
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    ext = _ext_for_ct(content_type) if content_type else ""
    return _CACHE_DIR / f"{h}{ext}"


def _ext_for_ct(ct: str) -> str:
    ct = (ct or "").split(";", 1)[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/jpg":  ".jpg",
        "image/png":  ".png",
        "image/webp": ".webp",
        "image/gif":  ".gif",
        "image/avif": ".avif",
    }.get(ct, "")


def _find_existing(url: str) -> Optional[Path]:
    """Look for a cached file with any of the supported extensions."""
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    for ext in (".jpg", ".png", ".webp", ".gif", ".avif", ""):
        p = _CACHE_DIR / f"{h}{ext}"
        if p.is_file():
            return p
    return None


_CT_FOR_EXT = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".webp": "image/webp",
    ".gif":  "image/gif",
    ".avif": "image/avif",
}


# In-process lock so two simultaneous clicks on the same image don't both
# do the upstream fetch (one wins, the other waits and reads from cache).
_inflight: dict[str, asyncio.Lock] = {}


@router.get("/proxy")
async def proxy_image(
    src: str = Query(..., min_length=8, max_length=2048),
    _user: User = Depends(get_current_user),
):
    """Proxy + cache an external image. ``src`` MUST be on the whitelist."""

    # 1. Parse + scheme + host check
    try:
        parsed = urlparse(src)
    except Exception:
        raise HTTPException(400, "Invalid URL")

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "Only http(s) schemes accepted")
    if not _host_allowed(parsed.hostname or ""):
        # Logging level kept low — this is the expected outcome for any
        # client that tries to feed us an arbitrary URL.
        logger.debug("[image_proxy] reject non-whitelisted host: %s", parsed.hostname)
        raise HTTPException(403, f"Host not on image-proxy whitelist: {parsed.hostname}")

    # 2. Disk cache hit?
    cached = _find_existing(src)
    if cached:
        ct = _CT_FOR_EXT.get(cached.suffix, "application/octet-stream")
        return FileResponse(cached, media_type=ct, headers={"Cache-Control": "public, max-age=86400"})

    # 3. Single-flight: if another coroutine is already fetching this URL,
    #    wait on its lock and then serve from cache.
    lock = _inflight.setdefault(src, asyncio.Lock())
    async with lock:
        # Recheck after acquiring — the in-flight fetch may have completed
        # while we were waiting.
        cached = _find_existing(src)
        if cached:
            ct = _CT_FOR_EXT.get(cached.suffix, "application/octet-stream")
            return FileResponse(cached, media_type=ct, headers={"Cache-Control": "public, max-age=86400"})

        # 4. Fetch upstream
        try:
            async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=True) as client:
                r = await client.get(src)
        except Exception as e:
            logger.info("[image_proxy] upstream fetch failed for %s: %s", parsed.hostname, e)
            _inflight.pop(src, None)
            raise HTTPException(502, "Upstream image fetch failed")

        if r.status_code != 200:
            _inflight.pop(src, None)
            raise HTTPException(502, f"Upstream returned HTTP {r.status_code}")

        ct = (r.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if not ct.startswith("image/"):
            _inflight.pop(src, None)
            raise HTTPException(415, f"Upstream content-type not image/*: {ct!r}")

        body = r.content
        if len(body) > _MAX_BYTES:
            _inflight.pop(src, None)
            raise HTTPException(413, f"Upstream image too large ({len(body)} bytes)")

        # 5. Write cache
        path = _cache_path(src, ct)
        try:
            path.write_bytes(body)
        except Exception as e:
            # Cache write failed — still serve the body in-memory so the
            # user sees the image. Subsequent requests will retry the
            # upstream until the cache write succeeds.
            logger.warning("[image_proxy] cache write failed for %s: %s", path.name, e)
            _inflight.pop(src, None)
            return Response(content=body, media_type=ct,
                            headers={"Cache-Control": "no-store"})

    _inflight.pop(src, None)
    return FileResponse(path, media_type=ct, headers={"Cache-Control": "public, max-age=86400"})
