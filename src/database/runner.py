"""Run blocking database work off the event loop.

Every route and scheduler job is a coroutine, so a synchronous SQLAlchemy
call made from one runs on the event-loop thread. When that call waits on
SQLite's write lock (busy_timeout), the whole server waits with it. If the
lock holder is a coroutine suspended on the same loop, it can never resume to
commit, and the wait is a deadlock until the timeout ends it with "database
is locked".

``run_read`` sends a function to the default thread pool; WAL lets readers
run concurrently. ``run_write`` sends it to ONE dedicated thread, so writes
made through it never contend with each other for the lock; busy_timeout
only arbitrates against other writers. Cancelling the awaiting coroutine does
not abort a write that is already running: its transaction finishes or rolls
back atomically in the thread.
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, TypeVar

T = TypeVar("T")

_writer: ThreadPoolExecutor | None = None
_writer_lock = threading.Lock()


def _get_writer() -> ThreadPoolExecutor:
    # Created on first use, so a shutdown (which drops it) never leaves a
    # dead executor behind for a server started again in the same process.
    global _writer
    with _writer_lock:
        if _writer is None:
            _writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-writer")
        return _writer


async def run_read(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    return await asyncio.to_thread(fn, *args, **kwargs)


async def run_write(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_get_writer(), partial(fn, *args, **kwargs))


def shutdown_writer() -> None:
    """Finish every queued write, then drop the writer thread. Called on
    shutdown before the engine is disposed."""
    global _writer
    with _writer_lock:
        writer, _writer = _writer, None
    if writer is not None:
        writer.shutdown(wait=True)
