"""
Curatarr - App State Manager

Persistent key-value store in the DB for runtime flags.
Replaces reading .env at runtime for things like:
  - last_sync_at
  - setup_complete
  - initial_enrichment_done
  - last_proactive_check_at
"""

from datetime import datetime
from typing import Optional
from src.database.connection import get_db_session
from src.database.models import AppState


def get_state(key: str) -> Optional[str]:
    with get_db_session() as db:
        row = db.query(AppState).filter(AppState.key == key).first()
        return row.value if row else None


def set_state(key: str, value: str) -> None:
    from sqlalchemy.exc import OperationalError
    try:
        with get_db_session() as db:
            row = db.query(AppState).filter(AppState.key == key).first()
            if row:
                row.value = value
                row.updated_at = datetime.utcnow()
            else:
                db.add(AppState(key=key, value=value, updated_at=datetime.utcnow()))
            db.commit()
    except OperationalError as e:
        # The pooled-engine write lost the 60s busy_timeout race during a
        # transient write-lock storm. Every scheduler heartbeat, /api progress
        # write and the music pipeline funnels through here, so a hard failure
        # crashes background jobs and 500s endpoints. Fall back to a fresh
        # direct-sqlite write with its own busy-wait instead of propagating.
        if "database is locked" in str(e).lower():
            force_set_state(key, value)
        else:
            raise


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


def release_state_lock(key: str) -> None:
    """Symmetric counterpart to ``acquire_state_lock`` — sets the key to ``"0"``."""
    set_state(key, "0")


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
    from src.config import settings

    db_path = settings.DATABASE_URL.replace("sqlite:///", "").lstrip("/")
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
