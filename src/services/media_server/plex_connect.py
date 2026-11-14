"""Connecting Curatarr to a Plex server with nothing copied by hand (the
setup wizard).

The wizard used to ask for the server's address and an X-Plex-Token copied
out of an XML page, then sent the owner to plex.tv anyway to sign in
(fresh-install test 2026-11-14). Now one plex.tv sign-in does both:

- **sign in**: a PIN from plex.tv, approved by the owner in a new tab (or
  at plex.tv/link). The account's token stays here, held under the login.
- **pick**: plex.tv lists the servers that account owns, each with its
  own access token and its addresses. The first address that answers
  ``/identity`` with that token is the one Curatarr keeps - LAN ones first,
  plain http before the plex.direct name (which some routers' DNS refuses),
  never a relay.
- **finish**: setup's /complete takes the ticket, writes the server's
  address and token, and signs the owner in with the same plex.tv token -
  no second sign-in.

No token reaches the browser: it holds a login id and a nonce, then a
ticket, like the Jellyfin connect (jellyfin_connect.py).
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from src.services.http_client import tls_context

logger = logging.getLogger(__name__)

PENDING_TTL_S = 15 * 60          # plex.tv keeps a strong PIN for about this long
TICKET_TTL_S = 60 * 60           # the rest of the wizard, with a coffee
_MAX_ENTRIES = 200
_PROBE_TIMEOUT = 3.0
RESOURCES_URL = "https://clients.plex.tv/api/v2/resources"


def headers(client_id: str, token: Optional[str] = None) -> dict:
    h = {"Accept": "application/json", "X-Plex-Client-Identifier": client_id,
         "X-Plex-Product": "Curatarr", "X-Plex-Version": "1.0"}
    if token:
        h["X-Plex-Token"] = token
    return h


def setup_client_id() -> str:
    """The client id the wizard signs in with. A fresh install gets its own
    (write_env keeps it: the owner's token is issued to it); an established
    one keeps the id its users' tokens belong to."""
    from src.config import settings
    from src.services.setup_wizard import DEFAULT_PLEX_CLIENT_ID
    current = (getattr(settings, "PLEX_CLIENT_ID", "") or "").strip() or DEFAULT_PLEX_CLIENT_ID
    if current != DEFAULT_PLEX_CLIENT_ID or settings.effective_jwt_secret:
        return current
    return f"curatarr-{secrets.token_hex(16)}"


@dataclass
class Server:
    server_id: str
    name: str
    version: str
    access_token: str
    candidates: list[str] = field(default_factory=list)
    url: Optional[str] = None     # the address that answered


def candidate_urls(connections: list) -> list[str]:
    """The addresses worth trying, best first: on the LAN before remote, a
    plain http address before the plex.direct name, never a relay."""
    local_plain, local_named, remote = [], [], []
    for c in connections or []:
        if not isinstance(c, dict) or c.get("relay"):
            continue
        addr, port, uri = c.get("address"), c.get("port"), c.get("uri")
        if c.get("IPv6"):
            continue
        if c.get("local"):
            if addr and port:
                local_plain.append(f"http://{addr}:{port}")
            if uri:
                local_named.append(str(uri).rstrip("/"))
        elif uri:
            remote.append(str(uri).rstrip("/"))
    return list(dict.fromkeys(local_plain + local_named + remote))


def owned_servers(resources: list) -> list[Server]:
    """The Plex Media Servers among plex.tv's resources that the signed-in
    account OWNS: only the owner may create Curatarr's admin account."""
    out = []
    for r in resources or []:
        if not isinstance(r, dict):
            continue
        if "server" not in str(r.get("provides") or "") or not r.get("owned"):
            continue
        token = r.get("accessToken")
        sid = r.get("clientIdentifier")
        if not token or not sid:
            continue
        out.append(Server(server_id=str(sid), name=str(r.get("name") or "Plex Media Server"),
                          version=str(r.get("productVersion") or ""), access_token=str(token),
                          candidates=candidate_urls(r.get("connections"))))
    return out


async def _answers(client: httpx.AsyncClient, url: str, token: str) -> bool:
    from src.services.endpoint_policy import resolved_refusal, validate_service_url
    try:
        validate_service_url(url)
        if await resolved_refusal(url):
            return False
        r = await client.get(f"{url}/identity",
                             headers={"Accept": "application/json", "X-Plex-Token": token})
        return r.status_code == 200
    except (ValueError, httpx.HTTPError):
        return False


async def reachable(server: Server, *, transport=None) -> Optional[str]:
    """The first candidate address that answers with the server's token."""
    async with httpx.AsyncClient(verify=tls_context(), timeout=_PROBE_TIMEOUT,
                                 transport=transport) as c:
        for url in server.candidates:
            if await _answers(c, url, server.access_token):
                return url
    return None


async def servers_for(user_token: str, client_id: str, *, transport=None) -> list[Server]:
    """The account's own servers, each with the address Curatarr reaches it
    at (None when no address answered from here)."""
    async with httpx.AsyncClient(verify=tls_context(), timeout=10, transport=transport) as c:
        r = await c.get(RESOURCES_URL, headers=headers(client_id, user_token),
                        params={"includeHttps": 1, "includeRelay": 0})
    if r.status_code != 200:
        raise RuntimeError(f"plex.tv resources: HTTP {r.status_code}")
    servers = owned_servers(r.json())
    urls = await asyncio.gather(*(reachable(s, transport=transport) for s in servers))
    for s, u in zip(servers, urls):
        s.url = u
    logger.info("[plex-connect] %d owned server(s), %d reachable",
                len(servers), sum(1 for s in servers if s.url))
    return servers


# ── pending sign-ins and finished connects ──────────────────────────────────
_pending: dict[str, tuple] = {}   # login id -> (nonce, pin id, client id, at)
_tickets: dict[str, tuple] = {}   # ticket -> (user token, client id, servers, at)


def _trim(store: dict, ttl: float) -> None:
    now = time.time()
    for k in [k for k, v in store.items() if now - v[-1] >= ttl]:
        store.pop(k, None)
    if len(store) >= _MAX_ENTRIES:
        for k, _ in sorted(store.items(), key=lambda kv: kv[1][-1])[:_MAX_ENTRIES // 2]:
            store.pop(k, None)


def bind_pending(pin_id: int, client_id: str) -> tuple[str, str]:
    _trim(_pending, PENDING_TTL_S)
    login_id, nonce = secrets.token_urlsafe(16), secrets.token_urlsafe(24)
    _pending[login_id] = (nonce, int(pin_id), client_id, time.time())
    return login_id, nonce


def check_pending(login_id: str, nonce: str) -> Optional[tuple[int, str]]:
    """``(pin id, client id)``, or None for an unknown, expired or foreign
    sign-in (the nonce proves the browser that asked)."""
    entry = _pending.get(login_id or "")
    if (entry is None or time.time() - entry[-1] >= PENDING_TTL_S
            or not hmac.compare_digest((nonce or "").encode(), entry[0].encode())):
        return None
    return entry[1], entry[2]


def drop_pending(login_id: str) -> None:
    _pending.pop(login_id, None)


def hold(user_token: str, client_id: str, servers: list[Server]) -> str:
    _trim(_tickets, TICKET_TTL_S)
    ticket = secrets.token_urlsafe(24)
    _tickets[ticket] = (user_token, client_id, list(servers), time.time())
    return ticket


def held(ticket: str) -> Optional[tuple[str, str, list]]:
    """``(user token, client id, servers)`` a sign-in left under ``ticket``."""
    entry = _tickets.get(ticket or "")
    if entry is None or time.time() - entry[-1] >= TICKET_TTL_S:
        return None
    return entry[0], entry[1], entry[2]


def release(ticket: str) -> None:
    _tickets.pop(ticket, None)


def public(servers: list[Server]) -> list[dict]:
    """What the browser may see of the servers: no token."""
    return [{"server_id": s.server_id, "name": s.name, "version": s.version,
             "url": s.url, "reachable": bool(s.url)} for s in servers]
