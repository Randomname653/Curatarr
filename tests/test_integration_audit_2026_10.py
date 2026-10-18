"""Post-refactor integration audit (2026-10-15): residual leaks and loop stalls.

Each check fails on the code before the fix:

* MetadataCache opened a new SQLite connection (PRAGMAs + schema DDL) per
  construction — the deletion scorer did it once per candidate, on the loop —
  and a handle dropped on an exception kept its open transaction.
* set_state read the row, then UPDATEd or INSERTed: two first writers of a new
  key both took the INSERT branch and one died on the primary key.
* the game watcher ran nvidia-smi / psutil / a synchronous GET on the event
  loop and wrote game_active every 30 s whether or not it changed.
* task_monitor broadcasts from a worker thread put into an asyncio.Queue
  directly (not thread-safe).
* resolve_memory_conflicts kept its session open across the LLM calls and ran
  a pure-Python cosine per memory on the loop.
* _migrate_columns committed each ALTER on its own, so a failure after an
  ALTER kept the column and lost its one-shot backfill for good.
* the household-floor guard never reached the Deletions card.
* the chat stream re-opened a pooled connection (a lazy ``user.id`` read after
  the commit) and held it for the whole stream.

Temp file databases only; the live DB and cache are never touched.

    python tests/test_integration_audit_2026_10.py
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_tmp = Path(tempfile.mkdtemp(prefix="curatarr-integration-"))
DB_FILE = (_tmp / "curatarr.db").resolve()
CACHE_FILE = (_tmp / "cache" / "enrichment.db").resolve()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["ENRICHMENT_CACHE"] = str(CACHE_FILE)

from src.config import settings  # noqa: E402

settings.ENRICHMENT_CACHE = CACHE_FILE

import src.database.connection as conn_mod  # noqa: E402
from src.database.connection import get_db_session, init_db  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


check("test DB is the temp file, not the real one",
      conn_mod.engine.url.database == DB_FILE.as_posix())
init_db()

# ── 1. MetadataCache: one connection per thread, bulk reads, no stuck txn ────
from src.cache import metadata_cache as mc  # noqa: E402

_real_connect = sqlite3.connect
_connects = []


def _counting_connect(*a, **kw):
    _connects.append(a[0] if a else kw.get("database"))
    return _real_connect(*a, **kw)


mc.sqlite3.connect = _counting_connect
try:
    for _ in range(200):          # the scorer's old per-candidate pattern
        c = mc.MetadataCache()
        c.get_cache("enriched:movie:1")
        c.close()
    main_thread_connects = len(_connects)
    t = threading.Thread(target=lambda: [mc.MetadataCache().get_cache("x") for _ in range(50)])
    t.start()
    t.join()
finally:
    mc.sqlite3.connect = _real_connect
check("200 handles on one thread open ONE connection (was 200)", main_thread_connects == 1)
check("...and another thread gets its own, once", len(_connects) == 2)

with mc.MetadataCache() as cache:
    cache.set_cache("enriched:movie:10", {"rating": 7.5, "vote_count": 900, "genres": ["Drama"]})
    cache.set_cache("enriched:movie:11", {"rating": 0, "genres": ["Horror"]})
    now = datetime.now()
    # an un-versioned v1 row, a v1 row shadowed by v2, an expired row
    cache.conn.executemany(
        "INSERT OR REPLACE INTO api_cache VALUES (?,?,?,?)",
        [("enriched:movie:12", json.dumps({"rating": 6.0, "genres": ["v1"]}), "x",
          (now + timedelta(days=1)).isoformat()),
         ("enriched:movie:10", json.dumps({"rating": 1.0}), "x",
          (now + timedelta(days=1)).isoformat()),
         ("v2:enriched:movie:13", json.dumps({"rating": 9.0}), "x",
          (now - timedelta(days=1)).isoformat())])
    cache.conn.commit()
    keys = [f"enriched:movie:{i}" for i in (10, 11, 12, 13, 14)]
    many = cache.get_cache_many(keys)
    check("bulk read returns exactly what get_cache returns, key by key",
          all(many.get(k) == cache.get_cache(k) for k in keys))
    check("bulk read: v2 beats v1, v1 is a fallback, expired and absent are left out",
          many["enriched:movie:10"]["response"]["rating"] == 7.5
          and many["enriched:movie:12"]["response"]["rating"] == 6.0
          and "enriched:movie:13" not in many and "enriched:movie:14" not in many)
    big = cache.get_cache_many([f"nope:{i}" for i in range(2000)] + keys)
    check("bulk read chunks past SQLite's variable limit", len(big) == 3)

a = mc.MetadataCache()
b = mc.MetadataCache()
b.conn.execute("INSERT INTO api_cache VALUES ('half-done','{}','x','9999')")
a.close()
check("an uncommitted write survives while another handle is still open",
      b.conn.in_transaction)
b.close()
check("the last handle's release rolls back what was left uncommitted",
      mc.MetadataCache().get_cache("half-done") is None
      and not mc.MetadataCache().conn.in_transaction)


def _drop_handle_mid_txn():
    leaked = mc.MetadataCache()
    leaked.conn.execute("INSERT INTO api_cache VALUES ('leaked','{}','x','9999')")
    raise RuntimeError("caller crashed before close()")


try:
    _drop_handle_mid_txn()
except RuntimeError:
    pass
import gc  # noqa: E402

gc.collect()
probe = sqlite3.connect(CACHE_FILE, timeout=0.5)
try:
    probe.execute("BEGIN IMMEDIATE")
    probe.execute("ROLLBACK")
    unlocked = True
except sqlite3.OperationalError:
    unlocked = False
finally:
    probe.close()
check("a handle dropped on an exception leaves no write lock behind", unlocked)

seen = {}
h = mc.MetadataCache()


def _use_from_worker():
    seen["conn"] = h.conn
    seen["val"] = h.get_cache("enriched:movie:10")


w = threading.Thread(target=_use_from_worker)
w.start()
w.join()
check("a handle used from another thread uses THAT thread's connection",
      seen["conn"] is not h.conn and seen["val"]["response"]["rating"] == 7.5)
h.close()

# ── 2. deletion scorer: one batched rating read, same answers ───────────────
from src.services import recommendations_engine as eng  # noqa: E402

items = [{"tmdb_id": 10, "title": "A"}, {"tmdb_id": 11, "title": "B"},
         {"tmdb_id": 12, "title": "C"}, {"title": "No Id"}]
bulk = eng._bulk_cached_ratings(items, "movie")
check("bulk ratings == per-item ratings for every candidate",
      all(bulk[eng._rating_cache_key(it, "movie")] == eng._get_cached_rating(it, "movie")
          for it in items))
check("a 0 rating stays 'unrated' but keeps its genres (Pass 54)",
      bulk["enriched:movie:11"] == (None, None, ["Horror"]))

_calls = {"many": 0, "one": 0}
_orig_many, _orig_one = mc.MetadataCache.get_cache_many, mc.MetadataCache.get_cache
mc.MetadataCache.get_cache_many = lambda self, k: (_calls.__setitem__("many", _calls["many"] + 1)
                                                   or _orig_many(self, k))
mc.MetadataCache.get_cache = lambda self, k: (_calls.__setitem__("one", _calls["one"] + 1)
                                              or _orig_one(self, k))
try:
    eng._bulk_cached_ratings(items * 100, "movie")
finally:
    mc.MetadataCache.get_cache_many, mc.MetadataCache.get_cache = _orig_many, _orig_one
check("400 candidates cost one bulk read, zero point lookups",
      _calls == {"many": 1, "one": 0})

src_eng = (ROOT / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
gen = src_eng.split("async def generate_deletion_proposals(")[1].split("\nasync def ")[0]
check("phase A's DB reads run in one worker-thread call",
      "await asyncio.to_thread(_load_phase_a)" in gen)
check("the embedding prefetch runs on the Chroma thread",
      "run_chroma(chroma_db.get_by_ids" in gen)
check("the scoring loop no longer opens the cache per candidate",
      "_get_cached_rating(item, category)" not in gen.split("_load_phase_a)")[1].split(
          "Phase B")[0].split("rating_ctx, _, cached_genres")[0])
check("the tech index and the plays aggregate load off the loop",
      "await asyncio.to_thread(load_tech_index)" in gen
      and "await asyncio.to_thread(_load_play_rows)" in gen)

# ── 3. set_state: atomic upsert; only-if-changed heartbeat writes ───────────
from src.services import app_state  # noqa: E402

errors = []
barrier = threading.Barrier(8)


def _first_writer(i):
    try:
        barrier.wait()
        app_state.set_state("race_key", f"v{i}")
    except Exception as e:  # noqa: BLE001
        errors.append(e)


threads = [threading.Thread(target=_first_writer, args=(i,)) for i in range(8)]
for th in threads:
    th.start()
for th in threads:
    th.join()
check("8 concurrent first writers of a new key: no IntegrityError", not errors)
check("...and exactly one row holds the last value",
      (app_state.get_state("race_key") or "").startswith("v"))

app_state.set_state("hb", "1")
with get_db_session() as db:
    from src.database.models import AppState  # noqa: E402
    ts1 = db.query(AppState.updated_at).filter(AppState.key == "hb").scalar()
time.sleep(0.01)
changed = app_state.set_states_if_changed({"hb": "1", "new_one": "x"})
with get_db_session() as db:
    ts2 = db.query(AppState.updated_at).filter(AppState.key == "hb").scalar()
check("an unchanged heartbeat value is not rewritten", ts1 == ts2)
check("changed keys come back with their previous value", changed == {"new_one": None})
check("...and a value change is reported against the old one",
      app_state.set_states_if_changed({"hb": "0"}) == {"hb": "1"})

# ── 4. game watcher: blocking probes off the loop, writes only on change ────
from src.services import scheduler  # noqa: E402
import src.services.process_monitor as pm  # noqa: E402
import src.services.llm_lane as lane  # noqa: E402

_orig = (pm.is_game_running, pm.unload_llm_models, lane.status)


def _slow_probe():
    time.sleep(0.4)            # nvidia-smi / psutil stand-in: blocks its thread
    return False


async def _no_unload():
    return []


pm.is_game_running = _slow_probe
pm.unload_llm_models = _no_unload
lane.status = lambda: {"gpu_pressed": False, "game": False, "reason": "", "summarizer": "gpu"}
_writes = []
_orig_upsert = app_state._UPSERT_SQL


async def _watch_twice():
    ticks = 0
    stop = asyncio.Event()

    async def _heartbeat():
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.02)

    hb = asyncio.create_task(_heartbeat())
    await scheduler.job_game_watcher()
    await scheduler.job_game_watcher()
    stop.set()
    await hb
    return ticks


try:
    with get_db_session() as db:
        db.query(AppState).filter(AppState.key.in_(
            ["game_active", "llm_lane", "llm_lane_reason"])).delete(synchronize_session=False)
    t0 = time.monotonic()
    ticks = asyncio.run(_watch_twice())
    elapsed = time.monotonic() - t0
    with get_db_session() as db:
        ga_ts = db.query(AppState.updated_at).filter(AppState.key == "game_active").scalar()
    t_before = ga_ts
    asyncio.run(scheduler.job_game_watcher())
    with get_db_session() as db:
        ga_ts2 = db.query(AppState.updated_at).filter(AppState.key == "game_active").scalar()
finally:
    pm.is_game_running, pm.unload_llm_models, lane.status = _orig
check("the loop kept running while the probe blocked (was frozen ~0.8 s)",
      ticks >= int(elapsed / 0.02 * 0.5))
check("the watcher persisted game_active and the lane",
      app_state.get_state("game_active") == "0" and app_state.get_state("llm_lane") == "free")
check("a tick with nothing changed writes nothing (was a write every 30 s)",
      t_before is not None and ga_ts2 == t_before)

# ── 5. task_monitor: a broadcast from a worker thread reaches the stream ────
from src.services.task_monitor import task_monitor  # noqa: E402


async def _thread_broadcast():
    q = task_monitor.subscribe()
    try:
        task = task_monitor.create(name="t", category="test")
        await asyncio.to_thread(task_monitor.update, task, message="from a thread")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            snap = await asyncio.wait_for(q.get(), timeout=2)
            if any("from a thread" in json.dumps(t, default=str) for t in snap):
                return True
        return False
    finally:
        task_monitor.unsubscribe(q)


check("a worker-thread progress update wakes the SSE subscriber",
      asyncio.run(_thread_broadcast()))

# ── 6. memory conflicts: scan off the loop, short verdict writes ────────────
import src.services.episodic_memory as em  # noqa: E402
from src.database.models import EpisodicMemory  # noqa: E402

old_ts = datetime.utcnow() - timedelta(hours=1)
with get_db_session() as db:
    twin = EpisodicMemory(user_id=7, memory_type="preference", content="I love slow cinema",
                          metadata_json="{}", embedding_json=json.dumps([1.0, 0.0, 0.0]),
                          importance=0.5, created_at=old_ts)
    other = EpisodicMemory(user_id=7, memory_type="preference", content="D&D nights",
                           metadata_json="{}", embedding_json=json.dumps([0.0, 1.0, 0.0]),
                           importance=0.5, created_at=old_ts)
    olddim = EpisodicMemory(user_id=7, memory_type="preference", content="old model",
                            metadata_json="{}", embedding_json=json.dumps([1.0, 0.0]),
                            importance=0.5, created_at=old_ts)
    new = EpisodicMemory(user_id=7, memory_type="preference", content="slow cinema is my thing",
                         metadata_json="{}", embedding_json=json.dumps([0.95, 0.05, 0.0]),
                         importance=0.5, created_at=datetime.utcnow())
    db.add_all([twin, other, olddim, new])
    db.flush()
    twin_id, other_id, new_id = twin.id, other.id, new.id

targets = em._conflict_targets(7, new_id, "")
check("similarity scan: only the near-duplicate is a target (other dims never match)",
      [t[0] for t in targets] == [twin_id])

_asked = []


class _FakeResp:
    status_code = 200

    def json(self):
        return {"message": {"content": '{"status": "REAFFIRMATION", "reason": "same"}'}}


class _FakeClient:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        # The scan's session must be closed by now: nothing is checked out
        # of the pool while the model thinks.
        _asked.append(conn_mod.engine.pool.checkedout())
        return _FakeResp()


_orig_client = em.httpx.AsyncClient
em.httpx.AsyncClient = _FakeClient
try:
    asyncio.run(em.resolve_memory_conflicts(7, new_id, "slow cinema is my thing", {}))
finally:
    em.httpx.AsyncClient = _orig_client
with get_db_session() as db:
    boosted = db.query(EpisodicMemory.importance).filter(EpisodicMemory.id == twin_id).scalar()
    gone = db.query(EpisodicMemory).filter(EpisodicMemory.id == new_id).first() is None
    untouched = db.query(EpisodicMemory.importance).filter(EpisodicMemory.id == other_id).scalar()
check("no pooled connection is held across the LLM call", _asked == [0])
check("a reaffirmation boosts the twin and folds the new copy into it",
      abs(boosted - 0.7) < 1e-9 and gone)
check("an unrelated preference is never touched", untouched == 0.5)

# ── 7. migrations: one transaction, loud failure, user_version recorded ─────
with conn_mod.engine.connect() as c:
    uv = c.exec_driver_sql("PRAGMA user_version").scalar()
check("init_db records the schema generation", uv == conn_mod.SCHEMA_VERSION)

legacy = _tmp / "legacy.db"
lc = sqlite3.connect(legacy)
lc.executescript("""
    CREATE TABLE enrichment_status (id INTEGER PRIMARY KEY, error TEXT, enriched_at DATETIME);
    INSERT INTO enrichment_status (error, enriched_at) VALUES ('Not found: x', '2026-09-01 00:00:00');
