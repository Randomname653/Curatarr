"""Pictures for the setup wizard, fetched live and never shipped.

- **Service logos**: the icon each service's own website names in its page
  head (apple-touch-icon first, then the largest icon, then /favicon.ico),
  fetched while someone sets up and kept in the install's data folder
  (refreshed monthly). The repository carries no third-party logo (Don,
  2026-11-14). For an app on the
  LAN that the wizard already found (an arr app, SoulSync), its own instance
  answers instead. The page's CSP allows no outside images, so they pass
  through here.
- **Program icons**: the icon inside a running program's .exe, for the AI
  step's "close your games" list (Windows only; elsewhere none).

The browser never names a URL here: a service is a key into SITES, a LAN app
an address that passes the endpoint policy, a program a name the setup's own
process list holds.
"""
from __future__ import annotations

import io
import logging
import os
import re
from typing import Optional
from urllib.parse import urljoin

import httpx

from src.services.http_client import tls_context

logger = logging.getLogger(__name__)

SITES = {
    "plex": "https://www.plex.tv",
    "jellyfin": "https://jellyfin.org",
    "ollama": "https://ollama.com",
    "tmdb": "https://www.themoviedb.org",
    "omdb": "https://www.omdbapi.com",
    "lastfm": "https://www.last.fm",
    "spotify": "https://open.spotify.com",
    "listenbrainz": "https://listenbrainz.org",
    "netflix": "https://www.netflix.com",
    "radarr": "https://radarr.video",
    "sonarr": "https://sonarr.tv",
    "lidarr": "https://lidarr.audio",
    "soulsync": "https://github.com/Nezreka/SoulSync",
}
# Straight image addresses when a site's own page names no usable icon
# (lidarr.audio answers /favicon.ico with HTML): the project's GitHub avatar.
FALLBACK = {
    "radarr": "https://github.com/Radarr.png",
    "sonarr": "https://github.com/Sonarr.png",
    "lidarr": "https://github.com/Lidarr.png",
}
# The arr apps serve their own logo; their favicon redirects to a login.
LAN_PATHS = ("/Content/Images/logo.svg", "/Content/Images/Icons/apple-touch-icon.png")
MAX_BYTES = 300_000
_TIMEOUT = 12.0     # netflix.com takes about 6 s for its front page
_UA = "Curatarr setup (logo fetch)"
_LINK = re.compile(r"<link\b[^>]*>", re.I)
_ATTR = re.compile(r'(\w[\w-]*)\s*=\s*["\']([^"\']*)["\']')

_logos: dict = {}      # key or LAN address -> (bytes, content type) or None
_icons: dict = {}      # program name -> png bytes or None


def icon_candidates(html: str, base: str) -> list[str]:
    """The icons a page names, best first: apple-touch-icon, then the
    largest sized icon, an SVG, any icon; /favicon.ico last."""
    ranked = []
    for tag in _LINK.findall(html or ""):
        a = {k.lower(): v for k, v in _ATTR.findall(tag)}
        rel, href = a.get("rel", "").lower(), a.get("href", "")
        if "icon" not in rel or not href or href.startswith("data:"):
            continue
        sizes = [int(x) for x in re.findall(r"(\d+)x\d+", a.get("sizes", ""))]
        score = (3 if "apple-touch" in rel else 1 if href.endswith(".svg") else 0, max(sizes or [0]))
        ranked.append((score, urljoin(base + "/", href)))
    ranked.sort(key=lambda r: r[0], reverse=True)
    out = [u for _, u in ranked]
    out.append(urljoin(base + "/", "/favicon.ico"))
    return list(dict.fromkeys(out))


# ── where a fetch may go ─────────────────────────────────────────────────────
# A page names its icons and a server names its redirects: neither is ours.
# Every address is checked right before it is fetched, the first one and
# each redirect hop (redirects are followed here, not by httpx). A public
# site's fetches stay on https and on hosts that resolve to public internet
# addresses only (github.com/Radarr.png lands on avatars.githubusercontent.com,
# netflix.com names a CDN). A LAN app's stay on that app's own host and pass
# the endpoint policy (code scanning 221/222, 2026-11-14).
_MAX_HOPS = 3


async def _resolve(host: str) -> list[str]:
    """The addresses ``host`` resolves to (a test replaces this)."""
    import asyncio
    infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, 443), 3.0)
    return [i[4][0] for i in infos]


