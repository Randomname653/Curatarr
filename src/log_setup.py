"""
Curatarr — logging bootstrap (file + optional console), idempotent.

The server used to log to stderr only; in tray mode (pythonw.exe) there IS no
stderr, so every diagnostic vanished. This sets up:

- a RotatingFileHandler at data/logs/curatarr.log (5 MB x 3, utf-8) — always;
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

from src.paths import LOG_DIR


def init_logging(level: str = "INFO") -> None:
    if getattr(init_logging, "_done", False):
        return
    init_logging._done = True

    root = logging.getLogger()
    root.setLevel(getattr(logging, (level or "INFO").upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(name)s — %(message)s")

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(LOG_DIR / "curatarr.log", maxBytes=5_000_000,
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
