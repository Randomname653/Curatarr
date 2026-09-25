"""Auth boundary audit, 2026-09: the gates that must honour revocation, the
setup-code guess budget, and Plex PINs bound to the browser that made them.

Pins:
  - require_admin_or_first_run (the setup wizard's gate: .env rewrite, model
    builds, connection tests) rejects a logged-out or deactivated admin's
    token - it used to decode the JWT itself and check only the admin flag
  - DELETE /api/users/{id} revokes the user's tokens like PATCH does, so
    re-enabling the account later does not revive them
  - wrong first-run setup codes are budgeted per IP and globally; the
    right code still works from another IP, localhost is never locked
  - /plex/poll needs the nonce /plex/pin handed its creator, is budgeted
    per client IP as well as per pin, and asks plex.tv under the client id
    the PIN was created with

    python tests/test_auth_hardening.py
"""
import asyncio
import pathlib
import sys
from datetime import datetime, timezone

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config import settings

# CI has no .env; PyJWT refuses an empty key (see test_auth_sessions.py).
if not settings.effective_jwt_secret:
    from pydantic import SecretStr
    settings.JWT_SECRET = SecretStr("test-only-secret-never-shipped-32-bytes-min-per-rfc7518")

import src.routers.auth as auth
import src.routers.users as users
from src.database.models import Base, User
from src.services import rate_limit

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def status_of(fn, *a, **kw):
    """HTTP status an endpoint/dependency call ends in (200 when it returns)."""
    try:
        out = fn(*a, **kw)
        if asyncio.iscoroutine(out):
            asyncio.run(out)
        return 200
    except HTTPException as e:
        return e.status_code


engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()


def _user(plex_id, is_admin):
    u = User(plex_user_id=plex_id, plex_username=f"u{plex_id}", is_admin=is_admin,
             is_active=True, created_at=datetime.now(timezone.utc))
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


class _Req:
    def __init__(self, host="192.168.1.50", headers=None):
        self.client = type("C", (), {"host": host})()
        self.headers = headers or {}


def _bearer(u):
    return _Req(headers={"Authorization": "Bearer " + auth._create_jwt(u.id, u.is_admin, u.token_version or 0)})


# ── 1. setup gate honours revocation ─────────────────────────────────────────

admin = _user("1", True)
member = _user("2", False)

req = _bearer(admin)
check("setup gate: a live admin token passes",
      status_of(auth.require_admin_or_first_run, req, db) == 200)
check("setup gate: a member token is refused",
      status_of(auth.require_admin_or_first_run, _bearer(member), db) == 403)

admin.token_version = (admin.token_version or 0) + 1       # POST /logout
db.commit()
check("setup gate: the admin's token from before a logout is dead",
      status_of(auth.require_admin_or_first_run, req, db) == 401)

req = _bearer(admin)
admin.is_active = False
db.commit()
check("setup gate: a deactivated admin's token is dead",
      status_of(auth.require_admin_or_first_run, req, db) == 401)
admin.is_active = True
db.commit()
check("setup gate: no token at all is a 401",
      status_of(auth.require_admin_or_first_run, _Req(), db) == 401)


# ── 2. DELETE /users/{id} revokes tokens ────────────────────────────────────

before = member.token_version or 0
member_req = _bearer(member)
asyncio.run(users.delete_user(member.id, _admin=admin, db=db))
db.refresh(member)
check("delete: the user is deactivated", member.is_active is False)
check("delete: the token version is bumped", (member.token_version or 0) == before + 1)
member.is_active = True                                   # admin re-enables later
db.commit()
check("delete: re-enabling does not revive the old token",
      status_of(auth.get_current_user, member_req, db) == 401)


# ── 3. setup-code guess budget ──────────────────────────────────────────────

rate_limit.reset("setup-code-fail")
attacker, owner_phone = "192.168.1.66", "192.168.1.20"
check("setup code: a missing code is a plain 401 and costs nothing",
      all(status_of(auth._require_setup_code, _Req(attacker)) == 401 for _ in range(30)))
codes = [status_of(auth._require_setup_code, _Req(attacker, {"X-Setup-Code": "0000-0000"}))
         for _ in range(auth.SETUP_CODE_FAILS_PER_IP)]
check("setup code: wrong guesses are 401 up to the budget", set(codes) == {401})
check("setup code: past the budget even the RIGHT code is refused from that IP",
      status_of(auth._require_setup_code,
                _Req(attacker, {"X-Setup-Code": auth.SETUP_CODE})) == 429)
