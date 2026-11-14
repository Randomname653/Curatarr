"""Give freed heap back to the OS after a big batch step.

CPython hands freed objects back to glibc, but glibc keeps the pages: a
taste rebuild that peaked at 900 MB left the container's RSS near the peak
long after the matrices were gone. ``malloc_trim(0)`` returns every free
page at the top AND in the middle of each arena (glibc >= 2.8). Windows
and musl have no such call; there the function is a cheap gc pass.

Called after jobs that allocate hundreds of MB and drop them again (taste
rebuild, Chroma index release, library walks). Never on a request path:
a full gc pass on a big heap takes tens of milliseconds.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import gc
import logging
import sys
import threading

logger = logging.getLogger(__name__)

_libc = None
_libc_lock = threading.Lock()
_libc_tried = False


def _malloc_trim():
    global _libc, _libc_tried
    with _libc_lock:
        if not _libc_tried:
            _libc_tried = True
            if sys.platform.startswith("linux"):
                try:
                    lib = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
                    lib.malloc_trim.argtypes = [ctypes.c_size_t]
                    lib.malloc_trim.restype = ctypes.c_int
                    _libc = lib
                except (OSError, AttributeError) as e:   # musl: no malloc_trim
                    logger.debug("[memory] malloc_trim unavailable: %s", e)
    return getattr(_libc, "malloc_trim", None)


async def reclaim_after_job() -> None:
    """The job runners' hook (custodian tasks, scheduler jobs): off the
    event loop, and never raising into the runner's bookkeeping."""
    import asyncio
    try:
        await asyncio.to_thread(reclaim_memory)
    except Exception as e:  # noqa: BLE001 - housekeeping only
        logger.debug("[memory] reclaim after job failed: %s", e)


def reclaim_memory(collect: bool = True) -> bool:
    """gc pass (unless the caller just ran one), then malloc_trim(0) on
    glibc. True when the trim ran and released something."""
    if collect:
        gc.collect()
    trim = _malloc_trim()
    if trim is None:
        return False
    try:
        return bool(trim(0))
    except Exception as e:  # noqa: BLE001 - housekeeping, never fails a job
        logger.debug("[memory] malloc_trim failed: %s", e)
        return False
