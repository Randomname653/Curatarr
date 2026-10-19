"""
Curatarr - cross-process lease on an app_state flag.

``music_pipeline_running`` was a bare "0"/"1" that three parties wrote
blindly: the out-of-process ``scripts/music_enricher.py`` took it with a
read-then-write (two runners could both pass), and the server set it to "0"
at every boot and shutdown, in the in-app runner's ``finally`` and nowhere
checked whose it was — a live enricher in another OS process lost its lock
and the custodian started a second pass beside it.

The lease keeps ``<key>`` as "0"/"1", so every reader (/music/ping, /status,
/stop, the scheduler) is unchanged. The OWNER lives next to it in
``<key>_holder`` as ``host|pid|create_time|heartbeat_epoch``. Acquire,
heartbeat, release and reclaim are each ONE ``BEGIN IMMEDIATE`` transaction
on a fresh sqlite3 connection, so the server, the enricher and any other
script serialise on the database file itself.

Liveness: on this machine the PID decides (the process create time guards
against PID reuse). A holder on another machine can only be judged by its
heartbeat, so it goes stale after LEASE_TTL_S without one. A legacy "1" with
no holder is stale — nobody can say who set it.
"""
from __future__ import annotations

import logging
import os
import socket
import sqlite3
import time
from typing import Callable, Optional
from src.database.loop_guard import sqlite_connect

logger = logging.getLogger(__name__)

LEASE_TTL_S = 15 * 60          # cross-host holders only: no heartbeat this long = stale


def _db_path() -> str:
    from src.database.connection import engine
    return engine.url.database


def my_token() -> str:
    """``host|pid|create_time`` of this process."""
    import psutil
    ct = round(psutil.Process(os.getpid()).create_time(), 2)
    return f"{socket.gethostname()}|{os.getpid()}|{ct}"


def _parse(raw: Optional[str]):
    try:
        host, pid, ct, hb = (raw or "").split("|")
        return host, int(pid), float(ct), float(hb)
    except ValueError:
        return None


def holder_alive(raw: Optional[str], now: Optional[float] = None) -> bool:
    """True while the recorded holder still exists (this host) or has
    heartbeated within LEASE_TTL_S (another host)."""
    h = _parse(raw)
    if h is None:
        return False                        # legacy bare "1": owner unknown -> stale
    host, pid, ct, hb = h
    if host != socket.gethostname():
        return ((time.time() if now is None else now) - hb) <= LEASE_TTL_S
    import psutil
    try:
        return ct == 0.0 or abs(psutil.Process(pid).create_time() - ct) < 1.0
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True                         # exists, owned by someone else: not ours to steal


def holder_pid(key: str, db_path: Optional[str] = None) -> Optional[int]:
    """The PID recorded for ``key``'s holder, for log lines. None when unset."""
    try:
        conn = sqlite_connect(db_path or _db_path(), timeout=5)
        try:
            row = conn.execute("SELECT value FROM app_state WHERE key=?",
                               (key + "_holder",)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    h = _parse(row[0] if row else None)
    return h[1] if h else None


def _get(conn, key):
    row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _put(conn, key, value):
    conn.execute(
        "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())))


def _transact(db_path: Optional[str], fn: Callable) -> bool:
    """Run ``fn(conn)`` inside BEGIN IMMEDIATE; commit when it returns True."""
    conn = sqlite_connect(db_path or _db_path(), timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            ok = bool(fn(conn))
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT" if ok else "ROLLBACK")
        return ok
    finally:
        conn.close()


def _stamp(conn, key):
    _put(conn, key + "_holder", f"{my_token()}|{time.time():.0f}")


def _mine(conn, key) -> bool:
    return (_get(conn, key + "_holder") or "").startswith(my_token() + "|")


def acquire(key: str, db_path: Optional[str] = None) -> bool:
    """Take the lease unless a LIVE holder has it; a dead or silent holder
    is taken over in the same transaction."""
    def fn(conn):
        if _get(conn, key) == "1" and holder_alive(_get(conn, key + "_holder")):
            return False
        _put(conn, key, "1")
        _stamp(conn, key)
        return True
    return _transact(db_path, fn)


def heartbeat(key: str, db_path: Optional[str] = None) -> bool:
    """Refresh our heartbeat. False when the lease is no longer ours — the
    caller must stop."""
    def fn(conn):
        if _get(conn, key) != "1" or not _mine(conn, key):
            return False
        _stamp(conn, key)
        return True
    return _transact(db_path, fn)


def release(key: str, db_path: Optional[str] = None) -> bool:
    """Compare-and-clear: only the holder's own release clears the flag."""
    def fn(conn):
        if not _mine(conn, key):
            return False
        _put(conn, key, "0")
        _put(conn, key + "_holder", "")
        return True
    return _transact(db_path, fn)


def reclaim_if_stale(key: str, db_path: Optional[str] = None) -> bool:
    """Boot-time replacement for ``set_state(key, "0")``: clears the flag
    only when its holder is gone. True when the flag is free afterwards."""
    def fn(conn):
        if _get(conn, key) == "1" and holder_alive(_get(conn, key + "_holder")):
            return False
        _put(conn, key, "0")
        _put(conn, key + "_holder", "")
        return True
    return _transact(db_path, fn)
