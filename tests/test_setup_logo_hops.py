"""The setup wizard's logo fetch goes only where it was sent.

A service's logo comes from the site in SITES, whose own page names the
icon; a LAN app's logo from that app. Neither the page's markup nor a
redirect may send the server anywhere else: a site's icon link or a 302 to
a LAN or link-local address must not be fetched (2026-11-14).
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio
import time

import httpx

from src.routers import image_proxy
from src.services import setup_icons

app_db()

PNG = {"content-type": "image/png"}
seen: list[str] = []


def _public_site(req):
    seen.append(str(req.url))
    if req.url.host == "www.themoviedb.org":
        if req.url.path == "/":
            return httpx.Response(200, text=(
                '<link rel="apple-touch-icon" sizes="180x180" href="http://169.254.169.254/latest/a.png">'
                '<link rel="icon" sizes="64x64" href="http://192.168.1.1/cam.png">'
                '<link rel="icon" sizes="32x32" href="/redirect.png">'))
        if req.url.path == "/redirect.png":
            return httpx.Response(302, headers={"location": "http://10.0.0.5/admin.png"})
        return httpx.Response(404)
    return httpx.Response(200, content=b"inner-bytes", headers=PNG)


# The public site resolves to a public address (no DNS in the test).
image_proxy._resolved["www.themoviedb.org"] = (True, time.time() + 3600)
setup_icons._logos.clear()
got = asyncio.run(setup_icons.service_logo("tmdb", transport=httpx.MockTransport(_public_site)))
inner = [u for u in seen if "169.254." in u or "192.168." in u or "10.0.0.5" in u]
check("a site's icon links and redirects never reach a LAN or link-local address", not inner, inner)
check("nothing from an inner address is handed back as the logo", got is None, got)


seen.clear()


def _lan_app(req):
    seen.append(str(req.url))
    if req.url.host == "192.168.0.189":
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta.png"})
    return httpx.Response(200, content=b"meta-bytes", headers=PNG)


setup_icons._logos.clear()
got = asyncio.run(setup_icons.service_logo("radarr", "http://192.168.0.189:7878",
                                           transport=httpx.MockTransport(_lan_app)))
off = [u for u in seen if not u.startswith("http://192.168.0.189:7878/")]
check("a LAN app's logo stays on that app: a redirect elsewhere is not followed", not off, off)
check("the redirect's target is not handed back as the logo", got is None, got)


seen.clear()


def _good(req):
    seen.append(str(req.url))
    if req.url.path == "/":
        return httpx.Response(301, headers={"location": "https://www.themoviedb.org/home"})
    if req.url.path == "/home":
        return httpx.Response(200, text='<link rel="apple-touch-icon" href="/a.png">')
    if req.url.path == "/a.png":
        return httpx.Response(200, content=b"png-bytes", headers=PNG)
    return httpx.Response(404)


setup_icons._logos.clear()
got = asyncio.run(setup_icons.service_logo("tmdb", transport=httpx.MockTransport(_good)))
check("a public site's own redirect and icon still give the logo", got == (b"png-bytes", "image/png"), got)

seen.clear()


def _lan_good(req):
    seen.append(str(req.url))
    if req.url.path == "/Content/Images/logo.svg":
        return httpx.Response(302, headers={"location": "/Content/Images/logo-v2.svg"})
    if req.url.path == "/Content/Images/logo-v2.svg":
        return httpx.Response(200, content=b"<svg/>", headers={"content-type": "image/svg+xml"})
    return httpx.Response(404)


setup_icons._logos.clear()
got = asyncio.run(setup_icons.service_logo("radarr", "http://192.168.0.189:7878",
                                           transport=httpx.MockTransport(_lan_good)))
check("a LAN app's redirect within itself still gives its logo", got and got[0] == b"<svg/>", got)

check.done()
