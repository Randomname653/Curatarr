"""
Curatarr — logging bootstrap (file + optional console), idempotent.

The server used to log to stderr only; in tray mode (pythonw.exe) there IS no
stderr, so every diagnostic vanished. This sets up:

- a RotatingFileHandler at data/logs/curatarr.log (5 MB x 3, utf-8) — always;
  a test process writes data/logs/tests.log instead (``log_file``);
- a StreamHandler ONLY when a real stderr exists (start.bat dev console).
  Under pythonw ``sys.stderr`` is None and an unconditional StreamHandler
  would raise on every emit.

Idempotent so the tray launcher can call it FIRST (preflight gets logged)
and the later ``import src.main`` re-call becomes a no-op.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.paths import LOG_DIR, ROOT


def is_test_process(script: str | None = None) -> bool:
    """True when the process was started from a script under tests/ (the
    battery, CI, one suite run by hand). ``script`` defaults to the __main__
    module's file (absolute since Python 3.9, so a chdir cannot move it);
    uvicorn, the tray's .pyw, ``-m src.deps_lock`` and ``-c`` are the app."""
    if script is None:
        script = getattr(sys.modules.get("__main__"), "__file__", None) or ""
    try:
        path = Path(script).resolve()
    except Exception:  # noqa: BLE001 — no verdict, the app
        return False
    return path.suffix == ".py" and (ROOT / "tests") in path.parents


def log_file(script: str | None = None) -> Path:
    """curatarr.log, or tests.log for a test process (is_test_process).

    Every suite that imports src.main runs init_logging, and until 2026-09-28
    that meant the live log: two "[slow] GET /api/history/recent" lines from a
    battery run sat in it while the app was down, reading like traffic."""
    return LOG_DIR / ("tests.log" if is_test_process(script) else "curatarr.log")


def init_logging(level: str = "INFO") -> None:
    if getattr(init_logging, "_done", False):
        return
    init_logging._done = True

    root = logging.getLogger()
    root.setLevel(getattr(logging, (level or "INFO").upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s — %(message)s")

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_file(), maxBytes=5_000_000,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception:
        pass  # a broken log dir must never block startup

    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)

    # Noise suppression (moved from main.py so both entries share it):
    # apscheduler logs every single job execution at INFO; httpx logs every
    # request; watchfiles spams change detection in dev --reload mode.
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    for noisy in ("httpx", "httpcore", "watchfiles", "watchfiles.main"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
