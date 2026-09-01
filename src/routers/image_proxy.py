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
- Host whitelist (exact match + suffix match for the dzcdn.net family).
  Rejects IP literals, file://, anything outside the list. THIS is the
  security boundary — not auth.
- Only ``http://`` and ``https://`` schemes accepted.
- Upstream Content-Type must start with ``image/`` — no HTML / JS / etc
  smuggled through.
- 5 MB upper bound — posters are tens of KB; cap protects disk + memory.
- Redirects are NOT auto-followed (audit follow-up): a whitelisted host
  cannot 30x us into a non-whitelisted host. Manual loop with re-check.
- Cache key is SHA-256 of the canonical URL → 16-char hex prefix so cache
  files are stable across restarts and safe to enumerate on disk.

No auth gate
------------
The endpoint is intentionally open (Pass 97 follow-up). Browsers cannot
attach ``Authorization: Bearer …`` headers to ``<img src="…">`` requests
— that's a built-in restriction — so any auth gate on this endpoint
breaks every poster in the UI (which is what happened right after the
initial Pass 97 ship).

The whitelist + image-only content-type + 5 MB cap are the real boundary
here. The worst an attacker on the same LAN can do is request whitelisted
TMDB / Deezer / Last.fm / Fanart.tv images — content they could fetch
directly from those CDNs anyway. They gain no new capability through the
proxy. The disk cache can be made to grow, but each entry is bounded
and the LAN attacker could equally fill any other writable Curatarr path.

If the deployment is exposed to the public internet, the existing
non-private-endpoint warning (Pass 97 #5) flags that as a configuration
risk — same threat model applies here.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response

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

from src.paths import DATA_DIR
_CACHE_DIR = DATA_DIR / "cache" / "images"
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
):
    """Proxy + cache an external image. ``src`` MUST be on the whitelist.

    No auth gate — see module docstring for the rationale (browsers can't
    attach Authorization headers to <img> tags, so auth would break every
    poster in the UI; the whitelist + content-type + size cap are the
    real security boundary here).
    """

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
        return FileResponse(cached, media_type=ct, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    # 3. Single-flight: if another coroutine is already fetching this URL,
    #    wait on its lock and then serve from cache.
    lock = _inflight.setdefault(src, asyncio.Lock())
    async with lock:
        # Recheck after acquiring — the in-flight fetch may have completed
        # while we were waiting.
        cached = _find_existing(src)
        if cached:
            ct = _CT_FOR_EXT.get(cached.suffix, "application/octet-stream")
            return FileResponse(cached, media_type=ct, headers={"Cache-Control": "public, max-age=31536000, immutable"})

        # 4. Fetch upstream — disable auto-redirect so the whitelist is
        #    enforced on every hop. With ``follow_redirects=True`` a
        #    whitelisted host could 30x us at a non-whitelisted host and
        #    httpx would silently make the second connection (audit
        #    follow-up). Manual loop: max 3 hops, re-validate the host
        #    on each. ``image.tmdb.org`` serves direct 200s in practice;
        #    Deezer's CDN occasionally 301s within the dzcdn.net family
        #    which is fine because the suffix-whitelist still matches.
        try:
            async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=False) as client:
                next_url = src
                r = None
                for hop in range(4):   # initial + up to 3 redirects
                    r = await client.get(next_url)
                    if not r.is_redirect:
                        break
                    loc = r.headers.get("location")
                    if not loc:
                        break
                    # Resolve relative redirects against the current URL.
                    target = httpx.URL(next_url).join(loc)
                    target_host = (target.host or "").lower()
                    if not _host_allowed(target_host):
                        logger.info(
                            "[image_proxy] reject redirect %s -> %s (not whitelisted)",
                            httpx.URL(next_url).host, target_host,
                        )
                        _inflight.pop(src, None)
                        raise HTTPException(
                            403, f"Redirect target not whitelisted: {target_host}",
                        )
                    next_url = str(target)
                else:
                    # Hit the redirect ceiling without ever landing on a 200.
                    _inflight.pop(src, None)
                    raise HTTPException(502, "Too many redirects")
        except HTTPException:
            raise
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
    return FileResponse(path, media_type=ct, headers={"Cache-Control": "public, max-age=31536000, immutable"})
