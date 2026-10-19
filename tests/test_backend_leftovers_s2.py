"""Backend leftovers of the 2026-10 sweep.

  * ALLOWED_HOSTS: no Host-header check existed (appsec audit row 1). Now an
    opt-in allow-list, empty by default, loopback names always admitted,
    refused before auth with the security headers still on the 400
  * GET /setup/gpu returned nvidia-smi's own error text / exception string
  * scripts/mbid_speedrunner.py took music_pipeline_running with a bare
    get-then-set_state: a killed run blocked every runner for good, and two
    runners could both start. Now the state_lease, with a heartbeat
  * GET /library/items: a member's root_filter was a free substring on the
    arr's folder paths (folder names they are not shown, found by counts)
  * taste_engine: a comment still promised a PIN-keyed embedding store

Each check fails on the code before this change.

    python tests/test_backend_leftovers_s2.py
"""
import asyncio
import importlib.util
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"
os.environ["ALLOWED_HOSTS"] = ""          # a developer's .env must not decide the default

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import Base  # noqa: E402

Base.metadata.create_all(conn_mod.engine)

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.services import app_state, state_lease  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


# ── 1. ALLOWED_HOSTS ────────────────────────────────────────────────────────
print("ALLOWED_HOSTS")
import src.main as main  # noqa: E402

_entry = next((m for m in main.app.user_middleware
               if m.cls.__name__ == "HostAllowListMiddleware"), None)
_names = [m.cls.__name__ for m in main.app.user_middleware]   # outermost first


def _status(client, host, path="/"):
    r = client.get(path, headers={"host": host})
    return r.status_code, r


plain = TestClient(main.app)
check("default (unset): registered from settings, and any Host passes",
      _entry is not None and _entry.kwargs.get("allowed_hosts") == ""
      and _status(plain, "rebind.attacker.example")[0] == 200
      and _status(plain, "192.168.1.20:8000")[0] == 200)

if _entry is not None:
    _entry.kwargs["allowed_hosts"] = "Curatarr.lan, *.home.example ,192.168.1.20"
    main.app.middleware_stack = None        # rebuilt with the list on the next request
guarded = TestClient(main.app)
listed = [_status(guarded, h)[0] for h in
          ("curatarr.lan", "CURATARR.LAN:8000", "box.home.example", "192.168.1.20:8000")]
bad_code, bad = _status(guarded, "rebind.attacker.example")
api_code, _ = _status(guarded, "rebind.attacker.example:8000", "/api/users/")
check("set: listed names (any case, port, wildcard) pass; others get 400 before auth",
      _entry is not None and listed == [200] * 4
      and bad_code == 400 and _status(guarded, "home.example")[0] == 400
      and api_code == 400 and bad.headers.get("x-content-type-options") == "nosniff"
      and _names.index("SecurityHeadersMiddleware") < _names.index("HostAllowListMiddleware")
      < _names.index("TokenRefreshMiddleware"))
check("set: localhost, 127.0.0.1 and [::1] are always admitted",
      _entry is not None
      and [_status(guarded, h)[0] for h in ("localhost:8000", "127.0.0.1", "[::1]:8000")]
      == [200] * 3)
if _entry is not None:
    _entry.kwargs["allowed_hosts"] = ""
    main.app.middleware_stack = None


# ── 2. nvidia-smi error text ────────────────────────────────────────────────
print("nvidia-smi")
from src.services import setup_wizard  # noqa: E402

SECRET = r"C:\Users\owner\secret-tools\nvidia-smi.exe"
cap = _Capture()
logging.getLogger("src.services.setup_wizard").addHandler(cap)
_real_run = subprocess.run


def _denied(*a, **kw):
    raise PermissionError(f"[WinError 5] Access is denied: '{SECRET}'")


def _nvml(*a, **kw):
    return SimpleNamespace(returncode=9, stdout=b"",
                           stderr=f"Failed to initialize NVML at {SECRET}".encode())

try:
    subprocess.run = _denied
    r1 = asyncio.run(setup_wizard.detect_gpu())
    subprocess.run = _nvml
    r2 = asyncio.run(setup_wizard.detect_gpu())
finally:
    subprocess.run = _real_run
    logging.getLogger("src.services.setup_wizard").removeHandler(cap)
check("the client gets a fixed sentence; the detail goes to the log",
      not r1["ok"] and not r2["ok"]
      and SECRET not in r1["error"] and SECRET not in r2["error"]
      and "server log" in r1["error"] and "server log" in r2["error"]
      and sum(SECRET in line for line in cap.lines) == 2)


# ── 3. mbid_speedrunner holds the lease ─────────────────────────────────────
print("mbid_speedrunner")
_cwd = os.getcwd()
_spec = importlib.util.spec_from_file_location("mbid_speedrunner_t",
                                               ROOT / "scripts" / "mbid_speedrunner.py")
