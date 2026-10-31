"""Startup checks, setup writes, the token refresh and two system endpoints
keep their blocking work off the event loop.

  * main /api/system/dependencies   a plain def (threadpool): the two
    requirement checks read files and walk importlib.metadata (~1 s cold)
  * main /api/system/shutdown       the delayed stop is a tracked task (a
    bare create_task could be collected, and "Shut down" did nothing)
  * setup complete / reconfigure    write_env (file I/O, two icacls runs on
    Windows) on a worker, a refused value still a 400
  * TokenRefreshMiddleware          the User lookup for a day-old token on
    the read pool
  * scheduler._startup_check, main._startup_sync_if_needed   a real game
    probe on a worker (on the loop it reads a cache not yet filled at boot)
  * scheduler.job_arr_sync          music_service (opens a SQLite file) on
    a worker

Each "off the loop" check fails on the code before this change.

    python tests/test_loop_free_s2d.py
"""
import asyncio
import datetime as dt
import os
import sys
import tempfile
import threading
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

from src.config import settings  # noqa: E402
settings.ENRICHMENT_CACHE = _TMP / "enrichment.db"
if not settings.effective_jwt_secret:
    from pydantic import SecretStr
    settings.JWT_SECRET = SecretStr("test-only-secret-never-shipped-32-bytes-min-per-rfc7518")

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import Base, DeletionProposal, User  # noqa: E402
from src.services import app_state  # noqa: E402

Base.metadata.create_all(conn_mod.engine)
with conn_mod.get_db_session() as _db:
    _db.add(User(id=1, plex_user_id="p1", plex_username="admin", is_admin=True, is_active=True))
    _db.commit()

from _checks import check  # noqa: E402


def _on_main() -> bool:
    # asyncio.run drives the loop on the main thread.
    return threading.current_thread() is threading.main_thread()


async def _no_sleep(*_a, **_k):
    return None


# ── 1. /api/system/dependencies ─────────────────────────────────────────────
print("main /api/system/dependencies")
import httpx  # noqa: E402
import src.main as main  # noqa: E402
import src.deps_check as deps_check  # noqa: E402
import src.deps_lock as deps_lock  # noqa: E402
from src.routers.auth import require_admin  # noqa: E402

_seen = {}


class _Report:
    def __init__(self, d):
        self.d = d

    def as_dict(self):
        return dict(self.d)


def _spy_check():
    _seen["check"] = _on_main()
    return _Report({"ok": True, "missing": []})


def _spy_lock():
    _seen["lock"] = _on_main()
    return _Report({"ok": True, "drift": []})


async def _get(path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                 base_url="http://t") as c:
        return await c.get(path)

_real = deps_check.check, deps_lock.check
deps_check.check, deps_lock.check = _spy_check, _spy_lock
main.app.dependency_overrides[require_admin] = lambda: None
try:
    r = asyncio.run(_get("/api/system/dependencies"))
finally:
    deps_check.check, deps_lock.check = _real
    main.app.dependency_overrides.pop(require_admin, None)
check("both checks ran off the loop",
      _seen.get("check") is False and _seen.get("lock") is False)
check("same response shape",
      r.status_code == 200
      and r.json() == {"ok": True, "missing": [], "lock": {"ok": True, "drift": []}})
r = asyncio.run(_get("/api/system/dependencies"))
check("still admin-only", r.status_code in (401, 403))

# ── 2. shutdown: a tracked task ─────────────────────────────────────────────
print("main /api/system/shutdown")
import src.services.bg_tasks as bg  # noqa: E402

_tracked = []


def _spy_track(coro, name=None):
    _tracked.append(name)
    coro.close()           # never actually stop the test process


_real_track = bg.track_task
bg.track_task = _spy_track
try:
    out = asyncio.run(main.shutdown_server())
finally:
    bg.track_task = _real_track
check("the stop goes through track_task as ui_shutdown", _tracked == ["ui_shutdown"])
check("same reply", out == {"status": "shutting_down"})

# ── 3. setup complete / reconfigure: write_env on a worker ──────────────────
print("setup complete / reconfigure")
import src.routers.setup as st  # noqa: E402
import src.services.setup_wizard as sw  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _BG:
    def add_task(self, *a, **k):
        pass


async def _no_refusal(url):
    return None

_writes = []


def _spy_write(cfg):
    _writes.append((_on_main(), dict(cfg)))


def _refuse(cfg):
    _writes.append((_on_main(), None))
    raise ValueError("PLEX_TOKEN: line breaks and NUL are not allowed in settings values")


