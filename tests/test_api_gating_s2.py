"""Member-reachable endpoints that did too much, or told too much.

  * POST /history/recompute-taste: no budget, no one-at-a-time guard
  * PATCH /users/{id}: an admin could deactivate themselves, or the last
    active admin, which DELETE already refused
  * GET /library/defaults and /libraries/discover: root folders, section
    paths and household play counts for any member
  * GET /library/items: root folders and raw connector text for members,
    and an unbudgeted forced live arr pull
  * GET /recommendations/?source=library: the arr LAN link for members
  * /library/search 502 and /chat/flush-memories: raw exception text
  * POST /chat/correct-anchor: unbudgeted drops of the shared cache

Each check fails on the code before this change.

    python tests/test_api_gating_s2.py
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

from fastapi import BackgroundTasks, FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.routers.auth import get_current_user  # noqa: E402
from src.services import rate_limit as _rl  # noqa: E402

from _checks import check  # noqa: E402


def status_of(fn, *a, **kw):
    """HTTP status a handler call ends in (200 when it returns)."""
    try:
        r = fn(*a, **kw)
        if asyncio.iscoroutine(r):
            asyncio.run(r)
        return 200
    except HTTPException as e:
        return e.status_code


ADMIN = SimpleNamespace(id=1, is_admin=True)
MEMBER = SimpleNamespace(id=2, is_admin=False)
LAN = "192.168.1.100"

# ── D1 recompute-taste ───────────────────────────────────────────────────────
print("recompute-taste")
import src.routers.history as hist  # noqa: E402
import src.services.taste_engine as taste  # noqa: E402

_rl.reset()
bt = BackgroundTasks()
first = status_of(hist.recompute_taste, bt, user=MEMBER)
second = status_of(hist.recompute_taste, BackgroundTasks(), user=MEMBER)
check("a second recompute while one runs answers 409", first == 200 and second == 409)
check("another user is not blocked by it",
      status_of(hist.recompute_taste, BackgroundTasks(), user=SimpleNamespace(id=3, is_admin=False)) == 200)

calls = []


async def _boom(uid):
    calls.append(uid)
    raise RuntimeError("gpu fell over")

taste.compute_all_taste_vectors = _boom
try:
    asyncio.run(hist._run_taste_bg(MEMBER.id))
except RuntimeError:
    pass
check("the guard is released when the run fails", calls == [2] and not _rl.TASTE_RECOMPUTE_IN_FLIGHT.busy(2))
codes = []
for _ in range(_rl.TASTE_RECOMPUTES_PER_10MIN):
    codes.append(status_of(hist.recompute_taste, BackgroundTasks(), user=MEMBER))
    _rl.TASTE_RECOMPUTE_IN_FLIGHT.leave(MEMBER.id)
check("starts beyond the per-user budget answer 429", codes[-1] == 429 and codes[0] == 200)

# ── D2 PATCH /users ──────────────────────────────────────────────────────────
print("PATCH /users last-admin guard")
import src.routers.users as users  # noqa: E402
from src.schemas.user import UserUpdate  # noqa: E402

db = conn_mod.SessionLocal()
a1 = User(plex_user_id="a1", plex_username="a1", is_admin=True, is_active=True)
a2 = User(plex_user_id="a2", plex_username="a2", is_admin=True, is_active=True)
m1 = User(plex_user_id="m1", plex_username="m1", is_admin=False, is_active=True)
db.add_all([a1, a2, m1])
db.commit()


def patch(target, caller, active):
    try:
        users.update_user(target.id, UserUpdate(is_active=active), _admin=caller, db=db)
        return 200, ""
    except HTTPException as e:
        db.rollback()
        return e.status_code, e.detail

code, detail = patch(a1, a1, False)
db.refresh(a1)
check("an admin cannot deactivate themselves (400, DELETE's wording)",
      code == 400 and "themselves" in detail and a1.is_active)
code, _ = patch(a2, a1, False)
db.refresh(a2)
check("deactivating another admin while one stays active works", code == 200 and not a2.is_active)
# A caller that is no longer in the active-admin set (deactivated mid-request)
# must not take the last one down with it.
code, detail = patch(a1, SimpleNamespace(id=999, is_admin=True), False)
db.refresh(a1)
check("the last active admin cannot be deactivated", code == 400 and "last active admin" in detail
      and a1.is_active)
tv = m1.token_version or 0
code, _ = patch(m1, a1, False)
db.refresh(m1)
check("deactivating a member still works and revokes tokens",
      code == 200 and not m1.is_active and (m1.token_version or 0) == tv + 1)
check("re-activating an admin is never refused", patch(a2, a1, True)[0] == 200)
db.close()

# ── D3 admin-only GETs ───────────────────────────────────────────────────────
print("admin-only GETs")
import src.routers.library as lib  # noqa: E402
import src.routers.libraries as libs  # noqa: E402

app = FastAPI()
app.include_router(lib.router, prefix="/api/library")
app.include_router(libs.router, prefix="/api/libraries")
client = TestClient(app)
lib._read_defaults = lambda svc: {"root_folder_path": "/storage/media/tv", "quality_profile_id": 4}


def as_user(u):
    app.dependency_overrides[get_current_user] = lambda: u

as_user(MEMBER)
check("member: /library/defaults is 403", client.get("/api/library/defaults").status_code == 403)
check("member: /libraries/discover is 403", client.get("/api/libraries/discover").status_code == 403)
as_user(ADMIN)
r = client.get("/api/library/defaults")
check("admin: /library/defaults unchanged",
      r.status_code == 200 and r.json()["sonarr"]["root_folder_path"] == "/storage/media/tv")
libs.settings = SimpleNamespace(effective_plex_url="", effective_plex_token="")   # no network
check("admin: /libraries/discover passes the gate (503: Plex not configured here)",
      client.get("/api/libraries/discover").status_code == 503)
app.dependency_overrides.clear()

# ── D3 /library/items ────────────────────────────────────────────────────────
print("/library/items")
import src.cache.metadata_cache as mc_mod  # noqa: E402


class _FakeCache:
    def __init__(self, *a, **kw):
        self.conn = SimpleNamespace(execute=lambda *a, **kw: SimpleNamespace(rowcount=1),
                                    commit=lambda: None)

    def close(self):
        pass

_real_cache = mc_mod.MetadataCache
mc_mod.MetadataCache = _FakeCache
lib._enrichment_lookup = lambda svc, raw, cache: None
_RAW = [{"id": 7, "title": "Bluey", "rootFolderPath": "/storage/media/anime", "tags": []}]
fetches = []


async def _stale(service, *, force_refresh=False):
    fetches.append(force_refresh)
    return _RAW, [], {"source": "stale", "age_s": 60, "ttl_s": 900,
                      "stale_error": f"Cannot connect to host {LAN}:8989"}

lib._fetch_arr_library = _stale


def items(user, **kw):
    return asyncio.run(lib.library_items("sonarr", sort="title_asc", needs_enrichment=False,
                                         root_filter=kw.get("root_filter"), curatarr_only=False,
                                         search=None, limit=200, refresh=kw.get("refresh", False),
                                         _user=user))

a = items(ADMIN)
m = items(MEMBER)
check("admin keeps root_folder and stale_error",
      a["items"][0]["root_folder"] == "/storage/media/anime" and LAN in a["cache"]["stale_error"])
check("member: no root_folder, no stale_error, still the 'stale' signal",
      "root_folder" not in m["items"][0] and "stale_error" not in m["cache"]
      and m["cache"]["source"] == "stale")
check("member: root_filter still splits on the server-side path",
      items(MEMBER, root_filter="anime")["total"] == 1 and items(MEMBER, root_filter="tv")["total"] == 0)
_rl.reset()
codes = [status_of(lambda: items(MEMBER, refresh=True)) for _ in range(_rl.ARR_REFRESHES_PER_10MIN + 1)]
check("member: forced refreshes beyond the budget answer 429 (no live pull)",
      codes[:-1] == [200] * _rl.ARR_REFRESHES_PER_10MIN and codes[-1] == 429
      and fetches.count(True) == _rl.ARR_REFRESHES_PER_10MIN)
check("admin: refresh is not budgeted",
      all(status_of(lambda: items(ADMIN, refresh=True)) == 200 for _ in range(6)))


async def _down(service, *, force_refresh=False):
    raise HTTPException(502, lib._format_arr_error(service, OSError(f"Cannot connect to host {LAN}:8989")))

lib._fetch_arr_library = _down
try:
    items(MEMBER)
    mdetail = ""
except HTTPException as e:
    mdetail = e.detail
try:
    items(ADMIN)
    adetail = ""
except HTTPException as e:
    adetail = e.detail
check("first-fetch 502: member gets no address, admin keeps the detail",
      mdetail == "sonarr unreachable" and LAN in adetail)

# ── D4 /library/search 502 ──────────────────────────────────────────────────
print("/library/search")
lib._get_arr_url_key = lambda svc: (f"http://{LAN}:7878", "key")


class _DeadClient:
    async def __aenter__(self):
        raise OSError(f"Cannot connect to host {LAN}:7878 ssl:default")

    async def __aexit__(self, *a):
        return False

lib._make_client = lambda *a, **kw: _DeadClient()
_rl.reset()
try:
    asyncio.run(lib.library_search("radarr", "dune", _user=MEMBER))
    sdetail = ""
except HTTPException as e:
    sdetail = f"{e.status_code} {e.detail}"
check("a failed lookup is a fixed 502 without the arr's address",
      sdetail.startswith("502") and LAN not in sdetail and "radarr" in sdetail)
mc_mod.MetadataCache = _real_cache

# ── D3 recommendations ?source=library ─────────────────────────────────────
print("recommendations source=library")
import src.routers.recommendations as recs  # noqa: E402
import src.services.recommendations_engine as eng  # noqa: E402


async def _unwatched(uid, cat):
    return [{"title": "Dune", "category": "movie", "size_mb": 2048,
             "arr_url": f"http://{LAN}:7878/movie/dune"}]


async def _score(uid, cat, items, top_n=50):
    return items


async def _gen(user_id, category, limit, arr_library=None):
    return [{"title": "Dune"}]


async def _poster(title, cat):
    return None

recs._fetch_arr_unwatched = _unwatched
recs._fetch_poster = _poster
eng.score_arr_items = _score
eng.generate_recommendations = _gen


def lib_recs(user):
    return asyncio.run(recs._get_recommendations_impl("movie", 8, False, "library", None, user))

ra, rm = lib_recs(ADMIN)["recommendations"][0], lib_recs(MEMBER)["recommendations"][0]
check("admin keeps the arr link", ra["arr_url"].startswith(f"http://{LAN}"))
check("member gets a blank arr_url (the UI hides 'Open in')", rm["arr_url"] == "" and rm["size_gb"] == 2.0)

# ── D4 flush-memories ───────────────────────────────────────────────────────
print("chat")
import src.routers.chat as chat  # noqa: E402
import src.services.episodic_memory as epi  # noqa: E402


async def _flush_boom(uid, tid):
    raise RuntimeError(f"sqlite3.OperationalError at /data/{LAN}/curatarr.db")

epi.flush_thread_extraction = _flush_boom
_rl.reset()
r = asyncio.run(chat.flush_memories(chat.FlushMemoriesRequest(thread_id="general"), user=MEMBER))
check("flush-memories failure is ok:false with a generic error",
      r["ok"] is False and r["error"] and LAN not in r["error"] and "OperationalError" not in r["error"])

# ── D5 correct-anchor ───────────────────────────────────────────────────────
deleted = []


class _AnchorCache(_FakeCache):
    def __init__(self, *a, **kw):
        self.conn = SimpleNamespace(
            execute=lambda sql, args: deleted.append(args[0]) or SimpleNamespace(rowcount=1),
            commit=lambda: None)

mc_mod.MetadataCache = _AnchorCache
chat._get_thread_active_title = lambda uid, tid: ("Dune", None, "movie")
chat._own_thread_active_titles = lambda uid: []
_rl.reset()
codes = [status_of(chat.correct_chat_anchor, chat.CorrectAnchorRequest(thread_id="general"), user=MEMBER)
         for _ in range(_rl.ANCHOR_CORRECTIONS_PER_10MIN + 1)]
n_ok = _rl.ANCHOR_CORRECTIONS_PER_10MIN
check("correct-anchor works, then answers 429 past the budget",
      codes[:-1] == [200] * n_ok and codes[-1] == 429 and len(deleted) == n_ok * len(set(deleted)))
chat._get_thread_active_title = lambda uid, tid: None
check("the no-anchor answer costs no budget",
      status_of(chat.correct_chat_anchor, chat.CorrectAnchorRequest(), user=MEMBER) == 200)
mc_mod.MetadataCache = _real_cache

check.done()
