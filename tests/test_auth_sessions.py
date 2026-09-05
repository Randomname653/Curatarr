"""A used session never expires mid-click; an idle one dies in a week.

Live failure: JWTs carried a hard 24h ``exp`` and nothing ever renewed
them, so the owner was logged out exactly one day after each login — a
200 and a 401 six seconds apart on the same connection. The fix is a
7-day token plus in-band rolling refresh: any request carrying a valid
token older than a day gets a fresh one in the
``x-curatarr-refreshed-token`` response header. Clients that ignore the
header keep a fixed 7-day expiry; nothing about 401 handling changes.
"""

import asyncio
import datetime as dt
import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import jwt as jose_jwt  # PyJWT

from src.config import settings
from src.middleware import TokenRefreshMiddleware
from src.routers.auth import _create_jwt


def _mint(age_seconds: int) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    return jose_jwt.encode(
        {"sub": "1", "admin": True,
         "iat": now - dt.timedelta(seconds=age_seconds),
         "exp": now + dt.timedelta(days=5)},
        settings.effective_jwt_secret, algorithm="HS256")


def _run(token: str | None):
    """Drive the middleware with a minimal ASGI app; return response headers."""
    headers = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message):
        if message["type"] == "http.response.start":
            headers.extend(message.get("headers", []))

    scope = {"type": "http", "method": "GET", "path": "/api/x",
             "headers": ([(b"authorization", b"Bearer " + token.encode())]
                         if token else [])}
    asyncio.run(TokenRefreshMiddleware(app)(scope, None, send))
    return dict(headers)


def test_tokens_now_live_seven_days():
    payload = jose_jwt.decode(_create_jwt(1, True),
                              settings.effective_jwt_secret, algorithms=["HS256"])
    lifetime = payload["exp"] - payload["iat"]
    assert lifetime == 7 * 24 * 3600, lifetime


def test_a_day_old_token_is_silently_refreshed():
    hdrs = _run(_mint(age_seconds=2 * 24 * 3600))
    fresh = hdrs.get(b"x-curatarr-refreshed-token")
    assert fresh, "old-but-valid token must earn a replacement"
    payload = jose_jwt.decode(fresh.decode(), settings.effective_jwt_secret,
                              algorithms=["HS256"])
    assert payload["sub"] == "1" and payload["admin"] is True
    assert payload["exp"] - payload["iat"] == 7 * 24 * 3600


def test_a_young_token_is_left_alone():
    hdrs = _run(_mint(age_seconds=3600))
    assert b"x-curatarr-refreshed-token" not in hdrs


def test_garbage_and_absent_tokens_pass_through_untouched():
    assert b"x-curatarr-refreshed-token" not in _run("not.a.jwt")
    assert b"x-curatarr-refreshed-token" not in _run(None)


def test_poster_bytes_are_cached_as_the_immutables_they_are():
    """TMDB/Deezer/TVDB poster URLs are content-addressed — the bytes behind
    a URL never change, so view switches must not re-request every image."""
    src = (_ROOT / "src" / "routers" / "image_proxy.py").read_text(encoding="utf-8")
    assert "max-age=31536000, immutable" in src
    assert "max-age=86400" not in src