_real_setup = (st.write_env, st.resolved_refusal, sw.current_env_config)
st.resolved_refusal = _no_refusal
st.write_env = _spy_write
_saved_settings = dict(settings.__dict__)
try:
    req = st.SetupCompleteRequest(plex_url="http://p", plex_token="t")
    out = asyncio.run(st.complete_setup(req, _BG(), _gate=None))
    check("complete_setup: write_env ran off the loop",
          len(_writes) == 1 and _writes[0][0] is False)
    check("... with the same config and reply",
          _writes[0][1]["plex_url"] == "http://p" and _writes[0][1]["pitcher_model"] == ""
          and out["status"] == "ok")

    st.write_env = _refuse
    try:
        asyncio.run(st.complete_setup(req, _BG(), _gate=None))
        code = None
    except HTTPException as e:
        code = (e.status_code, "line breaks" in e.detail)
    check("complete_setup: a refused value is still a 400", code == (400, True))

    _writes.clear()
    st.write_env = _spy_write
    sw.current_env_config = lambda: {"plex_url": "http://p", "plex_token": "t"}
    out = asyncio.run(st.reconfigure(st.ReconfigureRequest(plex_url="http://q"), _admin=None))
    check("reconfigure: write_env ran off the loop",
          len(_writes) == 1 and _writes[0][0] is False)
    check("... with the merged config and same reply",
          _writes[0][1]["plex_url"] == "http://q" and out["ok"] is True
          and out["changed"] == ["plex_url"])

    st.write_env = _refuse
    try:
        asyncio.run(st.reconfigure(st.ReconfigureRequest(plex_url="http://q"), _admin=None))
        code = None
    except HTTPException as e:
        code = e.status_code
    check("reconfigure: a refused value is still a 400", code == 400)
finally:
    st.write_env, st.resolved_refusal, sw.current_env_config = _real_setup
    settings.__dict__.update(_saved_settings)   # reconfigure reloads settings

# ── 4. TokenRefreshMiddleware: the User lookup on the read pool ─────────────
print("middleware.TokenRefreshMiddleware")
import jwt as pyjwt  # noqa: E402
import src.middleware as mw  # noqa: E402

_lookups = []


def _spy_subject(uid):
    _lookups.append(_on_main())
    return {"id": uid, "is_admin": True, "is_active": True, "token_version": 0}


def _mint(age_s):
    now = dt.datetime.now(dt.timezone.utc)
    return pyjwt.encode({"sub": "1", "admin": True, "iat": now - dt.timedelta(seconds=age_s),
                         "exp": now + dt.timedelta(days=5)},
                        settings.effective_jwt_secret, algorithm="HS256")


def _drive(token):
    headers = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message):
        if message["type"] == "http.response.start":
            headers.extend(message.get("headers", []))

    scope = {"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]}
    asyncio.run(mw.TokenRefreshMiddleware(app)(scope, None, send))
    return dict(headers)


_real_subject = mw._load_refresh_subject
mw._load_refresh_subject = _spy_subject
try:
    h = _drive(_mint(2 * 86400))
    check("the subject lookup ran off the loop", _lookups == [False])
    check("... and a fresh token is still issued", b"x-curatarr-refreshed-token" in h)
    _lookups.clear()
    h = _drive(_mint(60))
    check("a young token: no lookup, no refresh",
          _lookups == [] and b"x-curatarr-refreshed-token" not in h)
finally:
    mw._load_refresh_subject = _real_subject

# ── 5. startup checks probe for a game on a worker ──────────────────────────
print("scheduler._startup_check / main._startup_sync_if_needed")
import src.services.process_monitor as pm  # noqa: E402
from src.services import scheduler as sched  # noqa: E402

_probes = []


def _spy_game():
    _probes.append(_on_main())
    return True             # game running -> both return before any work


_real_igr, _real_sleep = pm.is_game_running, asyncio.sleep
pm.is_game_running = _spy_game
asyncio.sleep = _no_sleep
try:
    asyncio.run(sched._startup_check())
    check("_startup_check: the game probe ran off the loop", _probes == [False])
    _probes.clear()
    asyncio.run(main._startup_sync_if_needed())
    check("_startup_sync_if_needed: the game probe ran off the loop", _probes == [False])
finally:
    pm.is_game_running, asyncio.sleep = _real_igr, _real_sleep

# ── 6. job_arr_sync: music_service on a worker ──────────────────────────────
print("scheduler.job_arr_sync")


async def _candidates(category=None):
    return [{"title": "Old Song", "service": "lidarr", "arr_id": 9, "category": "music"}]


async def _gen(user_id, items, category, monitor_task=None):
    return [{"title": "Old Song", "pitch": "never played", "confidence": 0.8,
             "arr_id": 9, "service": "lidarr", "category": "music"}]


async def _enrich(p, item_map, category):
    return p


# The job imports these inside; the real ones open ChromaDB at import.
_saved_mods = {k: sys.modules.get(k) for k in
               ("src.routers.recommendations", "src.services.recommendations_engine")}
sys.modules["src.routers.recommendations"] = types.SimpleNamespace(
    _fetch_arr_candidates=_candidates, _enrich_proposal=_enrich,
    build_proposal_item_map=lambda items: {}, _retire_switched_off=lambda db, uid: 0)
sys.modules["src.services.recommendations_engine"] = types.SimpleNamespace(
    generate_deletion_proposals=_gen, run_judged_everything=lambda cat: True)
import src.services.music_source as ms  # noqa: E402

_music = []


def _spy_music():
    _music.append(_on_main())
    return "plex"


_real_music = ms.music_service
ms.music_service = _spy_music
app_state.set_state("deletion_run", "0")
app_state.set_state("game_active", "0")
try:
    out = asyncio.run(sched.job_arr_sync())
finally:
    ms.music_service = _real_music
    for k, v in _saved_mods.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v
with conn_mod.get_db_session() as _db:
    rows = [(p.title, p.status, p.category) for p in _db.query(DeletionProposal).all()]
check("music_service ran off the loop", _music == [False])
check("the scan still wrote its proposal",
      out is None and rows == [("Old Song", "pending", "music")])

check.done()
