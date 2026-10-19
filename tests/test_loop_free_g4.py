"""Plex sync, episodic memory and the Plex write / cache helpers run their
database work off the event loop.

Each scenario drives a converted coroutine against a temp file database with
the loop guard in raise mode, then checks the side effect is unchanged:

  * sync_plex_history: the plex_sync_running lock, the cooldown read, the
    library config and the per-section 404 streak all go through workers;
    the lock is released and both streak counters land as before
  * cleanup_orphan_watch_history (a deletion path): scan, guard and delete in
    one write; only the orphan goes
  * handle_protection_intent (a protection path): ProtectedMedia row, open
    proposal rejected, resolution logged
  * write_memory / retrieve_memories: the row lands and the access stamp too
  * push_collections, get_machine_identifier, add_to_watchlist: app_state and
    the token read are off the loop
  * listenbrainz / studio notes: every cache call runs on a worker thread

    python tests/test_loop_free_g4.py
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import types
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402

import src.database.connection as conn_mod  # noqa: E402
from src.database import loop_guard  # noqa: E402
from src.database.models import (Base, CuratorResolutionLog, DeletionProposal,  # noqa: E402
                                 EpisodicMemory, LibraryConfig, ProtectedMedia,
                                 User, WatchHistoryEntry)
from src.services import app_state  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


tmp = Path(tempfile.mkdtemp())
eng = create_engine(f"sqlite:///{(tmp / 'g4.db').as_posix()}",
                    connect_args={"check_same_thread": False})
Base.metadata.create_all(eng)
conn_mod.engine = eng
conn_mod.SessionLocal.configure(bind=eng)
loop_guard.install(eng)
loop_guard.serving(True)

import src.services.episodic_memory as em  # noqa: E402
import src.services.listenbrainz as lb  # noqa: E402
import src.services.plex_collections as pc  # noqa: E402
import src.services.plex_playlists as pp  # noqa: E402
import src.services.plex_sync as ps  # noqa: E402
import src.services.plex_watchlist as pw  # noqa: E402
import src.services.size_norms as size_norms  # noqa: E402
import src.services.studio_notes as sn  # noqa: E402

MINE = ("plex_sync.py", "episodic_memory.py", "plex_playlists.py", "plex_collections.py",
        "plex_watchlist.py", "listenbrainz.py", "studio_notes.py")


def run_guarded(coro_fn):
    """Run one coroutine in raise mode. Returns (result, error, my_sites)."""
    loop_guard.reset()
    os.environ["CURATARR_LOOP_GUARD"] = "raise"
    try:
        try:
            return asyncio.run(coro_fn()), None, _my_sites()
        except Exception as e:  # noqa: BLE001 - reported by the check
            return None, e, _my_sites()
    finally:
        os.environ.pop("CURATARR_LOOP_GUARD", None)


def _my_sites():
    return [s["site"] for s in loop_guard.snapshot()["sites"]
            if any(f in s["site"] for f in MINE)]


def ok(res, err, sites):
    return err is None and sites == []


with conn_mod.get_db_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True))
    db.add(User(id=2, plex_user_id="22", plex_username="partner", plex_token=None))
    db.add(LibraryConfig(plex_section_key="1", plex_section_title="Films",
                         plex_section_type="movie", media_category="movie"))
    db.add(LibraryConfig(plex_section_key="2", plex_section_title="Old Films",
                         plex_section_type="movie", media_category="movie"))
    db.commit()


# ── Plex fakes ──────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


ROUTES: dict = {}


class _Client:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        path = url.split("http://plex", 1)[-1]
        status, body = ROUTES.get(path, (404, {}))
        return _Resp(status, body)


ps.httpx = types.SimpleNamespace(AsyncClient=_Client, Timeout=lambda *a, **k: None,
                                 HTTPError=Exception, TimeoutException=TimeoutError)
ps.settings = types.SimpleNamespace(effective_plex_url="http://plex",
                                    effective_plex_token="tok", PLEX_CLIENT_ID="cid")

# ── 1. sync_plex_history: lock, cooldown, config, 404 streak ───────────────
print("sync_plex_history")
app_state.set_state("plex_section_missing:1", "2")    # an old streak to end
ROUTES.update({
    "/library/sections/1/all": (200, {"MediaContainer": {"Metadata": []}}),
    "/library/sections/2/all": (404, {}),
    "/library/sections": (200, {"MediaContainer": {"Directory": [{"key": "1"}]}}),
})
res, err, sites = run_guarded(lambda: ps.sync_plex_history(force=True))
check("the sync runs with no statement on the loop", ok(res, err, sites))
check("...and returns its usual nothing-new answer with the 404 section skipped",
      (res or {}).get("synced") == 0 and (res or {}).get("skipped_sections") == ["Old Films"])
check("the lock is released", app_state.get_state("plex_sync_running") == "0")
check("an answering section ends its 404 streak",
      app_state.get_state("plex_section_missing:1") == "0")
with conn_mod.get_db_session() as db:
    n_configs = db.query(LibraryConfig).count()
check("a confirmed miss counts one more on the streak, config kept",
      app_state.get_state("plex_section_missing:2") == "1" and n_configs == 2)

# ── 2. cleanup_orphan_watch_history (deletion path) ────────────────────────
print("cleanup_orphan_watch_history")
t0 = datetime(2026, 9, 1)
with conn_mod.get_db_session() as db:
    for i, key in enumerate(["100", "101", "102", "103", "104", "200"]):
        db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id=key,
                                 title=f"T{key}", viewed_at=t0 + timedelta(hours=i),
                                 media_type="movie"))
    db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id="spotify:x",
                             title="S", viewed_at=t0, media_type="music"))
    db.commit()
live = {"MediaContainer": {"totalSize": 5, "Metadata": [
    {"ratingKey": k} for k in ("100", "101", "102", "103", "104")]}}
ROUTES["/library/sections/1/all"] = (200, live)
ROUTES["/library/sections/2/all"] = (200, live)
app_state.set_state("recs_invalidate_at", "")
res, err, sites = run_guarded(lambda: ps.cleanup_orphan_watch_history())
check("orphan cleanup runs with no statement on the loop", ok(res, err, sites))
check("...and deletes exactly the orphan",
      (res or {}).get("deleted") == 1 and (res or {}).get("examined") == 6)
with conn_mod.get_db_session() as db:
    keys = {r.plex_item_id for r in db.query(WatchHistoryEntry).all()}
check("the orphan row is gone, live and Spotify rows stay",
      "200" not in keys and {"100", "104", "spotify:x"} <= keys)
check("recommendations are invalidated", bool(app_state.get_state("recs_invalidate_at")))

# ── 3. handle_protection_intent (protection path) ──────────────────────────
print("handle_protection_intent")
with conn_mod.get_db_session() as db:
    db.add(DeletionProposal(user_id=1, media_id="m1", title="Keep Me", service="radarr",
                            status="pending"))
    db.commit()
size_norms.is_bloated_by_title = lambda title, media_type=None: False
line = ("ACTION: PROTECT_MEDIA | TITLE: Keep Me | REASON: comfort film | "
        "RESOLUTION: override | CURATOR_STANCE: delete it | OVERRIDE_REASON: nostalgia")
res, err, sites = run_guarded(lambda: em.handle_protection_intent(1, line, category="movie"))
check("protection runs with no statement on the loop", ok(res, err, sites))
check("...and reports the title protected", "permanently protected" in (res or ""))
with conn_mod.get_db_session() as db:
    pm = db.query(ProtectedMedia).filter_by(user_id=1, identifier="Keep Me").first()
    dp = db.query(DeletionProposal).filter_by(title="Keep Me").first()
    crl = db.query(CuratorResolutionLog).filter_by(title="Keep Me").first()
    pm_reason, dp_status = (pm.reason if pm else None), dp.status
    crl_row = (crl.resolution_type, crl.override_reason) if crl else None
check("the ProtectedMedia row is written", pm_reason == "comfort film")
check("the open proposal is rejected", dp_status == "rejected")
check("the override is logged", crl_row == ("override", "nostalgia"))

# ── 4. write_memory / retrieve_memories ────────────────────────────────────
print("episodic memory")


async def _embed(text, kind="document"):
    return [1.0, 0.0]


em._embed = _embed


async def _write_and_read():
    mid = await em.write_memory(1, "preference", "The user values slow-burn sci-fi.",
                                media_category="movie")
    got = await em.retrieve_memories(1, "sci-fi", media_category="movie")
    return mid, got


res, err, sites = run_guarded(_write_and_read)
check("write + retrieve run with no statement on the loop", ok(res, err, sites))
mid, got = res or (None, [])
check("the memory id comes back and retrieval serves it",
      mid is not None and [m["id"] for m in got] == [mid])
with conn_mod.get_db_session() as db:
    row = db.query(EpisodicMemory).filter_by(id=mid).first()
    access = row.access_count if row else None
check("the served memory's access is stamped", access == 1)

# ── 5. Plex write helpers: collections, identity, watchlist token ──────────
print("plex write helpers")


async def _no_collections(section_key):
    return []


pc.list_collections = _no_collections
app_state.set_state("curatarr_collections", json.dumps({"designs": [{"section_key": "1"}]}))
res, err, sites = run_guarded(lambda: pc.push_collections([]))
check("push_collections runs with no statement on the loop", ok(res, err, sites))
snap = json.loads(app_state.get_state("curatarr_collections") or "{}")
check("...and replaces the snapshot", res == {"created": 0, "deleted": 0, "failed": 0}
      and snap.get("designs") == [] and "pushed_at" in snap)

app_state.set_state("plex_machine_identifier", "mid-123")
res, err, sites = run_guarded(lambda: pp.get_machine_identifier())
check("the cached machine identifier is read off the loop", ok(res, err, sites) and res == "mid-123")

res, err, sites = run_guarded(lambda: pw.add_to_watchlist(2, "Arrival"))
check("the watchlist token read is off the loop",
      ok(res, err, sites) and res == {"ok": False, "error": "user has no plex.tv token on file"})

# ── 6. metadata cache calls from coroutines ────────────────────────────────
print("metadata cache")
loop_threads: list = []


class _ThreadCache:
    def __init__(self, store):
        self.store, self.threads = store, []

    def _seen(self):
        self.threads.append(threading.get_ident())

    def get_cache(self, key):
        self._seen()
        return self.store.get(key)

    def set_cache(self, key, value, days=7):
        self._seen()
        self.store[key] = {"response": value}


async def _on_loop(coro):
    loop_threads.append(threading.get_ident())
    return await coro


lb._auth_headers = lambda: {"Authorization": "Token t"}
c1 = _ThreadCache({"lb:artist:mb1": {"response": {"total_listens": 7}}})
loop_threads.clear()
out = asyncio.run(_on_loop(lb.fetch_artist_popularity("mb1", cache=c1)))
check("listenbrainz: the cache hit is served, no cache call on the loop thread",
      out == {"total_listens": 7} and c1.threads and loop_threads[0] not in c1.threads)

c2 = _ThreadCache({sn._cache_key("Madhouse"): {"response": {"note": "Known for X."}}})
loop_threads.clear()
out = asyncio.run(_on_loop(sn.ensure_studio_note("Madhouse", cache=c2)))
check("studio note: the cache hit is served, no cache call on the loop thread",
      out == "Known for X." and c2.threads and loop_threads[0] not in c2.threads)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
