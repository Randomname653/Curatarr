"""Find database statements that run on the event-loop thread.

A synchronous SQLAlchemy call made from a coroutine blocks the whole server
for as long as SQLite takes, and up to busy_timeout when another writer holds
the lock. That is why the timeouts are 60 s, and why they cannot come down
while such calls remain. This module makes those calls visible: an engine
hook checks, for every statement, whether an asyncio loop is running on the
calling thread, and if so records the call site: the innermost coroutine
in ``src/`` on the stack (the code that should have awaited a worker), or
the first ``src/`` frame outside ``src/database/`` when no coroutine is.

Each site is logged once at WARNING and counted. ``snapshot()`` feeds
``GET /api/system/loop-guard``. ``CURATARR_LOOP_GUARD`` sets the mode:

    warn   (default) log once per site, count every statement
    raise  raise LoopBlockingQuery, for tests that pin a path as loop-free
    off    no hook at all

The lifespan's own startup (before it yields) and its final teardown run
while no request can be waiting, so they are counted under ``lifecycle``
and not logged: the lifespan calls ``serving(False)`` on entry,
``serving(True)`` right before its yield and ``serving(False)`` again once
background work has drained. Outside a lifespan (tests) every statement on
a running loop counts.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sqlite3
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_SRC = str(Path(__file__).resolve().parents[1]) + os.sep
_DB_DIR = str(Path(__file__).resolve().parent) + os.sep

_ASYNC_FLAGS = inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR
_lock = threading.Lock()
_sites: dict[str, int] = {}      # "src/x.py:123 (func)" -> statements on the loop
_coro_sites: set[str] = set()    # sites attributed to a src/ coroutine (not a test's)
_lifecycle = 0                   # statements while no request could be waiting
_serving = True
_installed: set[int] = set()


class LoopBlockingQuery(RuntimeError):
    """A statement ran on the event-loop thread while the guard is in raise mode."""


def mode() -> str:
    m = os.environ.get("CURATARR_LOOP_GUARD", "warn").strip().lower()
    return m if m in ("warn", "raise", "off") else "warn"


def serving(on: bool) -> None:
    """The lifespan flips this around its yield; see the module docstring."""
    global _serving
    _serving = on


def _fmt(f) -> str:
    rel = f.f_code.co_filename[len(_SRC) - len("src" + os.sep):].replace(os.sep, "/")
    return f"{rel}:{f.f_lineno} ({f.f_code.co_name})"


def _call_site(depth: int = 3) -> tuple[str, bool]:
    """(site, True when it is a src/ coroutine). A site that is only a sync
    src/ function was reached from a coroutine outside src/ (a test)."""
    f, first = sys._getframe(depth), None
    while f is not None:
        path = f.f_code.co_filename
        if path.startswith(_SRC) and not path.startswith(_DB_DIR):
            if f.f_code.co_flags & _ASYNC_FLAGS:
                return _fmt(f), True
            first = first or f
        f = f.f_back
    return (_fmt(first) if first is not None else "<outside src>"), False


def _record(label: str = "") -> str | None:
    """Count one statement if it runs on the loop thread; the site, or None.
    Called directly from a hook: the caller's caller is the driver."""
    global _lifecycle
    if asyncio._get_running_loop() is None:
        return None
    if not _serving:
        with _lock:
            _lifecycle += 1
        return None
    site, from_coro = _call_site()
    site += label
    with _lock:
        first = site not in _sites
        _sites[site] = _sites.get(site, 0) + 1
        if from_coro:
            _coro_sites.add(site)
    if first:
        logger.warning(
            "Database statement on the event-loop thread at %s - the server "
            "waits for it; move it to run_read/run_write or a def handler",
            site)
    return site


def _on_execute(conn, cursor, statement, parameters, context, executemany):
    site = _record()
    if site is not None and mode() == "raise":
        raise LoopBlockingQuery(f"database statement on the event loop at {site}")


def sqlite_connect(database, *args, **kwargs) -> sqlite3.Connection:
    """``sqlite3.connect`` for the app's other SQLite files (the enrichment
    cache, plex_music.db, the offline indexes, ...). Their statements are
    counted like the engine's, labelled with the file name. A trace callback
    cannot abort a statement, so raise mode does not apply here: tests read
    ``snapshot()`` instead. src/ opens no SQLite file any other way
    (tests/test_loop_db_ratchet.py)."""
    con = sqlite3.connect(database, *args, **kwargs)
    if mode() != "off":
        name = str(database).rsplit("?", 1)[0].replace("\\", "/").rsplit("/", 1)[-1]
        label = f" [{name}]"
        con.set_trace_callback(lambda _stmt: _record(label))
    return con


def install(engine) -> None:
    """Attach the hook to an engine (idempotent; a no-op in off mode)."""
    if mode() == "off" or id(engine) in _installed:
        return
    from sqlalchemy import event
    event.listen(engine, "before_cursor_execute", _on_execute)
    _installed.add(id(engine))


def snapshot() -> dict:
    with _lock:
        sites = sorted(_sites.items(), key=lambda kv: -kv[1])
        lifecycle = _lifecycle
    return {
        "mode": mode(),
        "serving": _serving,
        "sites": [{"site": s, "statements": n, "coroutine": s in _coro_sites}
                  for s, n in sites],
        "statements_on_loop": sum(n for _, n in sites),
        "lifecycle_statements": lifecycle,
    }


def reset() -> None:
    global _lifecycle
    with _lock:
        _sites.clear()
        _coro_sites.clear()
        _lifecycle = 0


def _dump_at_exit() -> None:
    """With CURATARR_LOOP_GUARD_DUMP=<dir>, every process writes its snapshot
    there on exit; tests/run_all.py collects them over the whole battery."""
    out = os.environ.get("CURATARR_LOOP_GUARD_DUMP")
    if not out or not _sites:
        return
    import json
    try:
        Path(out, f"{os.getpid()}.json").write_text(
            json.dumps({"argv": sys.argv, **snapshot()}), encoding="utf-8")
    except OSError as e:
        logger.warning("loop guard: could not write %s: %s", out, e)


if os.environ.get("CURATARR_LOOP_GUARD_DUMP"):
    import atexit
    atexit.register(_dump_at_exit)
