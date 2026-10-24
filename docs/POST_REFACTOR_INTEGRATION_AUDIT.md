# Post-Refactor Integration Audit & Residual Bottleneck Cleanup — 2026-10-15

> **Dated report.** It describes the code at the commit named below: line numbers,
> counts and "open" items are from then, and status notes added later carry their
> date. For what is true today, see [the docs index](README.md#audit-reports--dated-snapshots).

Scope: whether the UI/UX, documentation, LLM pipeline, AppSec (2026-10-10) and SQLite
concurrency (2026-10-11) workstreams fit together, covering frontend↔backend JSON contracts,
model/migration parity and the test battery. It also covers the residual leaks and event-loop
blockers that `docs/SQLITE_ASYNC_CONCURRENCY_AUDIT.md` listed but the first four fixes did not
address.

Method: reading the code; programmatic parity checks (every `_migrate_columns` column against
`Base.metadata`, plus the free variables of the chat stream generator); a first-writer race run
30 times against the old and the new `set_state`; and a new regression suite,
`tests/test_integration_audit_2026_10.py` (41 checks). Battery: 148/148 green before, 149/149
after.

---

## 1. Integration & System Cohesion Summary

The workstreams fit together. `DeletionProposal`, `_migrate_columns` and the model-derived
Pass-90c rebuild agree on every column, including `score_factors`, `soulsync_ban`,
`returned_at` and `returned_info`. No PIN column or endpoint is left, and `user_pin_hashes` is
dropped at boot. The deletion-run summary (`judged`/`flagged`/`deferred`/`failed`/
`failed_by_kind`/`failed_reason`/`stopped`/`at`) matches `deletions.js:_renderRunBanner`
key for key. One contract was broken: the household-floor guard (`_guard`) was dropped before
the proposal was persisted, so a "Your call" produced by the household rule showed the generic
"merely fine" tooltip, which states the opposite of the real reason. Migrations had one real
integrity gap: each `ALTER` committed on its own, and failures were logged at DEBUG. The
residual performance debt was concentrated in five places: the per-call metadata-cache
connection, phase A of deletion scoring, the taste rebuild, the game watcher, and a chat stream
that held a pooled connection for its whole duration. All of these are fixed here, each with a
regression check.

---

## 2. Cross-Cutting Audit & Bottleneck Table

| Area / Files | Issue Type | Severity | Current State & Conflict | Resolution (this change) |
| :--- | :--- | :--- | :--- | :--- |
| `src/cache/metadata_cache.py` (66 construction sites) + `recommendations_engine._get_cached_rating()` | Handle Leak / Loop Stall | High | Every `MetadataCache()` opened a new `sqlite3` connection and ran 4 PRAGMAs plus `CREATE TABLE/INDEX` and a commit. The deletion scorer did this once per candidate, on the loop: hundreds per category. A handle dropped on an exception kept its open transaction, and its write lock, until GC. | One connection per (thread, file), opened once, schema checked once. A handle is a cheap ref-counted view: `with MetadataCache() as c:`, `close()` and GC all release it. When the last handle on a thread is released, anything left uncommitted is rolled back. `conn` resolves to the *calling* thread's connection, so a handle used inside `to_thread` never shares a connection across threads. The connection is reopened if the file was replaced. New `get_cache_many()` does one indexed `IN` per 500 keys with v2-over-v1 precedence. `_bulk_cached_ratings()` makes one read for the whole batch. |
| `recommendations_engine.generate_deletion_proposals()` phase A | Loop Stall | High | The taste blob, protections, keep-cooldown, 60 days of assistant text, the 90-day watch veto (tens of thousands of music rows), music ratings, the Chroma `get_by_ids` over the whole library, the tech index and the ≈350k-row plays `GROUP BY` all ran synchronously on the loop. | `_load_phase_a()` covers every DB read in one `asyncio.to_thread` call. Embeddings load on a dedicated Chroma thread (`run_chroma`), concurrently with the bulk rating read. `load_tech_index` and the plays aggregate run in `to_thread`. The candidate loop, now pure CPU, yields every 250 items with no transaction open. |
| `taste_engine.compute_taste_vector_for_user()` / `_domain_corpus_mean()` / `_corpus_calibration()` / `compute_all_taste_vectors()` | Loop Stall | High | Full `WatchHistoryEntry` ORM rows (≈350k) were loaded on the loop, with one cache lookup per play in the stats pass. Up to 30k Chroma vectors plus NumPy k-means and percentile math ran on the loop, twice per category. | `_load_watch_entries()` selects 9 columns into dicts on a worker thread. `_bulk_profile_tags()` makes one batched read and keeps 3 short lists per title. The k-means, corpus mean and calibration run on the Chroma thread. `_update_plain_taste_stats` and `_store_taste_blobs` run on the DB writer thread. *Residual:* the embedding phase still does up to 3 point lookups per embedded entry (≤5,000) on the loop. Each is a statement on the shared connection now, not a connect. *Done since (silent-failure sweep rounds 5 and 7–8): the cache lookups run in `asyncio.to_thread` and the stored-vector `get_by_id` on the Chroma thread (`run_chroma`).* |
| `episodic_memory.resolve_memory_conflicts()` | Loop Stall / Pool | Med | The session and its pooled connection stayed open across up to N × 20 s Ollama calls. A pure-Python cosine ran over every older memory on the loop. | `_conflict_targets()` runs a column-only scan and one NumPy matrix product in `run_read`, and the session is closed before any LLM call (tested: 0 connections checked out during the call). `_apply_conflict_verdict()` writes each verdict in its own `write_session()` through `run_write`. |
| `scheduler.job_game_watcher()` | Loop Stall / Write churn | Med | `nvidia-smi` (≤4 s), a synchronous `httpx.get(/api/ps)` (2 s) and `psutil` ran on the loop every 30 s, and the 30 s caches expire on the same cadence. `game_active` was written every tick: 2,880 transactions a day. | `is_game_running` and `lane_status` run in `asyncio.to_thread`. All three keys go through `set_states_if_changed` in one transaction on the writer thread, which writes only changed keys and returns their previous values for the transition log. |
| `app_state.set_state()` | Race / Locking | Med | It read and then UPDATEd or INSERTed. With 8 concurrent first writers of a new key, the old code raised in 26 of 30 runs. On `database is locked` it stacked a 120 s `force_set_state` busy-wait on top of the 60 s already spent, on the loop. | A single `INSERT … ON CONFLICT(key) DO UPDATE` (0 of 30 runs fail). The fallback waits 5 s on the loop and 30 s off it, and logs a warning instead of failing silently. `aset_state()` routes through `run_write`. `force_set_state()`'s 120 s default stays for crash-cleanup scripts. |
| `routers/chat.py` `_send_message_guarded()` | Pool Starvation | Med | After `_save_message` committed (which expires `user`), `weakref.finalize(stream, …, user.id, …)` lazily refreshed the row. That opened a transaction on the request session and held the pooled connection until the response finished: minutes of generation per open chat. | `_uid = user.id` is captured before the commit, the generator closes over plain values only (checked from its free variables), and `db.close()` runs before `StreamingResponse` is returned. |
| `task_monitor._broadcast()` | Thread-safety | Med | `asyncio.Queue.put_nowait` was called from worker threads. Offloaded code reports progress through it, and `asyncio.Queue` is not thread-safe, so a put can miss waking the SSE coroutine. | The loop is recorded at `subscribe()`. A broadcast made off that loop is handed over with `call_soon_threadsafe`. |
| `connection._migrate_columns()` | Migration Idempotency | Med | Each `ALTER` autocommitted on its own. A crash after the ALTER kept the column and lost the one-shot `attempt_count` backfill permanently. Failures, such as a lock held by the music enricher, were logged at DEBUG and surfaced later as `no such column`. | Everything runs in one `BEGIN IMMEDIATE … COMMIT` on a driver-autocommit connection, so columns and backfills land together or not at all. A failure is logged at ERROR, rolled back and re-raised, which stops the boot. `PRAGMA user_version = SCHEMA_VERSION` (1) gives future backfills a version to key on. |
| `update_db.py` | Migration Safety | Low | Destructive DDL (table rebuild, retired-table drop) ran with no backup, even while the server was live. | Refuses to run if the app port answers. Takes an online `sqlite3.backup()` snapshot verified with `quick_check` before `init_db()`. |
| `get_db_session()` / `get_db()` | Lifecycle | Low | `except Exception` skipped the rollback for `CancelledError`, and `get_db` never rolled back explicitly. | `except BaseException`, plus an explicit rollback in `get_db` when a transaction is open. |
| `recommendations_engine` household floor → `deletions.js` | Contract Drift | Med | `_guard: "household_floor"` lived only in the in-memory verdict. The card's "Your call" tooltip said "merely fine". | `_with_guard()` stores `guard` inside the existing `score_factors` JSON (no schema change; older rows have none). `_load_score_factors` passes it through, and `_yourCallWhy(p)` picks the tooltip. |
| `DELETION_RUN_SUMMARY` ↔ `deletions.js` / Activity | Contract | — (verified) | Keys match. `lane_closed` is a failure kind with a `DESCRIPTIONS` sentence. The banner reads `failed_reason` (by design) and treats `failed >= judged` as "nothing judged", which is correct because outages decrement `judged`. `curation.js` and the Activity view do not read this structure: Activity uses `task_monitor.last_runs`. | No change. |
| Card fallbacks (`rec.confidence \|\| 0.7`) | Contract | — (verified) | Absent everywhere. The card uses `score_factors` for "Why?", and `confidence > .7` only for the `is-hot` styling. | No change. |
| PIN removal | Contract | Low | Endpoints, model, UI and table were gone. Two log lines still said "until the PIN flow decrypts it" / "Cannot update without PIN". (`api.js`'s "PIN nonce" is the Plex login PIN and is correct.) | Log lines now name the real remedy, the next taste rebuild. |
| AppSec errors ↔ UI | Contract | — (verified) | `setup/test` returns 400 with `NEW_ADDRESS_NEEDS_KEY`. `_errMsg` parses the JSON `detail` and both callers render it inline as `.test-status err`. `library/test` returns `{ok:false,error}` to `_showTestResult`. `app_context.py` is pinned by `test_app_context_drift.py` (green). | No change. |
| Bulk-delete runner (`recommendations.py`) | Pool | Low | It holds one connection for the run, but every claim commits before an await (`_transition_if_open`), and an in-memory guard allows one run at a time. | No change. |

---

## 3. Patches (as applied)

The code is in the commit; these are the load-bearing shapes.

### 3.1 `metadata_cache.py`: per-thread connection, bulk read

```python
_local = threading.local()

def _thread_slot(path: Path) -> _Slot:
    slots = getattr(_local, "slots", None)
    if slots is None:
        slots = _local.slots = {}
    ident = _file_ident(path)                 # (st_dev, st_ino) or None
    slot = slots.get(str(path))
    if slot is not None and (ident is None or slot.ident != ident):
        slot.conn.close(); slot = None        # file replaced / deleted
    if slot is None:
        slot = slots[str(path)] = _Slot(_open(path), _file_ident(path))  # PRAGMAs + schema once
    return slot

class MetadataCache:
    def __init__(self, cache_path=None):
        self.cache_path = Path(cache_path or settings.ENRICHMENT_CACHE)
        self._owner = threading.get_ident()
        self._slot = _thread_slot(self.cache_path); self._slot.refs += 1
        self._closed = False
    @property
    def conn(self):                           # always the CALLING thread's connection
        if threading.get_ident() == self._owner and not self._closed:
            return self._slot.conn
        return _thread_slot(self.cache_path).conn
    def __enter__(self): return self
    def __exit__(self, *exc): self.close()
    def __del__(self): self.close()
    def close(self):                          # last handle out rolls back leftovers
        if getattr(self, "_closed", True): return
        self._closed = True; self._slot.refs -= 1
        if self._slot.refs <= 0 and self._slot.conn.in_transaction:
            self._slot.conn.rollback()
    def get_cache_many(self, keys) -> dict:   # one IN per 500 keys, v2 beats v1
        ...
```

```python
# recommendations_engine.py
def _bulk_cached_ratings(items, category) -> dict:
    keys = [_rating_cache_key(it, category) for it in items]
    with MetadataCache() as cache:
        found = cache.get_cache_many(keys)
    return {k: _rating_from_cached(found.get(k)) for k in keys}
```

### 3.2 `app_state.py`: atomic `set_state`

```python
_UPSERT_SQL = ("INSERT INTO app_state (key, value, updated_at) VALUES (:k, :v, :ts) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
               "updated_at = excluded.updated_at")

def set_state(key, value):
    try:
        with get_db_session() as db:
            db.execute(text(_UPSERT_SQL), {"k": key, "v": value, "ts": datetime.utcnow().isoformat()})
    except OperationalError as e:
        if "database is locked" not in str(e).lower():
            raise
        if not force_set_state(key, value, timeout_s=5 if _on_event_loop() else 30):
            logger.warning("[app_state] %s=%r not written — the database stayed locked", key, value)

async def aset_state(key, value):
    await run_write(set_state, key, value)

def set_states_if_changed(values: dict) -> dict:     # {key: previous} for changed keys
    ...  # one SELECT … IN, upsert only the changed ones, one transaction
```

### 3.3 Off-loop game watcher and deletion scoring

```python
async def job_game_watcher():
    running = await asyncio.to_thread(is_game_running)
    st = await asyncio.to_thread(lane_status)
    mode = (("game" if st["game"] else "cpu" if st["summarizer"] == "cpu" else "paused")
            if st["gpu_pressed"] else ("game" if running else "free"))
    changed = await run_write(set_states_if_changed, {
        "game_active": "1" if running else "0",
        "llm_lane": mode, "llm_lane_reason": st["reason"] or ""})
    if "llm_lane" in changed:
        logger.info("[lane] %s -> %s%s", changed["llm_lane"] or "unknown", mode, ...)
    if running:
        await unload_llm_models()
```

```python
# generate_deletion_proposals — phase A
_ctx = await asyncio.to_thread(_load_phase_a)          # every DB read, one worker call
...
_prefetched_embeddings, _ratings_by_key = await asyncio.gather(
    run_chroma(chroma_db.get_by_ids, ids),             # dedicated Chroma thread
    asyncio.to_thread(_bulk_cached_ratings, arr_items, category))
for _idx, item in enumerate(arr_items):
    if _idx and _idx % 250 == 0:
        await asyncio.sleep(0)                         # no transaction open here
    ...
_tech_idx = await asyncio.to_thread(load_tech_index)
```

```python
# chromadb_wrapper.py
async def run_chroma(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_get_chroma_pool(), partial(fn, *args, **kwargs))
```

### 3.4 Contract and migration fixes

```python
def _with_guard(sf, cand, verdict):
    guard = (verdict or {}).get("_guard")
    if not guard:
        return sf
    return {**(sf or {"total": round(cand.get("score") or 0, 1), "factors": []}), "guard": guard}
```

```js
export function _yourCallWhy(p) {
  return p.score_factors?.guard === 'household_floor'
    ? 'The curator leaned toward cutting it, but someone else in your household has watched or played it — so the decision is yours'
    : 'Curatarr finds it merely fine, not a clear cut — the decision is yours';
}
```

```python
# _migrate_columns
with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
    conn.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        ...  # ALTERs, indexes, the attempt_count backfill, PRAGMA user_version
        conn.exec_driver_sql("COMMIT")
    except BaseException as e:
        conn.exec_driver_sql("ROLLBACK")
        _mig_log.error("[migrate] column migration failed and was rolled back: %s", e)
        raise
```

---

## Not done here

- The remaining route handlers are still `async def` with synchronous SQLAlchemy calls in the
  body (§3.2 of the concurrency audit). That is a separate, endpoint-by-endpoint migration onto
  `run_read`/`run_write`.
- `busy_timeout` stays at 60 s until those handlers are off the loop.
- Leases for the `*_running` flags (music enricher across restarts), batch commits in
  `plex_sync._write_entries`, and VACUUM gating are unchanged.

*Status 2026-10-19: all three done, see `docs/SILENT_FAILURE_SWEEP_2026_10.md` §3.*
- *Handlers: 80 await-free handlers became plain `def` (round 3), and the rest moved their DB
  work onto `run_read`/`run_write` (round 5); `src/database/loop_guard.py` and
  `tests/test_loop_db_ratchet.py` keep statements off the loop.*
- *Timeouts: `busy_timeout` is 15 s (`connection.BUSY_TIMEOUT_S`, round 5; the enrichment
  cache too, round 6), `pool_timeout` 10 s, `pool_size` 10.*
- *`music_pipeline_running` is a cross-process lease (`src/services/state_lease.py`).
  `plex_sync._write_entries` commits every 2,000 rows (`_WRITE_BATCH`). VACUUM is postponed
  while long jobs run and skips files with < 20 % free pages (`scheduler.job_db_vacuum`).
  All three from follow-up #6.*
