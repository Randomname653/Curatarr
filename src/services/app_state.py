"""
Curatarr - App State Manager

Persistent key-value store in the DB for runtime flags.
Replaces reading .env at runtime for things like:
  - last_sync_at
  - setup_complete
  - initial_enrichment_done
  - last_proactive_check_at
"""

import logging
from datetime import datetime
from typing import Optional
from src.database.connection import get_db_session
from src.database.models import AppState


def get_state(key: str) -> Optional[str]:
    with get_db_session() as db:
        row = db.query(AppState).filter(AppState.key == key).first()
        return row.value if row else None


_UPSERT_SQL = (
    "INSERT INTO app_state (key, value, updated_at) VALUES (:k, :v, :ts) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
    "updated_at = excluded.updated_at"
)


def _on_event_loop() -> bool:
    """True when called from a thread that is running an asyncio loop — a
    blocking wait there freezes every request and SSE stream."""
    import asyncio
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def set_state(key: str, value: str) -> bool:
    """Write one key in ONE statement (INSERT … ON CONFLICT DO UPDATE).

    It used to read the row and then UPDATE or INSERT: two round trips, and
    two first writers of a new key both took the INSERT branch, so one died
    on the primary key. The upsert has no window. From a coroutine, prefer
    ``await aset_state(...)``, which runs it on the DB writer thread.

    Returns True when the value was written, False when the database stayed
    locked through the retry (logged at WARNING). Most callers ignore the
    result; lock releases must not — see ``release_state_lock``.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    try:
        with get_db_session() as db:
            db.execute(text(_UPSERT_SQL),
                       {"k": key, "v": value, "ts": datetime.utcnow().isoformat()})
    except OperationalError as e:
        # The write lost the busy_timeout race to ANOTHER process (the
        # out-of-process music enricher, update_db.py). Every scheduler
        # heartbeat and progress write funnels through here, so a hard failure
        # would crash background jobs and 500 endpoints. One more attempt on
        # a direct connection — short on the event loop, where every second
        # of waiting freezes the server (it was a further 120 s on top of the
        # 60 s already spent), longer off it.
        if "database is locked" not in str(e).lower():
            raise
        if not force_set_state(key, value, timeout_s=5 if _on_event_loop() else 30):
            logging.getLogger(__name__).warning(
                "[app_state] %s=%r not written — the database stayed locked", key, value)
            return False
    return True


async def aset_state(key: str, value: str) -> bool:
    """``set_state`` from a coroutine: runs on the single DB writer thread, so
    the event loop never waits on SQLite's write lock. Same bool result."""
    from src.database.runner import run_write
    return await run_write(set_state, key, value)


async def aget_state(key: str) -> Optional[str]:
    """``get_state`` from a coroutine: runs on a worker thread (WAL readers
    run concurrently), so the event loop never waits on SQLite."""
    from src.database.runner import run_read
    return await run_read(get_state, key)


async def aset_states_if_changed(values: dict) -> dict:
    from src.database.runner import run_write
    return await run_write(set_states_if_changed, values)


async def aget_datetime(key: str) -> Optional[datetime]:
    from src.database.runner import run_read
    return await run_read(get_datetime, key)


async def aset_datetime(key: str, dt: datetime) -> None:
    from src.database.runner import run_write
    await run_write(set_datetime, key, dt)


async def aacquire_state_lock(key: str, value: str = "1") -> bool:
    """``acquire_state_lock`` on the DB writer thread. Same atomic result."""
    from src.database.runner import run_write
    return await run_write(acquire_state_lock, key, value)


async def arelease_state_lock(key: str) -> bool:
    """``release_state_lock`` on the DB writer thread. Same bool result; a
    release that did not land is still logged at ERROR."""
    from src.database.runner import run_write
    return await run_write(release_state_lock, key)


def set_states_if_changed(values: dict) -> dict:
    """Write only the keys whose value differs from what is stored, in ONE
    transaction. Returns {key: previous value (None when absent)} for the
    keys that changed — empty when nothing did. For heartbeats that would
    otherwise write the same value every tick (the game watcher wrote
    ``game_active`` 2,880 times a day)."""
    from sqlalchemy import bindparam, text
    if not values:
        return {}
    with get_db_session() as db:
        current = dict(db.execute(
            text("SELECT key, value FROM app_state WHERE key IN :keys")
            .bindparams(bindparam("keys", expanding=True)),
            {"keys": list(values)}).all())
        changed = {k: current.get(k) for k, v in values.items() if current.get(k) != v}
        ts = datetime.utcnow().isoformat()
        for k in changed:
            db.execute(text(_UPSERT_SQL), {"k": k, "v": values[k], "ts": ts})
    return changed


