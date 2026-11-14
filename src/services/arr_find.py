"""Finding Radarr, Sonarr, Lidarr and SoulSync on the LAN for the setup wizard.

The wizard's arr step was three empty forms (fresh-install test
2026-11-14). The apps answer their own web page without a key - the login
page too - and its <title> names the app, so the hosts worth asking are
asked on the apps' usual ports: the media server's host (on a NAS the arr
apps usually live next to Plex), the host the admin's browser reached
Curatarr at, the container's gateway, this machine. Every address passes the
endpoint policy first. The API key stays the user's to paste: the apps hand
it out only behind their own sign-in; the wizard links to the page that
shows it.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import httpx

from src.services.http_client import tls_context

logger = logging.getLogger(__name__)

# Upstream defaults first, then the TrueNAS SCALE apps' defaults.
PORTS = {
    "radarr": (7878, 30025),
    "sonarr": (8989, 30113),
    "lidarr": (8686, 30071),
    "soulsync": (8008, 12279),
}
_TIMEOUT = 2.0
_TITLE = re.compile(r"<title>\s*([^<]{1,80})</title>", re.I)
_HOST = re.compile(r"^[A-Za-z0-9._:\-\[\]]{1,253}$")


def clean_host(raw: Optional[str]) -> Optional[str]:
    """A bare host name or address, or None for anything else."""
    h = (raw or "").strip().strip("[]")
    return h if h and _HOST.match(h) else None


def which_app(html: str) -> Optional[str]:
    """'radarr' / 'sonarr' / 'lidarr' / 'soulsync' when the page's title names one."""
    m = _TITLE.search(html or "")
    title = (m.group(1) if m else "").lower()
    for app in PORTS:
        if app in title:
            return app
    return None


async def _probe(client: httpx.AsyncClient, url: str) -> Optional[str]:
    from src.services.media_server.jellyfin_connect import _allowed
    if not await _allowed(url):
        return None
    try:
        r = await client.get(f"{url}/", follow_redirects=True)
    except httpx.HTTPError:
        return None
    if r.status_code >= 400:
        return None
    return which_app(r.text[:4000])


async def find_arrs(hosts: list[str], *, transport=None) -> dict:
    """``{app: url}`` for each arr app found, the first host that answered.
    ``hosts`` in the order to prefer."""
    from src.services.media_server.jellyfin_connect import _gateway
    order: list[str] = []
    for h in [*hosts, _gateway(), "localhost"]:
        h = clean_host(h)
        if h and h not in order:
            order.append(h)
    cands = [(app, f"http://{f'[{h}]' if ':' in h else h}:{p}")
             for h in order for app, ports in PORTS.items() for p in ports]
    async with httpx.AsyncClient(verify=tls_context(), timeout=_TIMEOUT, transport=transport) as c:
        seen = await asyncio.gather(*(_probe(c, url) for _, url in cands))
    found: dict = {}
    for (expected, url), app in zip(cands, seen):
        # The title decides, not the port: a port can be remapped.
        if app and app not in found:
            found[app] = url
    logger.info("[setup] looked for arr apps at %d addresses: %s", len(cands),
                ", ".join(sorted(found)) or "none")
    return found
