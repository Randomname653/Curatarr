"""Connecting Curatarr to a Jellyfin server with nothing copied by hand (the
setup wizard; Settings -> Media server).

- **find**: Jellyfin answers a LAN broadcast ("who is JellyfinServer?", UDP
  7359), but a container's broadcast seldom leaves its bridge network - the
  TrueNAS app's did not answer it (2026-10-31). So more hosts are asked, on
  Jellyfin's usual ports, with ``GET /System/Info/Public``, which needs no
  key: the one the admin's browser reached Curatarr at (on a NAS, the NAS),
  the container's gateway (the Docker host), the hosts this install already
  talks to (Plex, the *arrs, SoulSync) and this machine. Every address passes the endpoint policy first: a
  broadcast answer is anyone's word. The address field stays for the rest.
- **connect**: an admin approves a Quick Connect code (or types their
  password once). With that session Curatarr makes its own API key -
  "Curatarr" under Dashboard -> API Keys - or finds the one an earlier
  connect made, then closes the session (``JellyfinAuth.admin_api_key``).

The key never reaches the browser: a connect leaves it here under a ticket,
and setup's /complete (or the Settings save) hands the ticket back.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import socket
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx

from src.services.http_client import tls_context
from src.services.media_server.base import normalize_jellyfin_id

logger = logging.getLogger(__name__)

DISCOVERY_PORT = 7359
PROBE_PORTS = (8096, 30013)      # Jellyfin's own default; the TrueNAS app's
_PROBE_TIMEOUT = 2.0
_BROADCAST_WAIT_S = 1.5

PENDING_TTL_S = 10 * 60          # Jellyfin drops a pending Quick Connect request itself
TICKET_TTL_S = 60 * 60           # the rest of the wizard, with a coffee
_MAX_ENTRIES = 200


@dataclass(frozen=True)
class FoundServer:
    url: str
    name: str
    version: str
    server_id: str


# ── find ────────────────────────────────────────────────────────────────────
def _broadcast(wait: float) -> list[str]:
    """The addresses that answered the discovery broadcast within ``wait`` s."""
    out: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.settimeout(0.25)
            s.sendto(b"who is JellyfinServer?", ("255.255.255.255", DISCOVERY_PORT))
            end = time.monotonic() + wait
            while time.monotonic() < end:
                try:
                    data, _peer = s.recvfrom(4096)
                except socket.timeout:
                    continue
                try:
                    addr = str(json.loads(data).get("Address") or "").rstrip("/")
                except (ValueError, AttributeError):
                    continue
                if addr.startswith(("http://", "https://")) and addr not in out:
                    out.append(addr)
    except OSError as e:
        logger.info("[jellyfin] discovery broadcast not sent: %s", e)
    return out


def _gateway() -> Optional[str]:
    """The default gateway on Linux - inside a bridge-network container,
    the host that publishes the other apps' ports."""
    try:
        for line in open("/proc/net/route", encoding="ascii").read().splitlines()[1:]:
            f = line.split()
            if len(f) > 2 and f[1] == "00000000":
                return socket.inet_ntoa(bytes.fromhex(f[2])[::-1])
    except (OSError, ValueError):
        pass
    return None


def _known_hosts() -> list[str]:
    """The gateway, the hosts this install already talks to, this machine."""
    from src.config import settings
    hosts: list[str] = [g] if (g := _gateway()) else []
    for raw in (settings.effective_plex_url, settings.RADARR_URL,
                settings.SONARR_URL, settings.LIDARR_URL, settings.SOULSYNC_URL):
        host = urlparse(str(raw or "")).hostname
        if host and host not in hosts:
            hosts.append(host)
    if "localhost" not in hosts:
        hosts.append("localhost")
    return hosts


def _probe_urls(hosts: list[str]) -> list[str]:
    return [f"http://{f'[{h}]' if ':' in h else h}:{p}" for h in hosts for p in PROBE_PORTS]


async def _allowed(url: str) -> bool:
    from src.services.endpoint_policy import resolved_refusal, validate_service_url
    try:
        validate_service_url(url)
    except ValueError:
        return False
    return not await resolved_refusal(url)


