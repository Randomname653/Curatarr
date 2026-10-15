"""
Curatarr — idempotent schema migration helper.

Runs the same ``init_db()`` the server runs at startup: ``create_all`` adds
newly declared tables, and ``_migrate_columns`` adds columns declared after a
table first existed (``ALTER TABLE ... ADD COLUMN``, guarded by a column
check). Existing data is left untouched. Safe to run as many times as you
want.

Use after pulling a release that adds tables or columns, or after editing
models locally and wanting your dev DB to catch up without dropping data.
(Before 2026-10 this script ran ``create_all`` alone, which never adds a
column to an existing table — only a server start did.)

Two guards, because ``init_db()`` can rebuild a table and drop a retired one:

* it refuses to run while Curatarr answers on its port — a live server (or
  the out-of-process music enricher) holding the write lock is exactly what
  made column adds fail;
* it first takes an online backup of the database next to it, verified with
  ``PRAGMA quick_check``, and names it so a bad migration can be undone.

Usage::

    python update_db.py
"""

import logging
import socket
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from src.config import settings
from src.database.connection import engine, init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DB-Update")


def _server_running() -> bool:
    """True when something answers on Curatarr's port on this machine."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", int(settings.PORT))) == 0


def _backup() -> Path | None:
    """Online backup of the live database file, verified; None for a
    database that does not exist yet (a fresh install has nothing to lose)."""
    db = engine.url.database
    if engine.url.get_backend_name() != "sqlite" or not db or db == ":memory:":
        return None
    src_path = Path(db)
    if not src_path.exists():
        return None
    dest = src_path.with_name(
        f"{src_path.stem}.pre-migrate-{datetime.now():%Y%m%d_%H%M%S}{src_path.suffix}")
    src = sqlite3.connect(src_path, timeout=15)
    dst = sqlite3.connect(dest)
    try:
        with dst:
            src.backup(dst)
        if (dst.execute("PRAGMA quick_check(1)").fetchone() or ["?"])[0] != "ok":
            raise RuntimeError(f"backup {dest} failed quick_check")
    finally:
        dst.close()
        src.close()
    return dest


def update_database():
    if _server_running():
        logger.error("Curatarr is running on port %s — stop it (and "
                     "scripts/music_enricher.py) before migrating.", settings.PORT)
        sys.exit(1)
    backup = _backup()
    if backup:
        logger.info("Backup written: %s", backup)
    logger.info("Checking the database for newly-declared tables and columns...")
    init_db()
    logger.info("Done. Missing tables and columns have been added; existing data is untouched.")
    if backup:
        logger.info("If anything looks wrong, copy %s back over %s.", backup, engine.url.database)


if __name__ == "__main__":
    update_database()