""")
lc.commit()
lc.close()
from sqlalchemy import create_engine, event  # noqa: E402

saved_engine = conn_mod.engine
leg = create_engine(f"sqlite:///{legacy.as_posix()}")


@event.listens_for(leg, "before_cursor_execute")
def _boom(conn, cursor, statement, params, context, executemany):
    if statement.startswith("UPDATE enrichment_status"):
        raise RuntimeError("crash during the backfill")


conn_mod.engine = leg
raised = False
try:
    conn_mod._migrate_columns()
except RuntimeError:
    raised = True
finally:
    conn_mod.engine = saved_engine
lc = sqlite3.connect(legacy)
cols = {r[1] for r in lc.execute("PRAGMA table_info(enrichment_status)")}
lc.close()
check("a failed migration step stops the boot (was a DEBUG line)", raised)
check("...and rolls back the ALTERs with it, so the backfill re-runs next boot",
      "attempt_count" not in cols)

event.remove(leg, "before_cursor_execute", _boom)
conn_mod.engine = leg
try:
    conn_mod._migrate_columns()
finally:
    conn_mod.engine = saved_engine
lc = sqlite3.connect(legacy)
row = lc.execute("SELECT attempt_count, next_retry_at FROM enrichment_status").fetchone()
lc.close()
check("the retried migration adds the column AND its backfill together",
      row[0] == 2 and row[1] is not None)
leg.dispose()

# ── 8. contracts: the household-floor guard reaches the card ────────────────
cand = {"score": 61.04, "score_terms": None}
check("a guarded verdict without score terms still carries the guard",
      eng._with_guard(None, cand, {"verdict": "STAGNANT", "_guard": "household_floor"})
      == {"total": 61.0, "factors": [], "guard": "household_floor"})
sf = {"total": 50.0, "factors": [{"key": "taste", "points": 40.0, "label": "x"}]}
check("...and is added to existing factors without dropping them",
      eng._with_guard(sf, cand, {"_guard": "household_floor"})["factors"] == sf["factors"])
check("an unguarded verdict leaves score_factors untouched",
      eng._with_guard(sf, cand, {"verdict": "CUT"}) is sf)
from src.routers.recommendations import _load_score_factors  # noqa: E402

stored = json.dumps(eng._with_guard(sf, cand, {"_guard": "household_floor"}))
check("the router hands the guard to the client", _load_score_factors(stored)["guard"]
      == "household_floor")
js = (ROOT / "frontend/js/deletions.js").read_text(encoding="utf-8")
check("the card explains a household-floor 'Your call'",
      "score_factors?.guard === 'household_floor'" in js and "_yourCallWhy(p)" in js)

# ── 9. chat stream: no pooled connection held while it streams ──────────────
chat_src = (ROOT / "src/routers/chat.py").read_text(encoding="utf-8")
body = chat_src.split("async def _send_message_guarded(")[1].split("\ndef _release_unstarted_chat")[0]
after = body.split("_uid = user.id")[1]
check("no ORM attribute read after the commit (it re-opened a transaction)",
      "user." not in after)
check("the chat path holds no request session (its DB work runs on worker threads)",
      "db: Session" not in body.split(")")[0] and "return StreamingResponse(stream" in after)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
