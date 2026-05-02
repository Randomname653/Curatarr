"""Curatarr - Database Connection"""

import asyncio
from contextlib import contextmanager
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config import settings
from src.database.models import Base

# Application-level write lock — serializes all DB writes to prevent SQLite locking
# SQLite WAL allows concurrent reads but only one writer at a time
_db_write_lock = asyncio.Lock()

# Check if we're using in-memory SQLite and adjust pool settings accordingly
if "sqlite:///:memory:" in settings.DATABASE_URL:
    engine = create_engine(
        settings.DATABASE_URL,
        connect_args={
            "check_same_thread": False,
        },
        pool_pre_ping=True,
        poolclass=StaticPool,
    )
else:
    engine = create_engine(
        settings.DATABASE_URL,
        connect_args={
            "check_same_thread": False,
            "timeout": 60,  # SQLite connection-level timeout (seconds)
        } if "sqlite" in settings.DATABASE_URL else {},
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_timeout=60,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

if "sqlite" in settings.DATABASE_URL:
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.execute("PRAGMA wal_autocheckpoint=1000")
        cursor.execute("PRAGMA cache_size=-64000")
        cursor.close()

    @event.listens_for(engine, "checkout")
    def on_checkout(dbapi_connection, connection_record, connection_proxy):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.close()


@contextmanager
def get_db_session():
    """Context manager for use in services/background tasks."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db():
    """FastAPI dependency — yields a session and closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        from src.database.models import User
        if not db.execute(select(User)).first():
            pass  # Removed print statement as requested
    finally:
        db.close()
