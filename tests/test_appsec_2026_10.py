"""AppSec audit 2026-10-10 (docs/security/appsec-audit-2026-10-10.md).

Pins:
  A  the first-run loopback exemption wants a browser that addresses us as
     localhost: a DNS-rebound page (foreign Host), a same-host reverse proxy
     (forwarding headers) and a foreign Origin all need the setup code
  B  "Test connection" sends a SAVED secret only to the saved address
  C  service URLs: http(s), no query/fragment/credentials, no link-local or
     metadata address; failed tests do not reflect upstream bodies or tell
     refused from timed out
  D  a delete with a malformed media id sends nothing
  -  the retired PIN endpoints, model and table are gone
  -  /library/profiles is admin-only; the image proxy serves raster only

    python tests/test_appsec_2026_10.py
"""
import asyncio
import inspect
import pathlib
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import httpx  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from pydantic import SecretStr, ValidationError  # noqa: E402

from src.config import settings  # noqa: E402
import src.routers.auth as auth  # noqa: E402
import src.routers.setup as st  # noqa: E402
import src.routers.library as lib  # noqa: E402
import src.routers.recommendations as recs  # noqa: E402
import src.routers.image_proxy as ip  # noqa: E402
import src.services.setup_wizard as sw  # noqa: E402
from src.services.endpoint_policy import (  # noqa: E402
    same_origin, secret_for_target, validate_service_url)

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


_RealClient = httpx.AsyncClient


class mock_http:
    """Every httpx.AsyncClient built inside the block talks to ``handler``."""
    def __init__(self, handler):
        self.handler = handler

    def __enter__(self):
        def factory(*a, **kw):
            kw.pop("transport", None)
            return _RealClient(*a, transport=httpx.MockTransport(self.handler), **kw)
        httpx.AsyncClient = factory

    def __exit__(self, *exc):
        httpx.AsyncClient = _RealClient


# ── A: the loopback exemption ────────────────────────────────────────────────

class _Req:
    def __init__(self, peer, headers):
        self.client = type("C", (), {"host": peer})()
        self.headers = headers


def gate(peer, headers):
    """None when the setup gate lets the request through, else the status."""
    try:
        auth._require_setup_code(_Req(peer, headers))
        return None
    except HTTPException as e:
        return e.status_code


check("A: browser on this machine at localhost passes",
      gate("127.0.0.1", {"host": "localhost:8000", "origin": "http://localhost:8000"}) is None)
check("A: 127.0.0.1 and [::1] spellings pass",
      gate("127.0.0.1", {"host": "127.0.0.1:8000"}) is None
      and gate("::1", {"host": "[::1]:8000"}) is None)
check("A: DNS rebinding (loopback peer, foreign Host) needs the code",
      gate("127.0.0.1", {"host": "rebind.evil.example:8000"}) == 401)
check("A: no Host header at all needs the code", gate("127.0.0.1", {}) == 401)
for h in ("x-forwarded-for", "x-real-ip", "forwarded", "x-forwarded-host"):
    check(f"A: same-host reverse proxy ({h}) needs the code",
          gate("127.0.0.1", {"host": "localhost:8000", h: "203.0.113.9"}) == 401)
check("A: a foreign Origin needs the code",
      gate("127.0.0.1", {"host": "localhost:8000", "origin": "http://evil.example"}) == 401)
check("A: a LAN peer claiming Host: localhost still needs the code",
      gate("192.168.1.50", {"host": "localhost:8000"}) == 401)
check("A: the code still opens the gate for a proxied caller",
      gate("127.0.0.1", {"host": "curatarr.example", "x-forwarded-for": "203.0.113.9",
                         "X-Setup-Code": auth.SETUP_CODE}) is None)


# ── C: the URL policy ────────────────────────────────────────────────────────

