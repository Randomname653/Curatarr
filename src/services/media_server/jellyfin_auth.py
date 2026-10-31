"""Signing in with a Jellyfin account (docs/JELLYFIN_DESIGN.md §3, phase 2).

Two ways, both against the ONE configured Jellyfin server, so a successful
sign-in proves the account belongs to it (no outside accounts exist, unlike
plex.tv):

- **Quick Connect** (preferred, no password passes through Curatarr):
  ``POST /QuickConnect/Initiate`` hands out a code and a secret; the user
  approves the code in any signed-in Jellyfin app; ``GET /QuickConnect/Connect``
  says when; ``POST /Users/AuthenticateWithQuickConnect`` turns the secret into
  a session. Off by default on a Jellyfin server: ``GET /QuickConnect/Enabled``.
- **Password**: ``POST /Users/AuthenticateByName``. The password goes to the
  server once and is never stored or logged.

Either way the Jellyfin session the server opens for Curatarr is only used to
read who signed in (and, when an admin connects the server in setup, to make
Curatarr's API key), then closed (``POST /Sessions/Logout``): Curatarr keeps no
user token. Each sign-in runs under its own device id, because Jellyfin ties a
Quick Connect secret and a session to the device that asked.
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Optional

import httpx

from src.services.http_client import tls_context
from src.services.media_server.base import normalize_jellyfin_id

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0
KEY_APP_NAME = "Curatarr"      # the API key setup makes, under Dashboard -> API Keys


class JellyfinAuthError(RuntimeError):
    """The server did not answer, or answered with an error."""


class JellyfinAuthRefused(JellyfinAuthError):
    """Wrong name or password, or a Quick Connect secret the server refuses."""


@dataclass(frozen=True)
class SignedIn:
    user_key: str            # the Jellyfin user id, stored spelling
    name: str
    is_admin: bool
    server_id: str


def new_device_id() -> str:
    return f"curatarr-login-{secrets.token_hex(8)}"


class JellyfinAuth:
    def __init__(self, base_url: str, *, device_id: Optional[str] = None,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self._base = str(base_url).rstrip("/")
        self.device_id = device_id or new_device_id()
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(verify=tls_context(), timeout=_TIMEOUT, transport=self._transport)

    def _headers(self, token: Optional[str] = None) -> dict:
        auth = (f'MediaBrowser Client="Curatarr", Device="Curatarr sign-in", '
                f'DeviceId="{self.device_id}", Version="1"')
        if token:
            auth += f', Token="{token}"'
        return {"Accept": "application/json", "Authorization": auth}

    async def _call(self, method: str, path: str, *, params=None, json=None,
                    token: Optional[str] = None, refused=(401, 403)):
        try:
            async with self._client() as c:
                r = await c.request(method, f"{self._base}{path}", params=params, json=json,
                                    headers=self._headers(token))
        except httpx.HTTPError as e:
            raise JellyfinAuthError(f"Jellyfin {path}: {type(e).__name__}") from e
        if r.status_code in refused:
            raise JellyfinAuthRefused(f"Jellyfin {path}: HTTP {r.status_code}")
        if r.status_code not in (200, 204):
            raise JellyfinAuthError(f"Jellyfin {path}: HTTP {r.status_code}")
        return r.json() if r.content else None

    # ── Quick Connect ───────────────────────────────────────────────────────
    async def quick_connect_enabled(self) -> bool:
        return bool(await self._call("GET", "/QuickConnect/Enabled"))

    async def initiate(self) -> tuple[str, str]:
        """``(secret, code)``. The code is what the user types into their
        Jellyfin app; the secret never leaves Curatarr's server."""
        body = await self._call("POST", "/QuickConnect/Initiate", refused=(401,))
        secret, code = (body or {}).get("Secret"), (body or {}).get("Code")
        if not secret or not code:
            raise JellyfinAuthError("Quick Connect answered without a code")
        return str(secret), str(code)

    async def approved(self, secret: str) -> bool:
        body = await self._call("GET", "/QuickConnect/Connect", params={"Secret": secret},
                                refused=(401, 403, 404))
        return bool((body or {}).get("Authenticated"))

    async def finish_quick_connect(self, secret: str) -> SignedIn:
        body = await self._call("POST", "/Users/AuthenticateWithQuickConnect", json={"Secret": secret})
        return await self._signed_in(body)

    # ── password ────────────────────────────────────────────────────────────
    async def password(self, username: str, password: str) -> SignedIn:
        body = await self._call("POST", "/Users/AuthenticateByName",
                                json={"Username": username, "Pw": password})
        return await self._signed_in(body)

    # ── Curatarr's own API key (setup; jellyfin_connect.py) ─────────────────
    async def admin_api_key(self, *, secret: Optional[str] = None, username: str = "",
                            password: str = "") -> tuple[SignedIn, str]:
        """An admin's Quick Connect ``secret`` (or name and password) turned
        into Curatarr's API key: the one named "Curatarr" an earlier connect
        made, else a new one. The admin's session only lives for these
        calls. Refused for an account that is no administrator."""
        if secret:
            body = await self._call("POST", "/Users/AuthenticateWithQuickConnect", json={"Secret": secret})
        else:
            body = await self._call("POST", "/Users/AuthenticateByName",
                                    json={"Username": username, "Pw": password})
        token = (body or {}).get("AccessToken")
        try:
            who = self._who(body)
            if not who.is_admin:
                raise JellyfinAuthRefused(f"{who.name} is no Jellyfin administrator")
            if not token:
                raise JellyfinAuthError("Jellyfin answered the sign-in without a session")
            return who, await self._own_key(token)
        finally:
            if token:
                await self._logout(token)

    async def _own_key(self, token: str) -> str:
        def newest(body) -> Optional[str]:
            mine = [k for k in (body or {}).get("Items") or []
                    if k.get("AppName") == KEY_APP_NAME and k.get("AccessToken")]
            mine.sort(key=lambda k: str(k.get("DateCreated") or ""), reverse=True)
            return str(mine[0]["AccessToken"]) if mine else None
        key = newest(await self._call("GET", "/Auth/Keys", token=token))
        if not key:
            await self._call("POST", "/Auth/Keys", params={"app": KEY_APP_NAME}, token=token)
            key = newest(await self._call("GET", "/Auth/Keys", token=token))
            if key:
                logger.info("[auth] made a Jellyfin API key named %r", KEY_APP_NAME)
        if not key:
            raise JellyfinAuthError("Jellyfin made no API key")
        return key

    # ── both ────────────────────────────────────────────────────────────────
    async def _signed_in(self, body) -> SignedIn:
        token = (body or {}).get("AccessToken")
        if token:
            await self._logout(token)
        return self._who(body)

    @staticmethod
    def _who(body) -> SignedIn:
        user = (body or {}).get("User") or {}
        key = normalize_jellyfin_id(user.get("Id"))
        name = str(user.get("Name") or "").strip()
        if not key or not name:
            raise JellyfinAuthError("Jellyfin answered the sign-in without a user")
        return SignedIn(user_key=key, name=name,
                        is_admin=bool((user.get("Policy") or {}).get("IsAdministrator")),
                        server_id=normalize_jellyfin_id((body or {}).get("ServerId")) or "")

    async def _logout(self, token: str) -> None:
        """Close the session the sign-in opened: Curatarr keeps no user
        token, so a session left open would be one nobody can end."""
        try:
            await self._call("POST", "/Sessions/Logout", token=token)
        except JellyfinAuthError as e:
            logger.warning("[auth] Jellyfin sign-in session not closed (%s); it expires "
                           "on the server's own schedule", e)
