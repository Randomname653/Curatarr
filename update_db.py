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

Usage::

    python update_db.py
"""

import logging

from src.database.connection import init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DB-Update")


def update_database():
    logger.info("Checking the database for newly-declared tables and columns...")
    init_db()
    logger.info("Done. Missing tables and columns have been added; existing data is untouched.")


if __name__ == "__main__":
    update_database()