for good, want in (("http://192.168.1.10:32400/", "http://192.168.1.10:32400"),
                   ("http://plex.local:32400", "http://plex.local:32400"),
                   ("http://localhost:11434", "http://localhost:11434"),
                   ("http://[::1]:11434", "http://[::1]:11434"),
                   ("https://media.example.com/sonarr", "https://media.example.com/sonarr"),
                   ("http://100.101.5.7:7878", "http://100.101.5.7:7878")):   # Tailscale/CGNAT
    try:
        got = validate_service_url(good)
    except ValueError as e:
        got = f"refused: {e}"
    check(f"C: {good} accepted", got == want)
for bad in ("http://169.254.169.254/latest/meta-data", "http://[fe80::1]/",
            "http://[fd00:ec2::254]/", "http://100.100.100.200/",
            "http://10.0.0.1/secret#", "http://10.0.0.1/x?a=", "file:///etc/passwd",
            "gopher://h/", "http://user:pw@host/", "http://0.0.0.0:80", "http://224.0.0.1/",
            "not a url"):
    try:
        validate_service_url(bad)
        check(f"C: {bad} refused", False)
    except ValueError:
        check(f"C: {bad} refused", True)
check("C: blank and None pass through (unset / keep saved)",
      validate_service_url("") == "" and validate_service_url(None) is None)

for model, field in ((st.TestRequest, "url"), (st.RecommendRequest, "ollama_endpoint"),
                     (lib.ArrTestRequest, "url")):
    extra = {"service": "radarr"} if model in (st.TestRequest, lib.ArrTestRequest) else {}
    try:
        model(**extra, **{field: "http://169.254.169.254/#"})
        check(f"C: {model.__name__}.{field} enforces the policy", False)
    except ValidationError:
        check(f"C: {model.__name__}.{field} enforces the policy", True)
for field in ("plex_url", "ollama_endpoint", "radarr_url", "sonarr_url", "lidarr_url", "soulsync_url"):
    kw = {"plex_url": "http://p", "plex_token": "t", field: "http://10.0.0.1/x#"}
    try:
        st.SetupCompleteRequest(**kw)
        check(f"C: SetupCompleteRequest.{field} enforces the policy", False)
    except ValidationError:
        check(f"C: SetupCompleteRequest.{field} enforces the policy", True)
    try:
        st.ReconfigureRequest(**{field: "http://10.0.0.1/x#"})
        check(f"C: ReconfigureRequest.{field} enforces the policy", False)
    except ValidationError:
        check(f"C: ReconfigureRequest.{field} enforces the policy", True)
check("C: ReconfigureRequest still means 'unchanged' when empty",
      st.ReconfigureRequest().model_dump(exclude_none=True) == {})


def _refused(req):
    raise httpx.ConnectError("[Errno 111] Connection refused", request=req)


def _timeout(req):
    raise httpx.ConnectTimeout("timed out", request=req)


with mock_http(_refused):
    r1 = asyncio.run(sw.test_plex("http://10.0.0.1:22", "t"))
with mock_http(_timeout):
    r2 = asyncio.run(sw.test_plex("http://10.0.0.2:22", "t"))
check("C: refused and timed out read the same (no port scanner)",
      r1 == r2 and r1["ok"] is False and "refused" not in r1["error"].lower())
with mock_http(lambda req: httpx.Response(200, text="<html>router admin</html>")):
    r3 = asyncio.run(sw.test_arr("http://10.0.0.1", "k", "radarr"))
check("C: a non-API 200 is named, its body is not echoed",
      r3["ok"] is False and "router admin" not in r3["error"])
err = lib._public_test_error(Exception('API error 500: {"secret":"internal-page-body"}'))
check("C: an arr error body is never reflected",
      "internal-page-body" not in err and "500" in err)
check("C: 401 reads as a rejected key",
      "rejected" in lib._public_test_error(Exception("API error 401: nope")))


# ── B: saved secrets only go to the saved address ────────────────────────────

check("B: same origin ignores case and an explicit default port",
      same_origin("http://PLEX:80/", "http://plex") and not same_origin("http://plex:32400", "https://plex:32400"))
check("B: blank URL uses the saved secret", secret_for_target(None, None, "http://p:1", "S") == "S")
check("B: the saved address uses the saved secret",
      secret_for_target("http://p:1/", "", "http://p:1", "S") == "S")
