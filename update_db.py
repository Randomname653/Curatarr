"""
Curatarr — idempotent schema migration helper.

Runs ``Base.metadata.create_all`` against the live SQLite DB so any tables
newly declared in ``src/database/models.py`` are added. Existing tables
and data are left untouched (``create_all`` only emits ``CREATE TABLE IF
NOT EXISTS``). Safe to run as many times as you want.

Use after pulling a release that adds new tables, or after editing models
locally and wanting your dev DB to catch up without dropping data.

Usage::

    python update_db.py
"""

import logging

from src.database.connection import engine
from src.database.models import Base

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DB-Update")


def update_database():
    logger.info("Checking the database for newly-declared tables...")
    Base.metadata.create_all(bind=engine)
    logger.info("Done. Any missing tables have been created; existing data is untouched.")


if __name__ == "__main__":
    update_database()