sr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sr)
os.chdir(_cwd)
KEY = "music_pipeline_running"
sr._resolve_admin_user_id = lambda: 1
seen = {}


async def _work(user_id, batch=0):
    seen["flag"] = app_state.get_state(KEY)
    seen["holder"] = app_state.get_state(KEY + "_holder") or ""
    return {"resolved": 0, "failed": 0, "queried": 0, "total_unique": 0, "remaining": 0}

sr.resolve_artist_mbids = _work
rc = asyncio.run(sr.amain([]))
check("a run holds the lease (this PID) and releases it",
      rc == 0 and seen.get("flag") == "1"
      and seen.get("holder", "").startswith(state_lease.my_token() + "|")
      and app_state.get_state(KEY) == "0" and not app_state.get_state(KEY + "_holder"))

# A killed run's legacy "1" (nobody recorded) is taken over; a live holder
# blocks and keeps its lease.
app_state.set_state(KEY, "1")
app_state.set_state(KEY + "_holder", "")
seen.clear()
rc_stale = asyncio.run(sr.amain([]))
state_lease.acquire(KEY)                     # this process = a live holder
live = app_state.get_state(KEY + "_holder")
seen.clear()
rc_live = asyncio.run(sr.amain([]))
blocked_kept = (app_state.get_state(KEY) == "1"
                and app_state.get_state(KEY + "_holder") == live)
state_lease.release(KEY)
check("a dead holder's flag is taken over; a live holder blocks and keeps it",
      rc_stale == 0 and rc_live == 2 and not seen and blocked_kept)

# Lease lost mid-run (taken over after a stall): the heartbeat stops the run
# and the release leaves the new holder alone.
OTHER = "otherhost|4242|1.0|9999999999"


async def _slow(user_id, batch=0):
    app_state.set_state(KEY + "_holder", OTHER)
    await asyncio.sleep(5)
    return {}

sr.resolve_artist_mbids = _slow
_hb = getattr(sr, "_HEARTBEAT_S", None)
sr._HEARTBEAT_S = 0.05
try:
    rc_lost = asyncio.run(asyncio.wait_for(sr.amain([]), 3))
except asyncio.TimeoutError:
    rc_lost = "ran on without the lease"
sr._HEARTBEAT_S = _hb
check("a lost lease stops the run, and the release leaves the new holder's lease",
      rc_lost == 3 and app_state.get_state(KEY) == "1"
      and app_state.get_state(KEY + "_holder") == OTHER)
app_state.set_state(KEY, "0")
app_state.set_state(KEY + "_holder", "")


# ── 4. root_filter for members ──────────────────────────────────────────────
print("/library/items root_filter")
import src.cache.metadata_cache as mc_mod  # noqa: E402
import src.routers.library as lib  # noqa: E402

ADMIN = SimpleNamespace(id=1, is_admin=True)
MEMBER = SimpleNamespace(id=2, is_admin=False)


class _FakeCache:
    def close(self):
        pass

_RAW = [{"id": 1, "title": "Bluey", "rootFolderPath": "/srv/private-share/anime", "tags": []},
        {"id": 2, "title": "Severance", "rootFolderPath": "/srv/private-share/tv", "tags": []}]


async def _fresh(service, *, force_refresh=False):
    return _RAW, [], {"source": "cache", "age_s": 1, "ttl_s": 900}

_real_cache, _real_lookup, _real_fetch = mc_mod.MetadataCache, lib._enrichment_lookup, lib._fetch_arr_library
mc_mod.MetadataCache = _FakeCache
lib._enrichment_lookup = lambda svc, raw, cache: None
lib._fetch_arr_library = _fresh


def total(user, rf):
    try:
        return asyncio.run(lib.library_items(
            "sonarr", sort="title_asc", needs_enrichment=False, root_filter=rf,
            curatarr_only=False, search=None, limit=200, refresh=False, _user=user))["total"]
    except HTTPException as e:
        return e.status_code

try:
    check("member: a probe substring is refused; the TV/Anime tab values work; "
          "admin unchanged",
          total(MEMBER, "private") == 400 and total(MEMBER, "/srv/p") == 400
          and total(MEMBER, "anime") == 1 and total(MEMBER, "TV") == 1
          and total(MEMBER, None) == 2 and total(ADMIN, "private-share") == 2)
finally:
    mc_mod.MetadataCache, lib._enrichment_lookup, lib._fetch_arr_library = \
        _real_cache, _real_lookup, _real_fetch


# ── 5. taste_engine comment ─────────────────────────────────────────────────
print("taste_engine")
_te = (ROOT / "src" / "services" / "taste_engine.py").read_text(encoding="utf-8")
check("no comment promises a PIN-keyed embedding store",
      "when PIN available" not in _te)


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
