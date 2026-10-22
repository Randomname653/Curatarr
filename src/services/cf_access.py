"""Cloudflare Access at the origin: verify the Access token on public names (2026-10-22).

Curatarr can be reached two ways: directly on the LAN (http://<pc>:8000) and
through Cloudflare -> the reverse proxy under a public name. Cloudflare Access
gates the public name with a login, but the proxy answers anyone who connects
to the home IP directly and sends that name - Access never sees them.
Cloudflare's answer is to have the origin check the token Access stamps on
every request it let through (``Cf-Access-Jwt-Assertion``, RS256, signed by
the team's keys).

So for a Host on ``CF_ACCESS_HOSTS`` a request must carry a valid token for
``CF_ACCESS_AUD`` from ``CF_ACCESS_TEAM_DOMAIN``; any other Host (LAN IP,
PC name, localhost) is untouched, so the local route never depends on
Cloudflare. Public hosts with the team or audience unset fail closed.

The RS256 check is done here, without the ``cryptography`` package PyJWT
would need: RSA public-key verification is one modular exponentiation, and
the result is compared byte for byte against the one valid PKCS#1 v1.5
encoding (nothing is parsed out of it - the parsing is where signature
forgeries live).
"""
import base64
import hashlib
import hmac
import json
import logging
import time

logger = logging.getLogger(__name__)

JWT_HEADER = b"cf-access-jwt-assertion"
JWKS_TTL_S = 3600
# An unknown key id refetches the key set at most this often: a stream of
# forged kids must not turn into a stream of requests to Cloudflare.
JWKS_REFETCH_MIN_S = 60
CLOCK_SKEW_S = 60

# DER prefix of a SHA-256 DigestInfo (RFC 8017, 9.2 note 1).
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")

_jwks: dict = {"keys": {}, "at": 0.0, "team": ""}


def parse_hosts(raw: str | None) -> list[str]:
    return [h.strip().lower().rstrip(".") for h in (raw or "").split(",") if h.strip()]


def host_matches(host: str, patterns: list[str]) -> bool:
    """Exact names and "*.example.com" wildcards; loopback is NOT implied."""
    for pat in patterns:
        if pat == host:
            return True
        if pat.startswith("*.") and host.endswith(pat[1:]) and len(host) > len(pat) - 1:
            return True
    return False


def _b64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _int(s: str) -> int:
    return int.from_bytes(_b64url(s), "big")


def rs256_verify(n: int, e: int, signing_input: bytes, sig: bytes) -> bool:
    k = (n.bit_length() + 7) // 8
    if k < 256 or len(sig) != k:          # RSA-2048 at least
        return False
    s = int.from_bytes(sig, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    digest = _SHA256_PREFIX + hashlib.sha256(signing_input).digest()
    expected = b"\x00\x01" + b"\xff" * (k - len(digest) - 3) + b"\x00" + digest
    return hmac.compare_digest(em, expected)


def check_claims(claims: dict, team: str, aud: str, now: float) -> str | None:
    """None when the claims fit, else why not."""
    if claims.get("iss") != f"https://{team}":
        return "wrong issuer"
    auds = claims.get("aud")
    auds = [auds] if isinstance(auds, str) else (auds or [])
    if aud not in auds:
        return "wrong audience"
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or exp < now - CLOCK_SKEW_S:
        return "expired"
    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and nbf > now + CLOCK_SKEW_S:
        return "not yet valid"
    return None


def verify_token(token: str, keys: dict, team: str, aud: str, now: float | None = None) -> str | None:
    """None when ``token`` is a valid Access token for ``aud``, else why not.
    ``keys`` maps kid -> (n, e)."""
    try:
        h64, p64, s64 = token.split(".")
        header = json.loads(_b64url(h64))
        claims = json.loads(_b64url(p64))
        sig = _b64url(s64)
    except (ValueError, TypeError):
        return "malformed token"
    if not isinstance(header, dict) or not isinstance(claims, dict):
        return "malformed token"
    if header.get("alg") != "RS256":
        return "wrong algorithm"
    key = keys.get(header.get("kid"))
    if key is None:
        return "unknown key"
    if not rs256_verify(key[0], key[1], f"{h64}.{p64}".encode("ascii"), sig):
        return "bad signature"
    return check_claims(claims, team, aud, time.time() if now is None else now)


def _parse_jwks(data: dict) -> dict:
    keys = {}
    for k in (data or {}).get("keys") or []:
        if isinstance(k, dict) and k.get("kty") == "RSA" and k.get("kid") and k.get("n") and k.get("e"):
            keys[k["kid"]] = (_int(k["n"]), _int(k["e"]))
    return keys


async def _fetch_jwks(team: str) -> dict:
    import httpx
    from src.services.http_client import tls_context
    async with httpx.AsyncClient(verify=tls_context(), timeout=10) as client:
        resp = await client.get(f"https://{team}/cdn-cgi/access/certs")
        resp.raise_for_status()
        return _parse_jwks(resp.json())


async def team_keys(team: str, kid: str | None) -> dict:
    """The team's signing keys, cached; refetched when stale, or when a token
    names a key we do not have (Cloudflare rotates them)."""
    now = time.monotonic()
    fresh = _jwks["team"] == team and now - _jwks["at"] < JWKS_TTL_S
    if fresh and (kid in _jwks["keys"] or now - _jwks["at"] < JWKS_REFETCH_MIN_S):
        return _jwks["keys"]
    try:
        keys = await _fetch_jwks(team)
    except Exception as e:  # noqa: BLE001 - a failed fetch keeps the old keys; no keys = refused
        logger.warning("[cf-access] fetching the key set from %s failed: %s", team, e)
        _jwks["at"] = now - JWKS_TTL_S + JWKS_REFETCH_MIN_S   # retry in a minute, not per request
        return _jwks["keys"] if _jwks["team"] == team else {}
    _jwks.update(keys=keys, at=now, team=team)
    return keys


def token_kid(token: str) -> str | None:
    try:
        return json.loads(_b64url(token.split(".", 1)[0])).get("kid")
    except (ValueError, TypeError, AttributeError):
        return None
