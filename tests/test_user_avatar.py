"""The user's own picture for the chat and sidebar badge (2026-11-01):
services/user_avatar.py and GET /api/users/me/avatar.

Plex asks plex.tv with the user's own sign-in token and follows the
avatar's redirects by hand, host by host; Jellyfin asks the server for the
user's primary image. Anything else - another host, plain http, a page
instead of an image, an outage - leaves the initial in place.

    python tests/test_user_avatar.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio
import time

import httpx
from pydantic import SecretStr

conn = app_db()
from src.config import settings  # noqa: E402
from src.database.models import User  # noqa: E402
from src.services import user_avatar as ua  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
with conn.get_db_session() as db:
    db.add_all([User(id=1, plex_user_id="111", plex_username="don", is_admin=True, plex_token="tok-own"),
                User(id=2, plex_user_id="222", plex_username="sam"),
                User(id=3, plex_user_id="a" * 32, plex_username="jo")])
    db.commit()

seen: list = []


def plex_tv(*, hop2="https://assets.plex.tv/avatars/don.png", image_type="image/png", body=PNG, me=200):
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(f"{req.url.host}{req.url.path}")
        if req.url.path == "/api/v2/user":
            ok = req.headers.get("X-Plex-Token") == "tok-own"
            return httpx.Response(me if ok else 401, json={"thumb": "https://plex.tv/users/abc/avatar?c=1"})
        if req.url.host == "plex.tv" and req.url.path == "/users/abc/avatar":
            return httpx.Response(302, headers={"location": hop2})
        if req.url.host == "secure.gravatar.com":
            return httpx.Response(302, headers={"location": "https://i0.wp.com/default.png"})
        return httpx.Response(200, content=body, headers={"content-type": image_type})
    return httpx.MockTransport(handler)


def pic(uid, transport):
    return asyncio.run(ua.picture(uid, transport=transport))


# ── Plex ────────────────────────────────────────────────────────────────────
ua.forget()
got = pic(1, plex_tv())
check("Plex: the picture plex.tv names for the user's own token, through its redirect",
      got == (PNG, "image/png") and "assets.plex.tv/avatars/don.png" in seen)
seen.clear()
check("...and a second ask is answered from memory", pic(1, plex_tv()) == got and not seen)

ua.forget()
check("an account without a picture gets Plex's default via Gravatar",
      pic(1, plex_tv(hop2="https://secure.gravatar.com/avatar/x?d=1")) == (PNG, "image/png"))

ua.forget()
seen.clear()
check("a member who never signed in with Plex has no token: no picture, plex.tv not asked",
      pic(2, plex_tv()) is None and not seen)

def fresh_hop(hop):
    ua.forget()
    seen.clear()
    return pic(1, plex_tv(hop2=hop)), any(hop.split("/")[2] in s for s in seen)


check.table("a hop that is not plex.tv / Gravatar over https is refused, never fetched", [
    ("another host", "https://evil.example/x.png", (None, False)),
    ("plain http", "http://assets.plex.tv/x.png", (None, False)),
    ("a metadata address", "https://169.254.169.254/latest", (None, False)),
    ("a look-alike host", "https://plex.tv.evil.example/x.png", (None, False)),
], fresh_hop)

ua.forget()
check("a page instead of an image is no picture", pic(1, plex_tv(image_type="text/html")) is None)
ua.forget()
check("...nor is one over 2 MB", pic(1, plex_tv(body=b"0" * (2 * 1024 * 1024 + 1))) is None)
ua.forget()
check("plex.tv refusing the token is no picture", pic(1, plex_tv(me=401)) is None)


def down(req):
    raise httpx.ConnectError("offline", request=req)


ua.forget()
t0 = time.monotonic()
check("an outage is no picture...", pic(1, httpx.MockTransport(down)) is None)
check("...remembered only briefly, unlike a real 'none'",
      ua._cache[1][0] <= time.monotonic() + ua._TTL_FAILED < t0 + ua._TTL_NONE)

# ── Jellyfin ────────────────────────────────────────────────────────────────
settings.MEDIA_SERVER_TYPE = "jellyfin"
settings.JELLYFIN_URL = "http://jf.test:8096"
settings.JELLYFIN_API_KEY = SecretStr("jf-key")


def jellyfin(status):
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(f"{req.url.host}{req.url.path}")
        if req.url.path == f"/Users/{'a' * 32}/Images/Primary" and 'Token="jf-key"' in req.headers.get(
                "Authorization", ""):
            return httpx.Response(status, content=PNG if status == 200 else b"",
                                  headers={"content-type": "image/jpeg"})
        return httpx.Response(500)
    return httpx.MockTransport(handler)


ua.forget()
check("Jellyfin: the user's primary image, asked with the server's key",
      pic(3, jellyfin(200)) == (PNG, "image/jpeg"))
ua.forget()
check("...and a user who set none (404) keeps the initial", pic(3, jellyfin(404)) is None)

# ── the route ───────────────────────────────────────────────────────────────
from fastapi import HTTPException  # noqa: E402
from src.routers.users import get_my_avatar  # noqa: E402

ua._cache.clear()
ua._cache[3] = (float("inf"), (PNG, "image/jpeg"))
r = asyncio.run(get_my_avatar(User(id=3)))
check("GET /me/avatar hands back the caller's picture, private and sandboxed",
      r.body == PNG and r.media_type == "image/jpeg"
      and r.headers["cache-control"].startswith("private") and r.headers["content-security-policy"] == "sandbox")
ua._cache[2] = (float("inf"), None)
try:
    asyncio.run(get_my_avatar(User(id=2)))
    status = 200
except HTTPException as e:
    status = e.status_code
check("...and 404 when there is none, so the page keeps the initial", status == 404)

check.done()
