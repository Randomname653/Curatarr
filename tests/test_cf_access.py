"""Cloudflare Access at the origin (2026-10-22).

Curatarr is reachable on the LAN and, through Cloudflare and the reverse
proxy, under a public name. Cloudflare Access gates the public name, but the
proxy answers anyone who connects to the home IP directly and sends that
name. So a request under a CF_ACCESS_HOSTS name must carry a valid Access
token; the LAN names are never checked.

A throwaway RSA-2048 key is generated per run (pure Python, nothing stored).

    python tests/test_cf_access.py
"""
import asyncio
import base64
import hashlib
import json
import secrets
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.services.cf_access as cfa  # noqa: E402
from src.middleware import CloudflareAccessMiddleware  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── a throwaway key ─────────────────────────────────────────────────────────

def _probable_prime(n, rounds=32):
    for p in (3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return False
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(rounds):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits):
    while True:
        c = secrets.randbits(bits) | (3 << (bits - 2)) | 1
        if _probable_prime(c):
            return c


def keypair(bits=2048):
    e = 65537
    while True:
        p, q = _prime(bits // 2), _prime(bits // 2)
        phi = (p - 1) * (q - 1)
        if (p * q).bit_length() == bits and phi % e:
            return p * q, e, pow(e, -1, phi)


N, E, D = keypair()
TEAM, AUD, KID = "home.cloudflareaccess.com", "aud-tag-123", "k1"
KEYS = {KID: (N, E)}


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def sign(signing_input: bytes, n=N, d=D) -> bytes:
    k = (n.bit_length() + 7) // 8
    digest = cfa._SHA256_PREFIX + hashlib.sha256(signing_input).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(digest) - 3) + b"\x00" + digest
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


def token(claims=None, header=None, n=N, d=D):
    now = int(time.time())
    c = {"iss": f"https://{TEAM}", "aud": [AUD], "exp": now + 600, "iat": now, "nbf": now,
         "email": "member@example.com"}
    c.update(claims or {})
    h = {"alg": "RS256", "kid": KID, "typ": "JWT"}
    h.update(header or {})
    si = f"{b64(json.dumps(h).encode())}.{b64(json.dumps(c).encode())}"
    return f"{si}.{b64(sign(si.encode(), n, d))}"


def verify(t):
    return cfa.verify_token(t, KEYS, TEAM, AUD)


# ── the token check ─────────────────────────────────────────────────────────
good = token()
check("a valid Access token passes", verify(good) is None)
h, p, s = good.split(".")
forged_claims = b64(json.dumps({"iss": f"https://{TEAM}", "aud": [AUD],
                                "exp": int(time.time()) + 600, "email": "attacker@example.com"}).encode())
check("changed claims break the signature", verify(f"{h}.{forged_claims}.{s}") == "bad signature")
check("another audience is refused", verify(token({"aud": ["someone-else"]})) == "wrong audience")
check("a single-string audience still counts", verify(token({"aud": AUD})) is None)
check("another team's issuer is refused", verify(token({"iss": "https://evil.cloudflareaccess.com"})) == "wrong issuer")
check("an expired token is refused", verify(token({"exp": int(time.time()) - 3600})) == "expired")
check("a token without exp is refused", verify(token({"exp": None})) == "expired")
check("a not-yet-valid token is refused", verify(token({"nbf": int(time.time()) + 3600})) == "not yet valid")
check("alg none / HS256 are refused",
      verify(token(header={"alg": "none"})) == "wrong algorithm"
      and verify(token(header={"alg": "HS256"})) == "wrong algorithm")
check("an unknown key id is refused", verify(token(header={"kid": "other"})) == "unknown key")
check("garbage is refused", verify("not.a.jwt.at.all") == "malformed token" and verify("") == "malformed token")
n2, e2, d2 = keypair()
check("a token signed by another key is refused", verify(token(n=n2, d=d2)) == "bad signature")
k = (N.bit_length() + 7) // 8
si = good.rsplit(".", 1)[0].encode()
check("a zero / out-of-range / short signature is refused",
      not cfa.rs256_verify(N, E, si, b"\x00" * k)
      and not cfa.rs256_verify(N, E, si, N.to_bytes(k, "big"))
      and not cfa.rs256_verify(N, E, si, sign(si)[1:]))
