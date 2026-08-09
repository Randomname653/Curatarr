"""
Curatarr — shutdown bridge between the web endpoint and the process host.

In dev/CLI mode uvicorn owns the process and the /api/system/shutdown
endpoint raises SIGINT — the exact Ctrl+C path. In tray mode uvicorn runs on
a worker thread inside pythonw: signals are unreliable there (the main
thread is blocked in pystray's Win32 message pump), and a raised SIGINT
would bypass uvicorn's graceful path entirely.

So the tray launcher registers a stop callback here (it sets
``server.should_exit = True``); the endpoint calls ``request_app_stop()``
first and only falls back to SIGINT when nothing is registered. Import-free
on purpose — no cycle risk from any direction.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_stop_cb: Optional[Callable[[], None]] = None


def register(cb: Callable[[], None]) -> None:
    """Called by the process host (tray launcher) at startup."""
    global _stop_cb
    _stop_cb = cb


def request_app_stop() -> bool:
    """Ask the registered host to stop the app. True if a host handled it."""
    if _stop_cb is None:
        return False
    try:
        _stop_cb()
        return True
    except Exception as e:
        logger.warning("[shutdown-bridge] stop callback failed: %s", e)
        return False
