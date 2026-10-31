"""Connecting a Jellyfin server in setup without copying a key (2026-10-31).

The wizard used to ask for the address and a pasted API key. Now it finds
the server (the TrueNAS app ignores the discovery broadcast, so the hosts
Curatarr already knows are asked too), an admin approves a Quick Connect
code, and Curatarr makes its own key - or reuses the one it made before -
which stays on the server under a ticket.

    python tests/test_jellyfin_connect.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

from src.services.media_server import jellyfin_connect as jc  # noqa: E402
from src.services.media_server.jellyfin_auth import (JellyfinAuth, JellyfinAuthRefused,  # noqa: E402
                                                     KEY_APP_NAME)

from _checks import check  # noqa: E402


SID = "f" * 32

# ── find ────────────────────────────────────────────────────────────────────
asked = []


def find_handler(req: httpx.Request) -> httpx.Response:
    asked.append(f"{req.url.host}:{req.url.port}")
    if req.url.path == "/System/Info/Public" and req.url.host in ("10.0.0.5", "10.0.0.9"):
        if req.url.host == "10.0.0.5" and req.url.port != 30013:
            return httpx.Response(404)
        return httpx.Response(200, json={"Id": SID, "ServerName": "HomeNAS", "Version": "10.11.0",
                                         "ProductName": "Jellyfin Server"})
    if req.url.host == "10.0.0.7":      # some other web server on the port
        return httpx.Response(200, json={"hello": "world"})
    return httpx.Response(404)


saved = (jc._broadcast, jc._known_hosts)
jc._broadcast = lambda wait: ["http://10.0.0.9:8096", "http://169.254.169.254:8096"]
jc._known_hosts = lambda: ["10.0.0.5", "10.0.0.7"]
try:
    found = asyncio.run(jc.find_servers(transport=httpx.MockTransport(find_handler)))
    # A fresh install knows no other host: the one the browser came in on
    # (the NAS that runs both apps) still finds it.
    jc._broadcast, jc._known_hosts = (lambda wait: []), (lambda: [])
    via = asyncio.run(jc.find_servers(via_host="10.0.0.5", transport=httpx.MockTransport(find_handler)))
finally:
    jc._broadcast, jc._known_hosts = saved
check("a fresh install finds Jellyfin on the host its page was loaded from",
      [f.url for f in via] == ["http://10.0.0.5:30013"])
check("a server on the TrueNAS app's port is found at a host Curatarr already knows",
      [f.url for f in found] == ["http://10.0.0.5:30013"] and found[0].name == "HomeNAS")
check("...the same server answering the broadcast is listed once",
      len(found) == 1 and "10.0.0.9:8096" in asked)
check("...a web server that is no Jellyfin is not listed", all(f.server_id == SID for f in found))
check("...and a broadcast answer naming the metadata address is never asked",
      not any(a.startswith("169.254.") for a in asked))

# ── the key ─────────────────────────────────────────────────────────────────
TOKEN = "t" * 32


def key_server(keys, *, admin=True):
    log = []

    def handler(req: httpx.Request) -> httpx.Response:
        auth = req.headers.get("Authorization", "")
        log.append((req.method, req.url.path, dict(req.url.params), f'Token="{TOKEN}"' in auth))
        if req.url.path == "/Users/AuthenticateWithQuickConnect":
            return httpx.Response(200, json={"AccessToken": TOKEN, "ServerId": SID, "User": {
                "Id": "a" * 32, "Name": "admin", "Policy": {"IsAdministrator": admin}}})
        if req.url.path == "/Auth/Keys" and req.method == "GET":
            return httpx.Response(200, json={"Items": list(keys)})
        if req.url.path == "/Auth/Keys" and req.method == "POST":
            keys.append({"AccessToken": "new" + "0" * 29, "AppName": req.url.params["app"],
                         "DateCreated": "2026-10-31T12:00:00Z"})
            return httpx.Response(204)
        if req.url.path == "/Sessions/Logout":
            return httpx.Response(204)
        return httpx.Response(404)
    return log, handler


log, h = key_server([{"AccessToken": "other" + "0" * 27, "AppName": "Sonarr", "DateCreated": "2026-01-01"}])
who, key = asyncio.run(JellyfinAuth("http://jf:8096", transport=httpx.MockTransport(h))
                       .admin_api_key(secret="s"))
check("no Curatarr key yet: one is made, named Curatarr, and handed back",
      key.startswith("new") and any(m == "POST" and q == {"app": KEY_APP_NAME} for m, _p, q, _t in log))
check("...with the admin's session, which is closed afterwards",
      log[-1][1] == "/Sessions/Logout" and all(t for _m, p, _q, t in log if p == "/Auth/Keys"))
check("...and the admin is named", who.name == "admin" and who.is_admin)

log, h = key_server([{"AccessToken": "old1" + "0" * 28, "AppName": KEY_APP_NAME, "DateCreated": "2026-03-01"},
                     {"AccessToken": "old2" + "0" * 28, "AppName": KEY_APP_NAME, "DateCreated": "2026-09-01"}])
_who, key = asyncio.run(JellyfinAuth("http://jf:8096", transport=httpx.MockTransport(h))
                        .admin_api_key(secret="s"))
check("a reconnect reuses the newest Curatarr key instead of piling up new ones",
      key.startswith("old2") and not any(m == "POST" and p == "/Auth/Keys" for m, p, _q, _t in log))

log, h = key_server([], admin=False)
try:
    asyncio.run(JellyfinAuth("http://jf:8096", transport=httpx.MockTransport(h)).admin_api_key(secret="s"))
    refused = False
except JellyfinAuthRefused as e:
    refused = "administrator" in str(e)
check("an account that is no administrator is refused, and makes no key",
      refused and not any(p == "/Auth/Keys" for _m, p, _q, _t in log))
check("...its session is still closed", log[-1][1] == "/Sessions/Logout")

# ── pending codes and tickets ───────────────────────────────────────────────
lid, nonce = jc.bind_pending("secret", "dev", "http://jf:8096")
check("a pending code is only the asking browser's", jc.check_pending(lid, "wrong") is None
      and jc.check_pending(lid, nonce) == ("secret", "dev", "http://jf:8096"))
ticket = jc.hold("http://jf:8096", "k" * 32)
check("a ticket holds the address and key until it is released",
      jc.held(ticket) == ("http://jf:8096", "k" * 32))
jc.release(ticket)
check("...and not after", jc.held(ticket) is None and jc.held("") is None)

# ── /complete takes the ticket ──────────────────────────────────────────────
import src.routers.setup as setup_router  # noqa: E402

written = []
saved_w = setup_router._write_env_or_400
setup_router._write_env_or_400 = lambda cfg: written.append(dict(cfg))
saved_b = setup_router.build_ollama_models


async def _no_build(*a, **k):
    return {}


setup_router.build_ollama_models = _no_build
try:
    from fastapi import BackgroundTasks
    ticket = jc.hold("http://10.0.0.5:30013", "k" * 32)
    req = setup_router.SetupCompleteRequest(media_server_type="jellyfin", jellyfin_ticket=ticket)
    asyncio.run(setup_router.complete_setup(req, BackgroundTasks(), None))
    cfg = written[-1] if written else {}
    check("finishing setup with a connect writes its address and key",
          cfg.get("jellyfin_url") == "http://10.0.0.5:30013" and cfg.get("jellyfin_api_key") == "k" * 32)
    check("...not the ticket, which is spent", "jellyfin_ticket" not in cfg and jc.held(ticket) is None)
    try:
        asyncio.run(setup_router.complete_setup(
            setup_router.SetupCompleteRequest(media_server_type="jellyfin", jellyfin_ticket="gone"),
            BackgroundTasks(), None))
        expired = False
    except Exception as e:  # noqa: BLE001 - the HTTPException is the answer under test
        expired = getattr(e, "status_code", None) == 422
    check("an expired ticket says so instead of saving a Jellyfin without a key", expired)
finally:
    setup_router._write_env_or_400, setup_router.build_ollama_models = saved_w, saved_b

check.done()