def get_datetime(key: str) -> Optional[datetime]:
    val = get_state(key)
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except Exception:
        return None


def set_datetime(key: str, dt: datetime) -> None:
    set_state(key, dt.isoformat())


def acquire_state_lock(key: str, value: str = "1") -> bool:
    """Atomic compare-and-set on an app_state key, used as a coarse mutex.

    Returns ``True`` when the caller now holds the "lock" (key was missing
    or held a different value), ``False`` if it's already set to ``value``
    by some other caller. Atomic at the DB level via a single
    ``INSERT … ON CONFLICT(key) DO UPDATE … WHERE value != :value``
    statement, so a read-then-write race between two concurrent POSTs
    can't both pass the check.

    Pair with ``release_state_lock(key)`` (or ``set_state(key, "0")``)
    in the corresponding ``finally:`` block. Crash-safe by design — an
    interrupted run leaves the key set, but the caller's startup hook
    (``main.lifespan``) clears stale flags so the next process boot
    starts unlocked.
    """
    from sqlalchemy import text
    now_iso = datetime.utcnow().isoformat()
    with get_db_session() as db:
        result = db.execute(
            text(
                """
                INSERT INTO app_state (key, value, updated_at)
                VALUES (:k, :v, :now)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                WHERE app_state.value != :v
                """
            ),
            {"k": key, "v": value, "now": now_iso},
        )
        db.commit()
        # rowcount == 1 means we either inserted or updated → we hold the lock.
        # rowcount == 0 means the row already had this value → someone else holds it.
        return result.rowcount > 0


def release_state_lock(key: str) -> bool:
    """Symmetric counterpart to ``acquire_state_lock`` — sets the key to ``"0"``.

    Returns True when the release landed. set_state's "database stayed
    locked" path used to return normally, so this reported success while the
    key stayed "1" and every later acquire failed until the next boot reset.
    A release that did not land is logged at ERROR with the manual fix.
    """
    ok = set_state(key, "0") is not False
    if not ok:
        logging.getLogger(__name__).error(
            "[app_state] lock %r NOT released — the database stayed locked; it "
            "stays held until the next restart (or set it to '0' by hand)", key)
    return ok


def force_set_state(key: str, value: str, timeout_s: int = 120) -> bool:
    """Resilient ``set_state`` for crash-cleanup paths.

    Pass 89b. Normal ``set_state`` goes through the shared SQLAlchemy
    engine. When the SQLite write-lock is held longer than the engine's
    ``busy_timeout`` — or when the engine's pool itself is in a degraded
    state after a cascading error — the calling script's ``finally:``
    cleanup block fails too. That leaves stale ``music_pipeline_running``
    / ``enrichment_running`` flags that block every future run until a
    server restart or manual reset.

    This bypasses the SQLAlchemy engine entirely: a fresh ``sqlite3``
    connection is opened with ``timeout=timeout_s`` (SQLite's built-in
    busy-wait — 120 s by default), and a single ``INSERT … ON CONFLICT
    … DO UPDATE`` writes the value atomically in auto-commit mode.
    Returns True on success, False on failure (so the caller can log a
    "manual reset needed" hint).

    Use only for crash-cleanup / signal-handler paths. Hot-path writes
    should use ``set_state`` so they benefit from connection pooling
    and the engine's PRAGMAs.
    """
    import sqlite3
    from src.database.connection import engine

    # The engine's parsed URL, not string surgery: ``.lstrip("/")`` on the
    # URL turned the POSIX path /home/…/curatarr.db into a RELATIVE one, so
    # on Linux this opened (or created) the wrong file, found no app_state
    # table and returned False — the stale flag stayed. Windows paths
    # ("C:/…") only worked by accident.
    db_path = engine.url.database
    try:
        conn = sqlite3.connect(db_path, timeout=timeout_s, isolation_level=None)
        try:
            conn.execute(
                """
                INSERT INTO app_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, datetime.utcnow().isoformat()),
            )
            return True
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return False