check("B: a typed secret goes wherever it is pointed",
      secret_for_target("http://other:1", "typed", "http://p:1", "S") == "typed")
try:
    secret_for_target("http://attacker:1", None, "http://p:1", "S")
    check("B: a new URL without its own secret is refused", False)
except ValueError:
    check("B: a new URL without its own secret is refused", True)

_saved = (settings.RADARR_URL, settings.RADARR_API_KEY, settings.PLEX_URL, settings.PLEX_TOKEN)
settings.RADARR_URL, settings.RADARR_API_KEY = "http://radarr.lan:7878", "SAVED-RADARR-KEY"
settings.PLEX_URL, settings.PLEX_TOKEN = "http://plex.lan:32400", SecretStr("SAVED-PLEX-TOKEN")
sent = []
_real = (st.test_arr, st.test_plex)


async def _rec_arr(url, key, service):
    sent.append((url, key))
    return {"ok": True}


async def _rec_plex(url, token):
    sent.append((url, token))
    return {"ok": True}


st.test_arr, st.test_plex = _rec_arr, _rec_plex
try:
    for service, field in (("radarr", "api_key"), ("plex", "token")):
        sent.clear()
        try:
            asyncio.run(st.test_connection(st.TestRequest(service=service, url="http://attacker.example:1"),
                                           _gate=None))
            status = None
        except HTTPException as e:
            status = e.status_code
        check(f"B: setup/test {service}: saved secret never sent to a new URL",
              status == 400 and sent == [])
    sent.clear()
    asyncio.run(st.test_connection(st.TestRequest(service="radarr"), _gate=None))
    asyncio.run(st.test_connection(st.TestRequest(service="plex", url="http://plex.lan:32400/"), _gate=None))
    check("B: setup/test re-tests the saved config without retyping",
          sent == [("http://radarr.lan:7878", "SAVED-RADARR-KEY"),
                   ("http://plex.lan:32400", "SAVED-PLEX-TOKEN")])
finally:
    st.test_arr, st.test_plex = _real

made = []


class _FakeClient:
    def __init__(self, key):
        self.key = key

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def test_connection(self):
        return {"version": "5"}


_real_lib = (lib._make_client, lib.set_state)
lib._make_client = lambda svc, url, key: made.append((url, key)) or _FakeClient(key)
lib.set_state = lambda *a, **k: None
try:
    out = asyncio.run(lib.library_test(lib.ArrTestRequest(service="radarr", url="http://attacker.example:1"),
                                       _user=None))
    check("B: library/test: saved key never sent to a new URL",
          out["ok"] is False and made == [] and "key" in out["error"])
    out = asyncio.run(lib.library_test(lib.ArrTestRequest(service="radarr"), _user=None))
    check("B: library/test: blank fields test the saved config",
          out["ok"] is True and made == [("http://radarr.lan:7878", "SAVED-RADARR-KEY")])
finally:
    lib._make_client, lib.set_state = _real_lib
    settings.RADARR_URL, settings.RADARR_API_KEY, settings.PLEX_URL, settings.PLEX_TOKEN = _saved


# ── D: malformed ids never reach a DELETE ────────────────────────────────────

check("D: positive integers pass", recs._require_media_id(42) == "42" and recs._require_media_id(" 7 ") == "7")
for bad in ("", None, "0", "None", "-1", "../config/host", "1/../2", "1?x=1", "a1", "12345678901"):
    check(f"D: {bad!r} is not a media id", recs._require_media_id(bad) is None)

settings.RADARR_URL, settings.RADARR_API_KEY = "http://radarr.test", "k"
calls = []


def _record(req):
    calls.append((req.method, req.url.path))
    return httpx.Response(200)


class _P:
    service = "radarr"
    title = "Big Movie"
    media_id = ""


try:
    with mock_http(_record):
        for mid in ("", "None", "1/../../v3/config/host"):
            _P.media_id = mid
            res = asyncio.run(recs._execute_arr_delete(_P()))
            check(f"D: arr delete with media id {mid!r} sends nothing and fails",
                  res is False and calls == [])
        _P.media_id = "7"
        res = asyncio.run(recs._execute_arr_delete(_P()))
        check("D: a real id still deletes", res is True and calls == [("DELETE", "/api/v3/movie/7")])