def _is_literal(host: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


async def _public_ok(url: str) -> bool:
    """https, and every address the host resolves to is on the internet."""
    import ipaddress
    try:
        u = httpx.URL(url)
    except (httpx.InvalidURL, ValueError, TypeError):
        return False
    if u.scheme != "https" or not u.host:
        return False
    import asyncio
    try:
        addrs = [u.host.strip("[]")] if _is_literal(u.host) else await _resolve(u.host)
        return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)
    except (OSError, asyncio.TimeoutError, ValueError) as e:
        # Unresolvable, slow or odd: not fetched (a logo is optional).
        logger.debug("[setup] logo host %s refused: %s", u.host, e)
        return False


def _lan_ok(lan_base: str):
    """The check for a LAN app: its own host, and the endpoint policy."""
    from src.services.media_server.jellyfin_connect import _allowed
    host = httpx.URL(lan_base).host

    async def ok(url: str) -> bool:
        try:
            if httpx.URL(url).host != host:
                return False
        except (httpx.InvalidURL, ValueError, TypeError):
            return False
        return await _allowed(url)
    return ok


async def _get(client: httpx.AsyncClient, url: str, ok) -> Optional[httpx.Response]:
    """GET ``url``, following up to _MAX_HOPS redirects, each address passing
    ``ok`` first; None when one does not or the fetch fails."""
    for _ in range(_MAX_HOPS + 1):
        if not await ok(url):
            return None
        try:
            r = await client.get(url)
        except httpx.HTTPError:
            return None
        if not r.is_redirect:
            return r
        url = str(r.url.join(r.headers.get("location", "")))
    return None


async def _image(client: httpx.AsyncClient, url: str, ok) -> Optional[tuple[bytes, str]]:
    r = await _get(client, url, ok)
    if r is None:
        return None
    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
    if r.status_code != 200 or not ctype.startswith("image/") or not 0 < len(r.content) <= MAX_BYTES:
        return None
    return r.content, ctype


