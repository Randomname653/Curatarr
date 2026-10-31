"""Signing in with Jellyfin (docs/JELLYFIN_DESIGN.md, phase 2).

- the login screen learns which server signs people in (/api/auth/server);
- Quick Connect: the code goes to the browser, the secret stays here; only
  the browser that asked can poll (nonce); one code, one session; the whole
  exchange runs under one device id;
- password: checked by the Jellyfin server, never stored, wrong ones budgeted;
- the FIRST account must be a Jellyfin administrator; later accounts are
  members; a deactivated account stays out;
- the session Jellyfin opens for the sign-in is closed again;
- on a Jellyfin install the Plex PIN route is gone, and the reverse.

    python tests/test_jellyfin_signin.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import Base, User  # noqa: E402
Base.metadata.create_all(conn_mod.engine)

from fastapi import BackgroundTasks, HTTPException  # noqa: E402

from media_server_fixtures import JellyfinAuthServer  # noqa: E402
from src.config import settings  # noqa: E402
from src.routers import auth  # noqa: E402
from src.services import rate_limit  # noqa: E402
import src.services.media_server.jellyfin_auth as ja  # noqa: E402

from _checks import check  # noqa: E402


def req(host="192.168.1.20", headers=None):
    return SimpleNamespace(client=SimpleNamespace(host=host), headers=headers or {})


def call(coro):
    """(status, body) of a handler call."""
    try:
        return 200, asyncio.run(coro)
    except HTTPException as e:
        return e.status_code, e.detail


def users():
    with conn_mod.get_db_session() as db:
        return {u.plex_username: (u.plex_user_id, u.is_admin, u.plex_token)
                for u in db.query(User).all()}


def reset_users():
    with conn_mod.get_db_session() as db:
        db.query(User).delete()
        db.commit()


from pydantic import SecretStr  # noqa: E402
settings.JWT_SECRET = SecretStr("x" * 64)
_Orig = ja.JellyfinAuth
SERVER = JellyfinAuthServer()
ja.JellyfinAuth = lambda url, device_id=None: _Orig(url, device_id=device_id,
                                                    transport=SERVER.transport())

print("\n-- which server signs in --")
settings.MEDIA_SERVER_TYPE = None
check("unset: Plex", call(auth.auth_server()) == (200, {"kind": "plex"}))
check("on a Plex install the Jellyfin routes are gone",
      call(auth.jellyfin_quick_connect(req()))[0] == 404
      and call(auth.jellyfin_password_login(auth.JellyfinPasswordLogin(username="jane", password="x"),
                                            req(), BackgroundTasks()))[0] == 404)
settings.MEDIA_SERVER_TYPE = "jellyfin"
settings.JELLYFIN_URL = "http://jf:8096"
check("jellyfin: the kind, and whether Quick Connect is on",
      call(auth.auth_server()) == (200, {"kind": "jellyfin", "quick_connect": True}))
check("on a Jellyfin install the Plex PIN route is gone", call(auth.request_plex_pin(req()))[0] == 404)

print("\n-- the first account must be a Jellyfin administrator --")
st, body = call(auth.jellyfin_password_login(auth.JellyfinPasswordLogin(username="Sam", password="pw"),
                                             req(), BackgroundTasks()))
check("a member cannot create the first account", st == 403 and "administrator" in body and users() == {})

print("\n-- Quick Connect --")
st, start = call(auth.jellyfin_quick_connect(req()))
check("the browser gets the code, a login id and a nonce - never the secret",
      st == 200 and start["code"] == "482913" and start["login_id"] and start["nonce"]
      and "e1f2" not in str(start))
check("polling without the nonce is refused",
      call(auth.jellyfin_quick_connect_poll(start["login_id"], req(headers={"X-Jellyfin-Login-Nonce": "x"}),
                                            BackgroundTasks()))[0] == 403)
nonce_hdr = {"X-Jellyfin-Login-Nonce": start["nonce"]}
check("before approval: pending",
      call(auth.jellyfin_quick_connect_poll(start["login_id"], req(headers=nonce_hdr),
                                            BackgroundTasks())) == (200, {"status": "pending"}))
SERVER.approved = True
st, ok = call(auth.jellyfin_quick_connect_poll(start["login_id"], req(headers=nonce_hdr), BackgroundTasks()))
check("after approval: a session for the admin, keyed by the Jellyfin user id, no token kept",
      st == 200 and ok["status"] == "ok" and ok["token"] and ok["user"]["is_admin"] is True
      and users() == {"jane": ("b0c1d2e3f405162738495a6b7c8d9e0f", True, None)})
check("every session Jellyfin opened for a sign-in is closed - the refused one too",
      SERVER.logouts == ["tok-sam", "tok-jane"])
qc_devices = [d for d, (p, _) in zip(SERVER.devices, SERVER.bodies) if p.startswith(("/QuickConnect/I", "/QuickConnect/C", "/Users/AuthenticateWithQuickConnect"))]
check("initiate, poll and exchange ran under one device id",
      len(set(qc_devices)) == 1 and qc_devices[0].startswith("curatarr-login-"))
check("one code, one session: the binding is gone",
      call(auth.jellyfin_quick_connect_poll(start["login_id"], req(headers=nonce_hdr),
                                            BackgroundTasks()))[0] == 403)
SERVER.approved = False

print("\n-- password --")
pw = auth.JellyfinPasswordLogin
st, ok = call(auth.jellyfin_password_login(pw(username="jane", password="hunter2"), req(), BackgroundTasks()))
check("the admin signs in again with a password: the same account",
      st == 200 and len(users()) == 1 and ok["user"]["is_admin"] is True)
st, ok = call(auth.jellyfin_password_login(pw(username="Sam", password="pw"), req(), BackgroundTasks()))
check("a second account is a member", st == 200 and users()["Sam"][1] is False)
check("no password was stored, and it went only to AuthenticateByName",
      all("Pw" not in b for p, b in SERVER.bodies if p != "/Users/AuthenticateByName")
      and not any("hunter2" in str(v) for v in users().values()))
check("a wrong password is a 401",
      call(auth.jellyfin_password_login(pw(username="jane", password="nope"), req(), BackgroundTasks()))[0] == 401)
codes = [call(auth.jellyfin_password_login(pw(username="jane", password=f"guess{i}"), req("10.0.0.9"),
                                           BackgroundTasks()))[0] for i in range(12)]
check("wrong passwords are budgeted per client (429 after ten)", codes[:10] == [401] * 10 and codes[-1] == 429)
check("...and a locked client stays locked even with the right password",
      call(auth.jellyfin_password_login(pw(username="jane", password="hunter2"), req("10.0.0.9"),
                                        BackgroundTasks()))[0] == 429)
rate_limit.reset()
with conn_mod.get_db_session() as db:
    db.query(User).filter(User.plex_username == "Sam").update({"is_active": False})
    db.commit()
check("a deactivated account stays out",
      call(auth.jellyfin_password_login(pw(username="Sam", password="pw"), req(), BackgroundTasks()))[0] == 403)

print("\n-- Quick Connect off, server down --")
SERVER.qc_enabled = False
auth._qc_enabled_cache[:] = [None, 0.0]
check("Quick Connect off: the login screen hears it, starting it says why",
      call(auth.auth_server())[1]["quick_connect"] is False
      and call(auth.jellyfin_quick_connect(req("10.1.1.1")))[0] == 409)
settings.JELLYFIN_URL = None
check("Jellyfin not configured: 503", call(auth.jellyfin_quick_connect(req("10.1.1.2")))[0] == 503)
settings.MEDIA_SERVER_TYPE = None

check.done()