finally:
    settings.RADARR_URL, settings.RADARR_API_KEY = _saved[0], _saved[1]


class _NoCalls:
    calls = []

    async def delete(self, *a, **k):
        self.calls.append("DELETE")

    async def get(self, *a, **k):
        self.calls.append("GET")

    async def aclose(self):
        pass


settings.PLEX_URL, settings.PLEX_TOKEN = "http://plex.test:32400", SecretStr("t")
try:
    c = _NoCalls()
    check("D: Plex delete with a malformed key sends nothing",
          asyncio.run(recs._plex_delete_artist("../../library/sections/1", client=c)) is False
          and c.calls == [])
finally:
    settings.PLEX_URL, settings.PLEX_TOKEN = _saved[2], _saved[3]


# ── PIN retired ──────────────────────────────────────────────────────────────

import src.routers.users as users  # noqa: E402
import src.database.models as models  # noqa: E402

paths = {getattr(r, "path", "") for r in users.router.routes}
check("PIN: no /me/pin or /me/pin-status route", not any("pin" in p for p in paths))
check("PIN: the UserPinHash model is gone", not hasattr(models, "UserPinHash"))
html = (_ROOT / "frontend/index.html").read_text(encoding="utf-8")
js = (_ROOT / "frontend/js/settings.js").read_text(encoding="utf-8")
check("PIN: the Settings passphrase UI is gone",
      "Encryption passphrase" not in html and "/api/users/me/pin" not in js)

import src.database.connection as conn  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

tmp = pathlib.Path(tempfile.mkdtemp()) / "pin.db"
eng = create_engine(f"sqlite:///{tmp.as_posix()}")
with eng.begin() as c:
    c.execute(text("CREATE TABLE user_pin_hashes (id INTEGER PRIMARY KEY, pin_hash TEXT, salt TEXT)"))
    c.execute(text("INSERT INTO user_pin_hashes (pin_hash, salt) VALUES ('deadbeefcafe', 'aa')"))
_real_engine = conn.engine
conn.engine = eng
try:
    conn._drop_retired_tables()
    conn._drop_retired_tables()     # idempotent: second boot finds nothing
    with eng.connect() as c:
        gone = c.execute(text("SELECT 1 FROM sqlite_master WHERE name='user_pin_hashes'")).first() is None
    eng.dispose()
    check("PIN: boot drops the stored hashes", gone)
    check("PIN: the freed pages do not keep the hash (secure_delete)",
          b"deadbeefcafe" not in tmp.read_bytes())
finally:
    conn.engine = _real_engine


# ── Low items ────────────────────────────────────────────────────────────────

dep = inspect.signature(lib.library_profiles).parameters["_user"].default.dependency
check("profiles: root folders and arr errors are admin-only", dep is auth.require_admin)

_real_ip = (ip._CACHE_DIR, ip._host_resolves_public)
ip._CACHE_DIR = pathlib.Path(tempfile.mkdtemp())


async def _public(host, resolver=None):
    return True


ip._host_resolves_public = _public
try:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    with mock_http(lambda req: httpx.Response(200, content=svg,
                                              headers={"content-type": "image/svg+xml"})):
        try:
            asyncio.run(ip.proxy_image(src="https://coverartarchive.org/release/x/front.svg"))
            status = 200
        except HTTPException as e:
            status = e.status_code
    check("proxy: SVG is refused (415), never cached",
          status == 415 and not any(ip._CACHE_DIR.iterdir()))
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    with mock_http(lambda req: httpx.Response(200, content=png, headers={"content-type": "image/png"})):
        resp = asyncio.run(ip.proxy_image(src="https://image.tmdb.org/t/p/w342/a.png"))
    check("proxy: a PNG is served with a sandbox CSP",
          resp.media_type == "image/png" and resp.headers.get("content-security-policy") == "sandbox")
finally:
    ip._CACHE_DIR, ip._host_resolves_public = _real_ip


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