async def _probe(client: httpx.AsyncClient, url: str) -> Optional[FoundServer]:
    try:
        r = await client.get(f"{url}/System/Info/Public", headers={"Accept": "application/json"})
        info = r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    sid = normalize_jellyfin_id((info or {}).get("Id")) if isinstance(info, dict) else None
    if not sid or not info.get("Version"):
        return None
    return FoundServer(url=url, name=str(info.get("ServerName") or "Jellyfin"),
                       version=str(info["Version"]), server_id=sid)


async def find_servers(*, via_host: Optional[str] = None, wait: float = _BROADCAST_WAIT_S,
                       transport=None) -> list[FoundServer]:
    """Every Jellyfin server found, one entry per server (the first address
    that reached it): the configured one, ``via_host`` (the host the admin's
    browser reached Curatarr at), the known hosts, broadcast answers. The
    hosts are asked while the broadcast waits for answers."""
    from src.config import settings
    first = [str(settings.JELLYFIN_URL).rstrip("/")] if settings.JELLYFIN_URL else []
    if via_host:
        first += _probe_urls([via_host])
    asked: list[str] = []
    async with httpx.AsyncClient(verify=tls_context(), timeout=_PROBE_TIMEOUT,
                                 transport=transport) as c:
        async def probe_all(urls: list[str]) -> list[Optional[FoundServer]]:
            urls = [u for u in dict.fromkeys(urls) if u not in asked and await _allowed(u)]
            asked.extend(urls)
            return list(await asyncio.gather(*(_probe(c, u) for u in urls)))
        known, answered = await asyncio.gather(probe_all(first + _probe_urls(_known_hosts())),
                                               asyncio.to_thread(_broadcast, wait))
        found = known + await probe_all(answered)
    out, seen = [], set()
    for f in found:
        if f and f.server_id not in seen:
            seen.add(f.server_id)
            out.append(f)
    logger.info("[jellyfin] looked for servers at %d addresses: %d found", len(asked), len(out))
    return out


# ── pending sign-ins and finished connects ──────────────────────────────────
_pending: dict[str, tuple[str, str, str, str, float]] = {}   # id -> (nonce, secret, device, url, at)
_tickets: dict[str, tuple[str, str, float]] = {}             # ticket -> (url, api key, at)


def _trim(store: dict, ttl: float, at_index: int) -> None:
    now = time.time()
    for k in [k for k, v in store.items() if now - v[at_index] >= ttl]:
        store.pop(k, None)
    if len(store) >= _MAX_ENTRIES:
        for k, _ in sorted(store.items(), key=lambda kv: kv[1][at_index])[:_MAX_ENTRIES // 2]:
            store.pop(k, None)


def bind_pending(secret: str, device_id: str, url: str) -> tuple[str, str]:
    """``(login id, nonce)`` for a Quick Connect code just handed out."""
    _trim(_pending, PENDING_TTL_S, 4)
    login_id, nonce = secrets.token_urlsafe(16), secrets.token_urlsafe(24)
    _pending[login_id] = (nonce, secret, device_id, url, time.time())
    return login_id, nonce


def check_pending(login_id: str, nonce: str) -> Optional[tuple[str, str, str]]:
    """``(secret, device id, url)``, or None for an unknown, expired or
    foreign sign-in (the nonce proves the browser that asked)."""
    entry = _pending.get(login_id or "")
    if (entry is None or time.time() - entry[4] >= PENDING_TTL_S
            or not hmac.compare_digest((nonce or "").encode(), entry[0].encode())):
        return None
    return entry[1], entry[2], entry[3]


def drop_pending(login_id: str) -> None:
    _pending.pop(login_id, None)


def hold(url: str, api_key: str) -> str:
    _trim(_tickets, TICKET_TTL_S, 2)
    ticket = secrets.token_urlsafe(24)
    _tickets[ticket] = (url, api_key, time.time())
    return ticket


def held(ticket: str) -> Optional[tuple[str, str]]:
    """``(url, api key)`` a connect left under ``ticket``, while fresh."""
    entry = _tickets.get(ticket or "")
    if entry is None or time.time() - entry[2] >= TICKET_TTL_S:
        return None
    return entry[0], entry[1]


def release(ticket: str) -> None:
    _tickets.pop(ticket, None)