# ── kept on disk ─────────────────────────────────────────────────────────────
# A website's logo is fetched once per install and kept in the data folder
# (next to the enrichment cache), refreshed after LOGO_MAX_AGE_S; a stale copy
# still answers when the site does not. Never shipped: the repository carries
# no third-party logo, an install holds the ones its own setup fetched. A LAN
# app's logo stays in memory only (it is one address of one network).
LOGO_MAX_AGE_S = 30 * 86400
_EXT = {"image/png": ".png", "image/svg+xml": ".svg", "image/vnd.microsoft.icon": ".ico",
        "image/x-icon": ".ico", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
_inflight: dict = {}   # base -> the running fetch, so a prefetch and a page ask once
_prefetched = False
_bg: set = set()


def _cache_dir():
    from pathlib import Path

    from src.config import settings
    return Path(settings.ENRICHMENT_CACHE).parent / "logos"


# A kept logo's file name is one of these constants, never the caller's
# string: the key arrives from a URL path (code scanning 224-227).
_FILE_STEM = {k: k for k in SITES}


def _disk_get(key: str) -> Optional[tuple[bytes, str, bool]]:
    """``(bytes, content type, fresh)`` of a kept logo, or None."""
    import time
    stem = _FILE_STEM.get(key)
    if stem is None:
        return None
    d = _cache_dir()
    for ctype, ext in _EXT.items():
        p = d / f"{stem}{ext}"
        try:
            st = p.stat()
            return p.read_bytes(), ctype, time.time() - st.st_mtime < LOGO_MAX_AGE_S
        except FileNotFoundError:
            continue
        except OSError as e:
            logger.debug("[setup] kept logo %s unreadable: %s", p.name, e)
            return None
    return None


def _disk_put(key: str, got: tuple[bytes, str]) -> None:
    ext, stem = _EXT.get(got[1]), _FILE_STEM.get(key)
    if not ext or stem is None:
        return
    d = _cache_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob(f"{stem}.*"):
            old.unlink()
        tmp = d / f".{stem}{ext}.tmp"
        tmp.write_bytes(got[0])
        tmp.replace(d / f"{stem}{ext}")
    except OSError as e:
        logger.info("[setup] could not keep the %s logo: %s", stem, e)


def prefetch_logos(*, transport=None) -> None:
    """Fetch every website logo not yet kept, in the background, once per
    run: the wizard's first logo request starts it, so the later steps'
    logos are there when the owner reaches them (netflix.com alone takes
    about 6 s)."""
    import asyncio
    global _prefetched
    if _prefetched:
        return
    _prefetched = True
    for key in SITES:
        if SITES[key] not in _logos:
            t = asyncio.ensure_future(service_logo(key, transport=transport))
            _bg.add(t)                      # held until done: a bare task may be collected
            t.add_done_callback(_bg.discard)


async def service_logo(key: str, lan_url: Optional[str] = None, *, transport=None
                       ) -> Optional[tuple[bytes, str]]:
    """``(image bytes, content type)`` for a service, or None: memory, then
    the data folder, then the site itself."""
    import asyncio
    base = (lan_url or "").rstrip("/") or SITES.get(key)
    if not base:
        return None
    if base in _logos:
        return _logos[base]
    kept = None if lan_url else _disk_get(key)
    if kept and kept[2]:
        _logos[base] = kept[:2]
        return _logos[base]
    task = _inflight.get(base)
    if task is None:
        task = asyncio.ensure_future(_fetch_logo(key, lan_url, base, transport))
        _inflight[base] = task
        task.add_done_callback(lambda _t: _inflight.pop(base, None))
    got = await asyncio.shield(task)
    if got:
        _logos[base] = got
        if not lan_url:
            _disk_put(key, got)
    elif kept:
        got = _logos[base] = kept[:2]    # a stale copy beats none
    return got


async def _fetch_logo(key: str, lan_url: Optional[str], base: str, transport
                      ) -> Optional[tuple[bytes, str]]:
    ok = _lan_ok(base) if lan_url else _public_ok
    got = None
    async with httpx.AsyncClient(verify=tls_context(), timeout=_TIMEOUT, follow_redirects=False,
                                 headers={"User-Agent": _UA}, transport=transport) as c:
        for path in LAN_PATHS if lan_url else ():
            got = await _image(c, base + path, ok)
            if got:
                return got
        page = await _get(c, base + "/", ok)
        if page is not None and page.status_code == 200:
            html, base_url = page.text[:200_000], str(page.url).rstrip("/")
        else:
            html, base_url = "", base
        for url in [*icon_candidates(html, base_url)[:4], *([FALLBACK[key]] if key in FALLBACK else [])]:
            got = await _image(c, url, ok)
            if got:
                break
    if not got:
        logger.info("[setup] no logo for %s", key)   # not kept: a slow site gets another try
    return got


# ── program icons (Windows) ─────────────────────────────────────────────────
def _exe_path(name: str) -> Optional[str]:
    import psutil
    for p in psutil.process_iter(["name"]):
        if (p.info.get("name") or "").lower() == name.lower():
            try:
                return p.exe()
            except psutil.Error:
                continue
    return None


def _exe_icon_png(path: str, size: int = 32) -> Optional[bytes]:
    """The .exe's first icon drawn into a 32-bit bitmap, as PNG."""
    import ctypes
    import ctypes.wintypes as wt
    shell32, user32, gdi32 = ctypes.windll.shell32, ctypes.windll.user32, ctypes.windll.gdi32
    # Handles are pointer-sized: without restypes a 64-bit handle is cut to an int.
    user32.GetDC.restype = wt.HDC
    gdi32.CreateCompatibleDC.restype = wt.HDC
    gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
    gdi32.CreateDIBSection.restype = wt.HBITMAP
    gdi32.SelectObject.restype = wt.HGDIOBJ
    gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wt.HDC]
    user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    user32.DrawIconEx.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, wt.HICON, ctypes.c_int,
                                  ctypes.c_int, wt.UINT, wt.HBRUSH, wt.UINT]
    user32.DestroyIcon.argtypes = [wt.HICON]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    icon = wt.HICON()
    if shell32.ExtractIconExW(path, 0, ctypes.byref(icon), None, 1) <= 0 or not icon:
        return None
    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), size, -size, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    try:
        if not bmp:
            return None
        old = gdi32.SelectObject(dc, bmp)
        user32.DrawIconEx(dc, 0, 0, icon, size, size, 0, None, 3)   # DI_NORMAL
        raw = ctypes.string_at(bits, size * size * 4)
        gdi32.SelectObject(dc, old)
    finally:
        if bmp:
            gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(dc)
        user32.ReleaseDC(None, screen)
        user32.DestroyIcon(icon)
    from PIL import Image
    img = Image.frombuffer("RGBA", (size, size), raw, "raw", "BGRA", 0, 1)
    if img.getextrema()[3][1] == 0:      # an icon without alpha: draw it opaque
        img.putalpha(255)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def program_icon(name: str) -> Optional[bytes]:
    """PNG of a running program's icon; None off Windows or when unreadable."""
    if os.name != "nt":
        return None
    key = name.lower()
    if key not in _icons:
        try:
            path = _exe_path(key)
            _icons[key] = _exe_icon_png(path) if path else None
        except Exception as e:  # noqa: BLE001 - a picture, never a failure
            logger.info("[setup] no icon for %s: %s", name, e)
            _icons[key] = None
    return _icons[key]
