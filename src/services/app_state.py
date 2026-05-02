"""
Curatarr - App State Manager

Persistent key-value store in the DB for runtime flags.
Replaces reading .env at runtime for things like:
  - last_sync_at
  - setup_complete
  - initial_enrichment_done
  - last_proactive_check_at
"""

from datetime import datetime
from typing import Optional
from src.database.connection import get_db_session
from src.database.models import AppState


def get_state(key: str) -> Optional[str]:
    with get_db_session() as db:
        row = db.query(AppState).filter(AppState.key == key).first()
        return row.value if row else None


def set_state(key: str, value: str) -> None:
    with get_db_session() as db:
        row = db.query(AppState).filter(AppState.key == key).first()
        if row:
            row.value = value
            row.updated_at = datetime.utcnow()
        else:
            db.add(AppState(key=key, value=value, updated_at=datetime.utcnow()))
        db.commit()


def get_datetime(key: str) -> Optional[datetime]:
    val = get_state(key)
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except Exception:
        return None


def set_datetime(key: str, dt: datetime) -> None:
    set_state(key, dt.isoformat())
