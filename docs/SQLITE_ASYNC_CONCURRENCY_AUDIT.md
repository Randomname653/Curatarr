# SQLite / AsyncIO Concurrency Audit — 2026-10-11

> **Dated report.** It describes the code at the commit named below: line numbers,
> counts and "open" items are from then, and status notes added later carry their
> date. For what is true today, see [the docs index](README.md#audit-reports--dated-snapshots).

Scope: database session lifecycles and engine/PRAGMA setup (`src/database/connection.py`,
`src/services/app_state.py`, `src/cache/metadata_cache.py`), background orchestration
(`src/services/scheduler.py`, `src/services/data_custodian.py`, `src/services/bg_tasks.py`,
`src/main.py` lifespan), `src/services/recommendations_engine.py`, the sync/enrichment
services they drive, migrations (`update_db.py` → `init_db()`), and every route handler
under `src/routers/`.

Method: manual reading plus AST sweeps over `src/`: route handlers by sync/async and DB use,
`with get_db_session()` blocks that contain an `await`, and flush/DML that runs before an
`await` with no commit in between. The three most severe findings were reproduced (see
"Verified" notes).

---

## 1. Concurrency & Database Health Summary

The engine setup is mostly right: WAL, `synchronous=NORMAL` and a `busy_timeout` are applied
to every pooled connection, and `autoflush=False` plus a commit-before-network habit keep
nearly all write transactions free of network I/O. The real weakness is the threading model.
All 141 route handlers are `async def`, and the scheduler and services call synchronous
SQLAlchemy from coroutines, so almost every query runs on the event-loop thread. A
`busy_timeout` of 60 s therefore stops being a wait and becomes a 60-second freeze of the whole
server, including SSE streams. If the lock holder is itself a coroutine suspended on that same
loop, the wait becomes a self-deadlock, and that is a reachable path today: `music_matcher`
flushes, yields, and holds the lock. Under heavy background scans the main failure modes are:

- that deadlock, followed by a `database is locked` cascade (`set_state` falls back to a
  further 120 s busy-wait);
- multi-second CPU and disk stalls from scoring and taste rebuilds run on the loop (ORM loads
  of 100k+ rows, Chroma bulk reads, NumPy, a per-candidate SQLite connect);
- a shutdown path that re-enables Syncthing while background writers and an unflushed `-wal`
  are still live.

On the migration side, every fresh install's first boot rebuilds `deletion_proposals` without
4 model columns, so every ORM query on that table fails until the next restart.

---

## 2. Detailed Technical Audit Table

| File / Function | Issue Type | Severity | Current Behavior & Failure Mode | Recommended Remediation |
| :--- | :--- | :--- | :--- | :--- |
| `src/services/music_matcher.py` > `match_spotify_to_plex()` (L192–245) | Transaction Bleed / Locking | **High** | Every 500 plays it calls `db.flush()`. That issues the UPDATEs, so pysqlite opens an implicit `BEGIN` and takes the WAL write lock. It then calls `await asyncio.sleep(0)` while still holding the lock. The lock is held for the entire match pass, because the only commit is at L240; the comment "flush … so progress is persistent" is wrong. Any other coroutine that writes synchronously during the yield, such as `job_game_watcher`'s unconditional `set_state("game_active")` every 30 s, busy-waits up to 60 s **on the loop thread the lock holder needs in order to resume**. The result is a 60 s whole-app freeze followed by `database is locked`. `set_state` then falls back to `force_set_state`, which busy-waits up to 120 s more. On top of that, `_stop_requested()` runs `get_state()` (a fresh pooled session) **once per play**. **Verified**: a stdlib repro of the pattern blocks for the full timeout, then raises `database is locked`. | Snapshot the work list in a short read, match in memory, and write each chunk in its **own** short `BEGIN IMMEDIATE` transaction on a worker thread (§3.3). Never leave a transaction open across `await`. Check the stop flag once per chunk, not once per row. |
| App-wide: `src/routers/*.py` (141 `async def` handlers, 0 sync; 52 use a `Session` directly) and all service coroutines calling `get_db_session()` | Event-Loop Blocking | **High** | Synchronous SQLAlchemy and sqlite3 calls run on the event-loop thread. With `busy_timeout=60000` (`connection.py` L42/L50), any contention freezes **every** request and SSE stream for up to 60 s. Contention sources include the `plex_sync` `to_thread` writer, `scripts/music_enricher.py` (a separate process by design), VACUUM, the online backup, and `update_db.py` run while the app is live. In-process writers end up "serialized" only because the loop is frozen. | Route DB work through `run_read()` / `run_write()` (§3.2). Writes go through a single-thread writer executor, so in-process writes never contend and `busy_timeout` only arbitrates against other processes. Convert pure-DB endpoints to plain `def` so FastAPI runs them in its threadpool. Once nothing busy-waits on the loop, lower `busy_timeout` to 10–15 s. |
| `src/database/connection.py` > `init_db()` → `_migrate_deletion_proposals_autoincrement()` (L293–413) | Migration | **High** | `DeletionProposal` declares `autoincrement=True` but not `sqlite_autoincrement=True`, so `create_all` emits a plain `INTEGER PRIMARY KEY`. On **every fresh install** the 90c check therefore fires on first boot and rebuilds the table from a **hard-coded** column list that lacks `score_factors`, `soulsync_ban`, `returned_at` and `returned_info`. `_migrate_columns` already ran, so nothing re-adds them until the next restart, and every ORM query on `DeletionProposal` raises `no such column`. On a legacy pre-90c DB the same rebuild drops those columns too. **Verified** against SQLAlchemy 2.0.54: `missing after first-boot init_db: ['returned_at','returned_info','score_factors','soulsync_ban']`, then `ORM query FAILS … no such column: deletion_proposals.score_factors`. | Add `{"sqlite_autoincrement": True}` to `__table_args__`. Generate the rebuild DDL from the model (`CreateTable(DeletionProposal.__table__)`) and copy only the columns the old and new tables share (§3.7). Drop the manual `sqlite_sequence` seed: SQLite maintains it on explicit-id inserts, and the seed adds a duplicate row. |
| `src/main.py` > `lifespan()` teardown (L166–206) + `scheduler.stop_scheduler()` | Lifecycle | **High** | `scheduler.shutdown(wait=False)` only stops APScheduler. The long-running work never gets cancelled or awaited: custodian queues, enrichment, the music pipeline and startup catch-ups all run as `_track_task` / `track_task` tasks. The `*_running` flags are reset to `"0"` while those tasks are still running and writing. There is no `PRAGMA wal_checkpoint` and no `engine.dispose()`. Then `sync_guard.disable()` re-enables Syncthing on `data/` while pooled connections are open and the `-wal` still holds uncheckpointed pages. That is exactly the "syncing an open WAL DB" corruption scenario Pass 100 exists to prevent. The leftover tasks are only cancelled later, when `asyncio.run` tears down, at an arbitrary `await`. | Teardown order: stop the scheduler → cancel **and await** all tracked tasks with a bounded timeout → reset flags → dispose the engine → `wal_checkpoint(TRUNCATE)` → dispose → only then `sync_guard.disable()` (§3.5). |
| `src/services/recommendations_engine.py` > `generate_deletion_proposals()` phase A (L1166–1720) | Event-Loop Blocking | **High** | From the DB load to the LLM phase the scoring path never awaits, apart from the SoulSync call. For each of the 4 categories it runs synchronously on the loop: the 90-day watch-history veto load (music: tens of thousands of rows), a concatenation of 60 days of assistant messages, `chroma_db.get_by_ids()` over the whole arr library (embedding read from disk), `_get_cached_rating()` **per candidate**, the NumPy cosine loop, `load_tech_index()`, and the grouped plays query. `_get_cached_rating()` opens a **new** `sqlite3` connection per candidate, with 4 PRAGMAs and 2 `CREATE … IF NOT EXISTS` statements each. The loop stalls for seconds per category, so the UI and SSE freeze during every ARR sync. | Split this into `_load_deletion_context()` (DB), `_bulk_cached_ratings()` (one cache connection), and a pure `_score_candidates()`. Run each with `await asyncio.to_thread(...)` (§3.6). NumPy releases the GIL, so the loop stays responsive. |
| `src/services/taste_engine.py` > `_domain_corpus_mean()` / `_corpus_calibration()` / `compute_taste_vector_for_user()` (L293, L508, L585) | Event-Loop Blocking | **High** | `embeddings_for_domain()` pulls up to **30,000** vectors from Chroma, converts them to NumPy, and normalizes them on the loop, twice per category per rebuild. `compute_taste_vector_for_user()` materializes the user's full `WatchHistoryEntry` ORM rows (`.all()`), and the owner's music history alone is about 350k plays. The `asyncio.sleep(0)` yields that follow (L654) cannot help with a load that has already blocked as one monolith. | Run the Chroma pull and the NumPy math as a single `to_thread` call. Load only the columns needed (`db.query(W.id, W.title, …)`) or `yield_per(5000)` inside a thread, never full ORM entities on the loop. |
| `src/services/scheduler.py` > `job_game_watcher()` (L127–174), every 30 s | Event-Loop Blocking / Locking | Med | `is_game_running()` runs `subprocess.run(nvidia-smi, timeout=4)`, a **synchronous** `httpx.get(/api/ps, timeout=2)` and `psutil.process_iter()` on the loop (the 30 s cache expires on the same 30 s cadence). It then issues 3–6 synchronous `get_state` / `set_state` calls, including an **unconditional write** of `game_active` every tick (2,880 write transactions per day). This is the most frequent loop-thread writer, and it is what trips the `music_matcher` deadlock. | `running = await asyncio.to_thread(is_game_running)`; `st = await asyncio.to_thread(lane_status)`. Persist all changed keys in one `run_write` transaction, and only when a value changed (§3.4). |
| `src/services/app_state.py` > `set_state()` (L24–45) | Locking | Med | It does read-then-write as two statements: a SELECT in autocommit, then an implicit BEGIN plus UPDATE or INSERT. Two first writers of a new key can both take the INSERT branch, and one fails with an IntegrityError. Every call costs two round trips across 248 call sites. On `database is locked` it falls back to `force_set_state(timeout_s=120)`, which can stack a **second** busy-wait of up to 2 minutes on the loop thread. | Use a single `INSERT … ON CONFLICT(key) DO UPDATE` statement (the same SQL `acquire_state_lock` already uses), run through `run_write`. Drop the fallback, or keep it only off-loop with a short timeout. |
| `src/services/app_state.py` > `force_set_state()` (L129) | Lifecycle / Locking | Med | `DATABASE_URL.replace("sqlite:///", "").lstrip("/")` turns the POSIX absolute path `sqlite:////home/…/data/curatarr.db` into the **relative** path `home/…/data/curatarr.db`. On Linux or Docker the crash-cleanup writer therefore opens, or creates, the wrong file, hits `no such table`, and silently returns `False`. Stale `*_running` flags then block jobs until restart. It works on Windows only by accident (`C:/…`). | Use `engine.url.database` (or `make_url(settings.DATABASE_URL).database`) here and in `job_db_vacuum` / `job_db_backup`. |
| `src/database/connection.py` engine (L22–32) + `get_db` + streaming routes (`chat.send_message`, bulk-delete runner L1777) | Lifecycle / Locking | Med | QueuePool is 5 + 10 with `pool_timeout=60`, and checkout happens **on the loop thread**. The request-scoped `get_db` session is held for the whole SSE stream: FastAPI's yield-dependency teardown runs after the streamed response, so the connection is held for minutes of LLM generation. The bulk-delete runner holds a connection for minutes too (the Lidarr guard alone can take about 120 s per item). Once the pool is exhausted, the next checkout blocks the loop inside `QueuePool.get()`, while the coroutines holding connections cannot run to return them. The result is a 60 s freeze, then `TimeoutError`. | Use `Depends(get_db, scope="function")` (FastAPI ≥ 0.121) on streaming routes, or open short `get_db_session()` blocks inside the generator. Use `pool_timeout` of about 10 s, and only take checkouts in worker threads. |
| `src/services/plex_sync.py` > `_write_entries()` (L≈600–804, in `to_thread`) | Locking (batching) | Med | The whole sync is written as **one** transaction: with `autoflush=False`, every `db.add()` piles up and the final `commit()` flushes them all at once. An initial or backlog sync of 10⁴–10⁵ rows holds the write lock for the entire flush, so loop-thread writers stall for up to 60 s and can fail. It also runs an uncached `_series_mi` query per episode (N+1). | `db.commit()` every 1,000–2,000 rows, which is safe because dedup is driven by `existing`. Cache `_series_mi` lookups in `local_mi_cache`. |
| `src/services/plex_sync.py` re-attribution (L1315–1398) | Transaction Bleed / Event-Loop Blocking | Med | The session (and its pooled connection) stays open across `await _fetch_account_to_user_map()`, a Plex HTTP call. It then loads **every** non-Spotify `WatchHistoryEntry` as ORM objects and mutates them all in one transaction on the loop. | Fetch the account map first. Then, in `to_thread`, select `(id, plex_item_id, viewed_at, user_id, plex_user_id)` and apply PK-bulk `update()` in chunks. |
| `src/services/scheduler.py` > `job_db_vacuum()` (L1088–1178) | Locking | Med | `VACUUM` on the **live** main DB holds the write lock for the full file rewrite (minutes on a large DB) and needs about twice the file size in free disk. The thread offload keeps the loop free of the VACUUM itself, but every app writer, most of them on the loop, stalls for up to 60 s and then fails. | Run it only when both custodian queues are idle and no enrichment or music flag is set. Run it only if `freelist_count / page_count > 0.2`. Prefer `auto_vacuum=INCREMENTAL` with periodic `PRAGMA incremental_vacuum(N)` in small steps. |
| `src/main.py` lifespan boot reset (L76–82) + `scripts/music_enricher.py` | Lifecycle (queue isolation) | Med | Boot unconditionally sets `music_pipeline_running = "0"`, `enrichment_running = "0"` and the other flags. `music_enricher.py` is designed to run **out of process across server restarts** and holds `music_pipeline_running = "1"`. A restart clears its claim, so the in-app pipeline can start a duplicate concurrent pass over the same library, causing write contention and double API spend. The DB "mutex" has no owner and no lease. | Make the locks leases: store `{owner_pid, host, heartbeat_at}` in the value. On boot, clear only rows whose owner PID is dead or whose heartbeat is stale (for example older than 5 min). The holder refreshes the heartbeat once per chunk. |
| `src/database/connection.py` > `_migrate_columns()` (L77–253) | Migration | Med | pysqlite does not wrap DDL, so each `ALTER TABLE` autocommits on its own and the run is not atomic. The one-shot `attempt_count` backfill is keyed on the in-memory `added` set, so a crash between the ALTER and the final `conn.commit()` loses the backfill **permanently**: the column now exists, and the next boot never re-adds it to `added`. Failures, including `database is locked` while `music_enricher.py` writes, are logged at **DEBUG**. A skipped column then surfaces later as a runtime `no such column`. | Run all of it in one `BEGIN IMMEDIATE … COMMIT` (SQLite DDL is transactional). Track applied steps in `PRAGMA user_version` or a `schema_migrations` table so backfills are tied to a version, not to whether the column was just added. Log failures at ERROR and abort startup. |
| `update_db.py` | Migration | Med | `init_db()` runs destructive DDL (the `deletion_proposals` rebuild and `DROP TABLE user_pin_hashes` under `secure_delete`) with no pre-migration backup. Nothing stops it from running while the server or `music_enricher.py` is live, which creates exactly the lock contention that makes the DEBUG-logged column adds silently fail. | Refuse to run if the app port answers. Take an online `sqlite3.backup()` snapshot, verified with `quick_check`, before `init_db()` (§3.7). |
| `src/services/episodic_memory.py` > `resolve_memory_conflicts()` (L205–336) | Event-Loop Blocking / Transaction Bleed | Med | For a title-less memory it runs `json.loads` plus pure-Python cosine over **every** older memory's embedding on the loop. The session and its connection stay open across up to N × 20 s Ollama calls. No write lock is held, because each write commits immediately. | Compute similarities with NumPy in `to_thread`. Close the session before the LLM loop and re-open a short write session per verdict. |
| `src/database/connection.py` PRAGMAs (L36–51) | Locking | Low | `foreign_keys=ON` is never set, so the 19 declared `ForeignKey`s are not enforced (orphans are possible). `busy_timeout` is re-issued on every checkout (redundant), and `pool_pre_ping` adds a `SELECT 1` per checkout against a local file. | Add `PRAGMA foreign_keys=ON` after a one-off `PRAGMA foreign_key_check` cleanup. Drop the checkout listener and `pool_pre_ping` for SQLite. Add `temp_store=MEMORY`. |
| `src/database/connection.py` > `get_db_session()` / `get_db()` (L54–74) | Lifecycle | Low | `except Exception` does not catch `CancelledError`, `KeyboardInterrupt` or `SystemExit` (all BaseException), so the explicit rollback is skipped and cleanup depends on the pool's reset-on-return. `get_db` never rolls back explicitly. | Use `except BaseException: db.rollback(); raise`, and add an explicit rollback in `get_db`'s `finally` when `db.in_transaction()`. |
| `src/cache/metadata_cache.py` > `MetadataCache.__init__` (64 call sites) | Locking / Lifecycle | Low | Each construction opens a new connection and runs 4 PRAGMAs plus `CREATE TABLE/INDEX IF NOT EXISTS`. Many call sites `close()` only on the happy path and have no context manager, so file handles leak on exceptions. | Add `__enter__` / `__exit__` and a process-wide `get_metadata_cache()` (thread-local connection; the schema is initialized once). |
| `src/services/scheduler.py` > `_attach_rec_ids()` (L1276) and `music_matcher` Plex index build (L150–175) | Event-Loop Blocking | Low | Full `MediaTechProfile` and `MediaIdentity` scans per category and lane, and `resp.json()` over whole Plex music sections (tens of MB), run on the loop. | `to_thread` for the scan. For the Plex index, `await asyncio.to_thread(resp.json)` or page with `X-Plex-Container-Size`. |

**Positive findings (keep these):** `autoflush=False` combined with commit-before-network
discipline means the AST sweep found exactly **one** flush-across-await site. Network and
LLM work runs before write sessions in `_cache_recommendations` and `job_arr_sync`.
`acquire_state_lock` is a correct single-statement CAS. Backups use the online backup API
with a `quick_check`, and VACUUM and backup run off the loop. Background-task references are
retained, and their exceptions are logged (`bg_tasks.track_task`, `scheduler._track_task`).
The custodian claims a queue as busy before the first `await`, so ticks cannot double-start a
queue.

---

## 3. Concrete Code Refactorings & Proposals

All snippets below were exercised against SQLAlchemy 2.0.54 / Python stdlib `sqlite3` in a
scratch environment (WAL + `BEGIN IMMEDIATE` sessions, PK-bulk `update()`, rollback on
error, single-writer executor with a responsive loop, checkpoint + dispose, model-derived
table rebuild).

### 3.1 Hardened engine & session primitives (`src/database/connection.py`)

```python
import logging
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from src.config import settings
from src.database.models import Base

_IS_SQLITE = settings.DATABASE_URL.startswith("sqlite")
# Waits now happen on worker threads, never on the event loop — 15 s is
# plenty to ride out another process's commit without hiding a real stall.
BUSY_TIMEOUT_MS = 15_000

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": BUSY_TIMEOUT_MS / 1000}
    if _IS_SQLITE else {},
    pool_size=10,
    max_overflow=10,
    pool_timeout=10,          # fail fast instead of a 60 s hang
)

if _IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cur.execute("PRAGMA foreign_keys=ON")      # after a one-off foreign_key_check
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA cache_size=-64000")
        cur.execute("PRAGMA wal_autocheckpoint=1000")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


@contextmanager
def get_db_session():
    """Read-mostly session. BaseException so a cancelled task still rolls back."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def write_session():
    """Write session that takes the SQLite write lock UP FRONT (BEGIN IMMEDIATE).

    A deferred BEGIN acquires the lock at the first DML statement — possibly
    after reads whose results the writes depend on. IMMEDIATE makes the
    transaction's lock window explicit and short, and fails fast (after
    busy_timeout) instead of mid-way. Never ``await`` inside this block.
    """
    db = SessionLocal()
    try:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def get_db():
    """FastAPI dependency. Prefer Depends(get_db, scope="function") on
    StreamingResponse routes so the connection is not held for the stream."""
    db = SessionLocal()
    try:
        yield db
    finally:
        if db.in_transaction():
            db.rollback()
        db.close()


def sqlite_path() -> str:
    """Filesystem path of the main DB — correct on POSIX and Windows."""
    return engine.url.database
```

### 3.2 Async offload: concurrent readers, one in-process writer (`src/database/runner.py`)

```python
"""Run blocking DB work off the event loop.

Reads go to the default threadpool (WAL lets readers run concurrently).
Writes go to ONE dedicated thread, so in-process writers never contend for
the SQLite write lock — busy_timeout only arbitrates against other
processes (music_enricher.py, update_db.py). Cancelling the awaiting
coroutine does not abort a write already running: the transaction completes
or rolls back atomically in its thread.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, TypeVar

T = TypeVar("T")
_WRITER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-writer")


async def run_read(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    return await asyncio.to_thread(fn, *args, **kwargs)


async def run_write(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_WRITER, partial(fn, *args, **kwargs))


def shutdown_writer() -> None:
    _WRITER.shutdown(wait=True)   # drain queued writes before engine.dispose()
```

Route conversion pattern: an endpoint that only touches the DB becomes a plain `def`, which
FastAPI runs in its threadpool:

```python
@router.get("/history")
def get_chat_history(thread_id: str = "general",
                     user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    ...  # unchanged body — now off the event loop
```

Mixed endpoints keep `async def` and wrap their DB sections in a sync helper:

```python
def _load_proposals(user_id: int, ids: list[int]) -> list[dict]:
    with get_db_session() as db:
        return [{"id": p.id, "title": p.title, "service": p.service}
                for p in db.query(DeletionProposal).filter(...).all()]

props = await run_read(_load_proposals, user.id, ids)
```

### 3.3 `music_matcher.match_spotify_to_plex()` — no lock across `await`, real chunked progress

```python
from sqlalchemy import update
from src.database.connection import get_db_session, write_session
from src.database.runner import run_read, run_write


def _load_unmatched(user_id: int, since_id: int) -> list[tuple]:
    W = WatchHistoryEntry
    with get_db_session() as db:
        return db.query(W.id, W.series_title, W.title, W.genres).filter(
            W.user_id == user_id, W.source == "spotify", W.media_type == "music",
            W.plex_item_id.like("spotify%"), W.id > since_id,
        ).order_by(W.id.asc()).all()


def _apply_matches(rows: list[dict]) -> None:
    if rows:
        with write_session() as db:                  # lock held for milliseconds
            db.execute(update(WatchHistoryEntry), rows)   # ORM bulk UPDATE by PK


# ── 3. Match Spotify plays ──
plays = await run_read(_load_unmatched, user_id, since_id)
total_plays, completed = len(plays), False
for start in range(0, total_plays, _MATCH_FLUSH):
    if await run_read(_stop_requested):              # once per chunk, not per row
        logger.info("[music_matcher] Stop requested at %d/%d", start, total_plays)
        break
    rows = []
    for pid, artist, title, genres in plays[start:start + _MATCH_FLUSH]:
        hit = plex_index.get((_normalize(artist or ""), _normalize(title or "")))
        if not hit:
            unmatched += 1
            continue
        matched += 1
        rows.append({"id": pid, "plex_item_id": hit["rating_key"],
                     "genres": genres or ",".join(hit["genres"]) or genres})
    await run_write(_apply_matches, rows)            # committed: survives a crash
    logger.info("[music_matcher] … %d/%d plays processed (matched=%d)",
                min(start + _MATCH_FLUSH, total_plays), total_plays, matched)
else:
    completed = True

if completed and plays:
    await run_write(set_state, cursor_key, str(plays[-1][0]))
```

Re-running after an interrupted pass is idempotent: matched rows no longer satisfy
`plex_item_id LIKE 'spotify%'`.

### 3.4 `app_state` — single-statement upsert, correct path; game watcher off the loop

```python
# src/services/app_state.py
from sqlalchemy import bindparam, text
from src.database.connection import get_db_session, sqlite_path

_UPSERT = text(
    "INSERT INTO app_state (key, value, updated_at) VALUES (:k, :v, :ts) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
    "updated_at = excluded.updated_at"
)


def set_state(key: str, value: str) -> None:
    """One atomic statement: no read-then-write race, one round trip."""
    with get_db_session() as db:
        db.execute(_UPSERT, {"k": key, "v": value, "ts": datetime.utcnow().isoformat()})


def set_states_if_changed(values: dict[str, str]) -> list[str]:
    """Write only keys whose value changed, in ONE transaction."""
    with get_db_session() as db:
        current = dict(db.execute(
            text("SELECT key, value FROM app_state WHERE key IN :keys")
            .bindparams(bindparam("keys", expanding=True)),
            {"keys": list(values)}).all())
        changed = [k for k, v in values.items() if current.get(k) != v]
        ts = datetime.utcnow().isoformat()
        for k in changed:
            db.execute(_UPSERT, {"k": k, "v": values[k], "ts": ts})
        return changed


def force_set_state(key: str, value: str, timeout_s: int = 15) -> bool:
    import sqlite3
    try:
        conn = sqlite3.connect(sqlite_path(), timeout=timeout_s, isolation_level=None)
        ...  # body unchanged
```

```python
# src/services/scheduler.py
async def job_game_watcher():
    try:
        from src.services.process_monitor import is_game_running, unload_llm_models
        from src.services.app_state import set_states_if_changed
        from src.services.llm_lane import status as lane_status
        from src.database.runner import run_write

        running = await asyncio.to_thread(is_game_running)   # nvidia-smi, psutil, sync httpx
        st = await asyncio.to_thread(lane_status)
        mode = (("game" if st["game"] else "cpu" if st["summarizer"] == "cpu" else "paused")
                if st["gpu_pressed"] else ("game" if running else "free"))
        changed = await run_write(set_states_if_changed, {
            "game_active": "1" if running else "0",
            "llm_lane": mode,
            "llm_lane_reason": st["reason"] or "",
        })
        if "llm_lane" in changed:
            logger.info("[lane] -> %s%s", mode, f" ({st['reason']})" if st["reason"] else "")
        if running:
            unloaded = await unload_llm_models()
            if unloaded:
                logger.info("[game watcher] game active — evicted from VRAM: %s",
                            ", ".join(unloaded))
    except Exception as e:
        logger.debug("[game watcher] tick failed: %s", e)
```

### 3.5 Graceful shutdown: cancel, drain, checkpoint, then release Syncthing

```python
# src/services/bg_tasks.py
async def drain(timeout: float = 20.0) -> None:
    """Cancel every tracked task and wait (bounded) for its finally-blocks —
    lock releases, rollbacks — to run while the loop is still alive."""
    from src.services.scheduler import _background_tasks
    tasks = [t for t in (*_BG_TASKS, *_background_tasks)
             if not t.done() and t is not asyncio.current_task()]
    for t in tasks:
        t.cancel()
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=timeout)
    for t in pending:
        logger.warning("[shutdown] task %s did not stop within %.0fs", t.get_name(), timeout)


# src/database/connection.py
def checkpoint_and_dispose() -> None:
    """Fold the WAL into the main file and close every pooled connection, so
    data/ holds one self-consistent .db (empty -wal) before Syncthing sees it."""
    engine.dispose()
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    engine.dispose()
```

```python
# src/main.py — lifespan teardown, in this order
    try:
        from src.services.episodic_memory import flush_all_pending_extractions
        await asyncio.wait_for(flush_all_pending_extractions(), timeout=30)
    except Exception as e:
        logger.debug("Shutdown memory flush failed: %s", e)

    from src.services.scheduler import stop_scheduler
    stop_scheduler()

    from src.services.bg_tasks import drain
    await drain(timeout=20)                 # running work rolls back / releases locks NOW

    try:
        from src.services.app_state import set_state
        for k in ("enrichment_running", "music_pipeline_running",
                  "music_pipeline_stop_requested", "deletion_run", "plex_sync_running"):
            set_state(k, "0")
    except Exception:
        pass

    try:
        from src.services.llm_priority import shutdown_evict
        await shutdown_evict()
    except Exception as e:
        logger.debug("[shutdown] evict pass failed: %s", e)

    from src.database.runner import shutdown_writer
    from src.database.connection import checkpoint_and_dispose
    shutdown_writer()
    checkpoint_and_dispose()                # only now is the DB safe to sync

    from src.services.sync_guard import disable as _sync_guard_disable
    _sync_guard_disable()
```

Every long-running coroutine (`_run_enrichment`, `_run_queue`, the music pipeline) must let
`CancelledError` propagate after its cleanup (`except asyncio.CancelledError: cleanup();
raise`) rather than returning. Otherwise `drain()` sees the task "finish" while the queue
keeps going, which is the 2026-10-07 reload hang `_cancel_requested()` works around.

### 3.6 Deletion scoring off the loop (`recommendations_engine.generate_deletion_proposals`)

```python
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

# ChromaDB's PersistentClient is guarded by a process lock; confine its calls
# to one thread so concurrent scans never interleave inside it.
_CHROMA = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chroma")


@dataclass
class DeletionCtx:
    taste_blurb: str
    ts_genres: dict
    blob: dict                    # decoded EncryptedTasteVector blob
    protected: set
    rejected_recent: set
    curator_positioned: set
    recent_watched: set
    user_rating_by_artist: dict
    plays_by_name: dict
    plays_by_mbid: dict
    tech_idx: object


def _load_deletion_context(user_id: int, category: str, titles: set) -> DeletionCtx:
    """Every DB read of phase A, in ONE short read session, on a worker thread."""
    with get_db_session() as db:
        ...  # the existing queries from L1170-1420 and L1656-1680, unchanged


def _bulk_cached_ratings(items: list, category: str) -> list[tuple]:
    """ONE metadata-cache connection for the whole batch (was one per item)."""
    cache = MetadataCache()
    try:
        return [_cached_rating_from(cache, it, category) for it in items]
    finally:
        cache.close()


def _score_candidates(ctx: DeletionCtx, items, embeddings, ratings) -> list[dict]:
    """Pure CPU: filters + NumPy cosine + calibration. No DB, no awaits."""
    ...  # the loop from L1484-1720, reading only ctx / embeddings / ratings


async def generate_deletion_proposals(user_id, arr_items, category="movie", monitor_task=None):
    titles = {i.get("title") for i in arr_items if i.get("title")}
    ctx = await asyncio.to_thread(_load_deletion_context, user_id, category, titles)
    loop = asyncio.get_running_loop()
    ids = [str(i.get("plex_rating_key") or i.get("tmdb_id") or i.get("title")) for i in arr_items]
    embeddings = await loop.run_in_executor(_CHROMA, chroma_db.get_by_ids, ids)
    ratings = await asyncio.to_thread(_bulk_cached_ratings, arr_items, category)
    scored = await asyncio.to_thread(_score_candidates, ctx, arr_items, embeddings, ratings)
    ...  # phase B (LLM judge / pitches) unchanged — already async
```

For loops that must stay on the loop (pure-Python work between awaits), use a cooperative
iterator. It is only valid when **no write transaction is open** across the yield:

```python
async def cooperative(items, every: int = 200):
    for i, item in enumerate(items):
        if i and i % every == 0:
            await asyncio.sleep(0)      # never while a flush/DML is uncommitted
        yield item
```

### 3.7 Migration safety: correct AUTOINCREMENT, model-derived rebuild, pre-migration backup

```python
# src/database/models.py — DeletionProposal
    __table_args__ = (
        Index("idx_dp_user_status", "user_id", "status"),
        Index("idx_dp_latest_activity", "latest_activity_at"),
        {"sqlite_autoincrement": True},   # create_all now emits AUTOINCREMENT
    )
```

```python
# src/database/connection.py — replaces the hard-coded CREATE/INSERT in Pass 90c
from sqlalchemy.schema import CreateIndex, CreateTable

def _rebuild_with_model_ddl(conn, table) -> None:
    """Atomic SQLite table rebuild from the CURRENT model. Copies only the
    columns both versions share, so no column added after the rebuild was
    written can ever be dropped. Caller holds BEGIN IMMEDIATE."""
    name, old = table.name, f"{table.name}_old_rebuild"
    for (ix,) in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name=? AND sql IS NOT NULL", (name,)).all():
        conn.exec_driver_sql(f'DROP INDEX IF EXISTS "{ix}"')
    conn.exec_driver_sql(f'ALTER TABLE "{name}" RENAME TO "{old}"')
    conn.exec_driver_sql(str(CreateTable(table).compile(dialect=engine.dialect)))
    old_cols = {r[1] for r in conn.exec_driver_sql(f'PRAGMA table_info("{old}")')}
    shared = ", ".join(f'"{c.name}"' for c in table.columns if c.name in old_cols)
    # Explicit-id inserts advance sqlite_sequence themselves — no manual seed.
    conn.exec_driver_sql(f'INSERT INTO "{name}" ({shared}) SELECT {shared} FROM "{old}"')
    for ix in table.indexes:
        conn.exec_driver_sql(str(CreateIndex(ix, if_not_exists=True).compile(dialect=engine.dialect)))
    conn.exec_driver_sql(f'DROP TABLE "{old}"')


def _migrate_deletion_proposals_autoincrement() -> None:
    from src.database.models import DeletionProposal
    with engine.connect() as conn:
        ddl = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='deletion_proposals'"
        ).scalar()
        if not ddl or "AUTOINCREMENT" in ddl.upper():
            return
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            _rebuild_with_model_ddl(conn, DeletionProposal.__table__)
            conn.exec_driver_sql("COMMIT")
        except BaseException:
            conn.exec_driver_sql("ROLLBACK")
            raise                           # a failed schema step must stop the boot
```

```python
# update_db.py
import socket
import sqlite3
from datetime import datetime
from pathlib import Path

from src.config import settings
from src.database.connection import engine, init_db


def _server_running() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", settings.PORT)) == 0


def _backup() -> Path | None:
    src_path = Path(engine.url.database)
    if not src_path.exists():
        return None
    dest = src_path.with_name(f"{src_path.stem}.pre-migrate-{datetime.utcnow():%Y%m%d_%H%M%S}.db")
    src, dst = sqlite3.connect(src_path, timeout=15), sqlite3.connect(dest)
    try:
        src.backup(dst)
        if dst.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
            raise RuntimeError(f"backup {dest} failed quick_check")
    finally:
        dst.close()
        src.close()
    return dest


def update_database():
    if _server_running():
        raise SystemExit(f"Curatarr is running on port {settings.PORT} — stop it "
                         "(and scripts/music_enricher.py) before migrating.")
    dest = _backup()
    logger.info("Backup written: %s", dest or "(fresh DB, nothing to back up)")
    init_db()
    logger.info("Done. Restore with: copy %s over %s if anything looks wrong.",
                dest, engine.url.database)
```

For `_migrate_columns()`: wrap the loop in `BEGIN IMMEDIATE … COMMIT`, version the one-shot
backfills with `PRAGMA user_version` (for example `if user_version < 7: backfill;
PRAGMA user_version = 7` in the same transaction), and replace the `debug` logging with
`error` plus a re-raise. A boot that silently skipped a column is worse than one that refuses
to start.

---

## Implementation status

Fixed on this branch, each with a regression test that fails on the old code:

| Finding | Fix | Test |
| :--- | :--- | :--- |
| Fresh-install column loss (§3.7) | `sqlite_autoincrement` on the model; `_rebuild_from_model()` builds the new table from the model and copies only shared columns; no manual `sqlite_sequence` seed | `tests/test_dp_rebuild_columns.py` |
| `music_matcher` lock across `await` (§3.3) | Load once, match in memory, commit each chunk in its own `write_session()` on the writer thread (`src/database/runner.py`); stop flag read per chunk | `tests/test_music_matcher_write_lock.py` |
| `force_set_state` POSIX path | `engine.url.database` | `tests/test_force_set_state_path.py` |
| Shutdown order (§3.5) | `bg_tasks.drain()` → flags → `shutdown_writer()` → `checkpoint_and_dispose()` (`wal_checkpoint(TRUNCATE)`) → Syncthing | `tests/test_shutdown_release.py` |

Shipped later (2026-10-15 to 2026-10-19), see `docs/POST_REFACTOR_INTEGRATION_AUDIT.md` and
`docs/SILENT_FAILURE_SWEEP_2026_10.md` §3:

| Finding | Fix | Test |
| :--- | :--- | :--- |
| `set_state` race, game watcher on the loop (§3.4) | Single-statement upsert; probes in `to_thread`, flags through `set_states_if_changed` (integration audit) | `tests/test_integration_audit_2026_10.py` |
| Deletion scoring and taste rebuild on the loop (§3.6) | Phase A in one worker hop, Chroma on its own thread (`run_chroma`) (integration audit; sweep rounds 7–8) | `tests/test_integration_audit_2026_10.py`, `tests/test_loop_free_vector.py` |
| Migration safety (§3.7) | `_migrate_columns` in one `BEGIN IMMEDIATE` with `user_version`; `update_db.py` backup (integration audit) | `tests/test_integration_audit_2026_10.py` |
| Route handlers and jobs on the loop (§3.2) | DB work in sync helpers run by `run_read` / `run_write` (`src/database/runner.py`); `src/database/loop_guard.py` reports any statement on the loop thread (sweep rounds 3, 5, 6, 8) | `tests/test_loop_db_ratchet.py`, `tests/test_loop_free_g1.py` … |
| Engine settings (§3.1) | `busy_timeout` 15 s (`connection.BUSY_TIMEOUT_S`; the enrichment cache too), `pool_size` 10, `pool_timeout` 10 s, no pre-ping, `temp_store=MEMORY` (sweep rounds 5–6) | `tests/test_loop_responsiveness.py` |
| Foreign keys not enforced (§3.1) | `_repair_foreign_keys` at boot, then `foreign_keys=ON` on both engines (sweep round 6) | `tests/test_foreign_keys.py` |
| `MetadataCache` connection per construction, `resolve_memory_conflicts`, `get_db_session` rollback | One connection per (thread, file); conflict scan in `run_read`, no session across LLM calls; `except BaseException` (integration audit) | `tests/test_integration_audit_2026_10.py` |
| `_write_entries` one transaction, re-attribution session across `await`, VACUUM on a busy DB, boot reset of `music_pipeline_running` | Commits per 2,000 rows; no session across the `/accounts` call; VACUUM postponed while long jobs run and skipped below 20 % free pages; the music flag is a cross-process lease (`src/services/state_lease.py`) (sweep follow-up #6) | `tests/test_silent_failures_followup.py`, `tests/test_custodian_job_contract.py` |

`write_session()` differs from the §3.1 sketch. It runs on a dedicated small engine with
pysqlite in driver-level autocommit (`isolation_level=None`), and a SQLAlchemy `begin`
listener emits `BEGIN IMMEDIATE`, so the driver never opens an implicit transaction that a
`BEGIN` could collide with. The hook is deliberately not on the main engine. There, every
read session, including a chat stream's held for minutes, would take the write lock.

## Suggested fix order

1. §3.7: the fresh-install `deletion_proposals` column loss. A one-line model fix plus the
   rebuild change; it hits every new user.
2. §3.3: the `music_matcher` lock-across-await self-deadlock.
3. §3.5: the shutdown ordering (Syncthing corruption window).
4. §3.4: `force_set_state` path, `set_state` upsert, and moving the game watcher off the loop.
5. §3.1/§3.2: the `run_read` / `run_write` foundation, then move the High
   event-loop-blocking paths onto it (§3.6, taste engine), endpoint by endpoint.

*Status 2026-10-19: all five steps shipped (see the second table under Implementation status).*