ns, es, ds = keypair(1024)
check("keys under 2048 bits are not trusted", not cfa.rs256_verify(ns, es, si, sign(si, ns, ds)))

# ── the key set: cached, refetched for a new kid, but not per request ───────
fetches = []


async def _fetch(team):
    fetches.append(team)
    return {KID: (N, E)}

cfa._fetch_jwks = _fetch
cfa._jwks.update(keys={}, at=0.0, team="")
asyncio.run(cfa.team_keys(TEAM, KID))
asyncio.run(cfa.team_keys(TEAM, KID))
check("the key set is fetched once and cached", len(fetches) == 1)
asyncio.run(cfa.team_keys(TEAM, "forged-1"))
asyncio.run(cfa.team_keys(TEAM, "forged-2"))
check("unknown key ids do not refetch on every request", len(fetches) == 1)
cfa._jwks["at"] -= cfa.JWKS_REFETCH_MIN_S + 1
asyncio.run(cfa.team_keys(TEAM, "rotated"))
check("...but a new key id refetches after a minute (Cloudflare rotates keys)", len(fetches) == 2)

# ── the middleware ──────────────────────────────────────────────────────────


def run_mw(host, tok=None, hosts="curatarr.example.de", team=TEAM, aud=AUD, kind="http"):
    reached, sent = [], []

    async def app(scope, receive, send):
        reached.append(scope["path"])

    async def send(msg):
        sent.append(msg)

    headers = [(b"host", host.encode())]
    if tok is not None:
        headers.append((b"cf-access-jwt-assertion", tok.encode()))
    mw = CloudflareAccessMiddleware(app, hosts=hosts, team=team, aud=aud)
    asyncio.run(mw({"type": kind, "path": "/api/x", "method": "GET", "headers": headers}, None, send))
    status = next((m.get("status") for m in sent if m["type"] == "http.response.start"), None)
    closed = any(m["type"] == "websocket.close" for m in sent)
    return bool(reached), status, closed


check("the LAN address passes without any token", run_mw("10.0.0.5:8000") == (True, None, False))
check("the PC's name and localhost pass too",
      run_mw("gaming-pc:8000")[0] and run_mw("localhost:8000")[0])
check("the public name without a token (around Cloudflare) is refused 403",
      run_mw("curatarr.example.de") == (False, 403, False))
check("...with a valid Access token it passes", run_mw("curatarr.example.de", good)[0])
check("...with a forged one it is refused", run_mw("curatarr.example.de", f"{h}.{forged_claims}.{s}")[1] == 403)
check("Host case, port and trailing dot do not dodge the check",
      run_mw("Curatarr.Example.DE.:443")[1] == 403)
check("a public name with team/audience unset fails closed",
      run_mw("curatarr.example.de", good, team="")[1] == 403
      and run_mw("curatarr.example.de", good, aud="")[1] == 403)
check("wildcards cover subdomains", run_mw("x.example.de", hosts="*.example.de")[1] == 403
      and run_mw("example.de", hosts="*.example.de")[0])
check("no CF_ACCESS_HOSTS = no check at all", run_mw("curatarr.example.de", hosts="")[0])
check("a websocket around Cloudflare is closed", run_mw("curatarr.example.de", kind="websocket")[2])

# ── the Plex login comes back to the name it started from ───────────────────
import src.routers.auth as auth  # noqa: E402
auth.settings.CF_ACCESS_HOSTS = "curatarr.example.de"
lan = str(auth.settings.PLEX_REDIRECT_URI)


def fwd(host):
    return auth._plex_forward_url(SimpleNamespace(headers={"host": host}))

check("a login on the public name returns to https://<public name>",
      fwd("curatarr.example.de") == "https://curatarr.example.de")
check("a login on the LAN returns to PLEX_REDIRECT_URI", fwd("10.0.0.5:8000") == lan)
check("a made-up Host is never echoed", fwd("evil.example.com") == lan)

# ── wired ───────────────────────────────────────────────────────────────────
src_main = (ROOT / "src/main.py").read_text(encoding="utf-8")
check("main.py installs the check inside the Host allow-list",
      src_main.index("CloudflareAccessMiddleware, hosts=") < src_main.index("HostAllowListMiddleware, allowed_hosts="))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
