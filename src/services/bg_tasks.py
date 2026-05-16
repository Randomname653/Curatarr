"""
Background-task tracker (Pass 41).

Why this exists
---------------
``asyncio.create_task()`` returns a Task object, but the event loop only
holds a WEAK reference to it. If the caller doesn't keep a strong
reference, the garbage collector can collect the Task mid-run and the
coroutine is silently dropped — no error, no log, just unfinished work.

This is documented behaviour:
  https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
  "Important: Save a reference to the result of this function, to avoid
   a task disappearing mid-execution."

The scheduler (services/scheduler.py) already implements this pattern
locally with ``_track_task`` + a module-level set. Pass 41 extracts it
into this shared helper so the same protection covers main.py's
startup tasks, chat.py's memory-extraction / protection-intent /
verification-response background calls, and library.py's prewarm +
re-enrich tasks.

Usage
-----
Replace::

    asyncio.create_task(some_coroutine())

with::

    from src.services.bg_tasks import track_task
    track_task(some_coroutine(), name="some_coroutine")

The ``name`` argument is optional — it shows up in the exception-on-error
log line, which is the main diagnostic value when a fire-and-forget task
crashes silently.

The helper returns the Task so callers can ``.cancel()`` or ``await`` it
if they want, but the typical usage is fire-and-forget.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Coroutine, Any

logger = logging.getLogger(__name__)

# Module-level strong references so the GC can't collect mid-run.
# Tasks self-remove via the done callback below, so the set never grows
# unbounded.
_BG_TASKS: set[asyncio.Task] = set()


def track_task(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task:
    """Create an asyncio task that's retained until it completes.

    Any exception is logged with the task's name so silent failures
    become visible. Cancellation is treated as normal completion (no
    log noise).
    """
    task = asyncio.create_task(coro, name=name) if name else asyncio.create_task(coro)
    _BG_TASKS.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _BG_TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error(
                "[bg_tasks] task %r raised: %s",
                name or t.get_name(), exc, exc_info=exc,
            )

    task.add_done_callback(_on_done)
    return task


def active_count() -> int:
    """Diagnostic helper — how many bg tasks are currently being tracked."""
    return len(_BG_TASKS)
