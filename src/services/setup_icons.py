"""Pictures for the setup wizard, fetched live and never shipped.

- **Service logos**: the icon each service's own website names in its page
  head (apple-touch-icon first, then the largest icon, then /favicon.ico),
  fetched while someone sets up and kept in memory for this run only. The
  repository carries no third-party logo (Don, 2026-11-14). For an app on the
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


async def _image(client: httpx.AsyncClient, url: str) -> Optional[tuple[bytes, str]]:
    try:
        r = await client.get(url)
    except httpx.HTTPError:
        return None
    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
    if r.status_code != 200 or not ctype.startswith("image/") or not 0 < len(r.content) <= MAX_BYTES:
        return None
    return r.content, ctype


async def service_logo(key: str, lan_url: Optional[str] = None, *, transport=None
                       ) -> Optional[tuple[bytes, str]]:
    """``(image bytes, content type)`` for a service, or None."""
    base = (lan_url or "").rstrip("/") or SITES.get(key)
    if not base:
        return None
    if base in _logos:
        return _logos[base]
    if lan_url:
        from src.services.media_server.jellyfin_connect import _allowed
        if not await _allowed(base):
            return None
    got = None
    async with httpx.AsyncClient(verify=tls_context(), timeout=_TIMEOUT, follow_redirects=True,
                                 headers={"User-Agent": _UA}, transport=transport) as c:
        for path in LAN_PATHS if lan_url else ():
            got = await _image(c, base + path)
            if got:
                _logos[base] = got
                return got
        try:
            page = await c.get(base + "/")
            html = page.text[:200_000] if page.status_code == 200 else ""
            base_url = str(page.url).rstrip("/") if page.status_code == 200 else base
        except httpx.HTTPError:
            html, base_url = "", base
        for url in [*icon_candidates(html, base_url)[:4], *([FALLBACK[key]] if key in FALLBACK else [])]:
            got = await _image(c, url)
            if got:
                break
    if got:
        _logos[base] = got     # a miss is not kept: a slow site gets another try
    else:
        logger.info("[setup] no logo for %s", key)
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