check("setup code: the right code still works from another device",
      status_of(auth._require_setup_code,
                _Req(owner_phone, {"X-Setup-Code": auth.SETUP_CODE})) == 200)
check("setup code: localhost is never locked", status_of(auth._require_setup_code, _Req("127.0.0.1")) == 200)

rate_limit.reset("setup-code-fail")
n = 0
while n < auth.SETUP_CODE_FAILS_GLOBAL:                  # rotating addresses
    status_of(auth._require_setup_code, _Req(f"10.0.{n // 250}.{n % 250}", {"X-Setup-Code": "BAD"}))
    n += 1
check("setup code: rotating IPs runs into the global budget",
      status_of(auth._require_setup_code, _Req("10.9.9.9", {"X-Setup-Code": "BAD"})) == 429)
rate_limit.reset("setup-code-fail")


# ── 4/5. Plex PIN bound to its creator ──────────────────────────────────────

seen_client_ids = []


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


class _FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, **kw):
        seen_client_ids.append(headers["X-Plex-Client-Identifier"])
        return _Resp(201, {"id": 4242, "code": "ABCD1234"})

    async def get(self, url, headers=None, **kw):
        seen_client_ids.append(headers["X-Plex-Client-Identifier"])
        return _Resp(200, {"authToken": None})


real_client, real_cid = auth.httpx.AsyncClient, settings.PLEX_CLIENT_ID
auth.httpx.AsyncClient = _FakeClient
rate_limit.reset("plex-poll-ip")
auth.poll_rate_limit.clear()
auth._pin_create_rate_limit.clear()
try:
    settings.PLEX_CLIENT_ID = "curatarr-install-a"
    pin = asyncio.run(auth.request_plex_pin(_Req("192.168.1.20")))
    check("pin: the creator gets a nonce", len(pin.get("nonce") or "") >= 24)
    check("pin: the auth URL names the same client id the PIN was made with",
          "clientID=curatarr-install-a" in pin["auth_url"] and seen_client_ids[-1] == "curatarr-install-a")

    def poll(nonce, host="192.168.1.20"):
        return auth.poll_plex_pin(pin["pin_id"], _Req(host, {"X-Plex-Pin-Nonce": nonce}),
                                  BackgroundTasks(), db=db)

    check("poll: no nonce is refused", status_of(poll, "") == 403)
    check("poll: a wrong nonce is refused", status_of(poll, "x" * 32, host="192.168.1.66") == 403)
    check("poll: an unknown pin is refused",
          status_of(auth.poll_plex_pin, 999, _Req(headers={"X-Plex-Pin-Nonce": pin["nonce"]}),
                    BackgroundTasks(), db=db) == 403)
    settings.PLEX_CLIENT_ID = "changed-by-a-reload"
    out = asyncio.run(poll(pin["nonce"]))
    check("poll: the creator's nonce polls it", out == {"status": "pending"})
    check("poll: plex.tv is asked under the PIN's own client id",
          seen_client_ids[-1] == "curatarr-install-a")

    # Per-IP budget: across pin ids, not just per pin.
    rate_limit.reset("plex-poll-ip")
    codes = [status_of(auth.poll_plex_pin, 1000 + i, _Req("192.168.1.77", {"X-Plex-Pin-Nonce": "guess"}),
                       BackgroundTasks(), db=db)
             for i in range(auth.MAX_POLLS_PER_IP_PER_MINUTE + 1)]
    check("poll: one client walking pin ids hits the per-IP budget", codes[-1] == 429 and codes[0] == 403)
    check("poll: other clients are unaffected", status_of(poll, pin["nonce"]) == 200)

    auth._pin_bindings[4242] = (pin["nonce"], "curatarr-install-a", 0.0)   # created long ago
    check("poll: an expired binding is refused", status_of(poll, pin["nonce"]) == 403)
finally:
    auth.httpx.AsyncClient, settings.PLEX_CLIENT_ID = real_client, real_cid
    rate_limit.reset("plex-poll-ip")
    auth._pin_bindings.clear()

frontend = (_ROOT / "frontend/js/auth.js").read_text(encoding="utf-8")
check("frontend: the poll sends the nonce back as a header, not in the URL",
      "{'X-Plex-Pin-Nonce': data.nonce}" in frontend and "?nonce=" not in frontend)


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
