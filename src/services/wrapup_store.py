"""
Curatarr — the wrapups table: one row per (user, scope, period) holding the
last computed facts and the last written story. Blocking — run_read for
load, run_write for the saves.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from src.database.connection import get_db_session


def _row(db, user_id: int, scope: str, period_key: str, create: bool = False):
    from src.database.models import Wrapup
    row = (db.query(Wrapup).filter(Wrapup.user_id == user_id, Wrapup.scope == scope,
                                   Wrapup.period_key == period_key).first())
    if row is None and create:
        row = Wrapup(user_id=user_id, scope=scope, period_key=period_key)
        db.add(row)
    return row


def _loads(text: Optional[str]):
    try:
        return json.loads(text) if text else None
    except (ValueError, TypeError):
        return None                 # a corrupt row reads as "not computed yet"


def load(user_id: int, scope: str, period_key: str) -> Optional[dict]:
    with get_db_session() as db:
        row = _row(db, user_id, scope, period_key)
        if row is None:
            return None
        return {"facts": _loads(row.facts_json), "facts_hash": row.facts_hash,
                "facts_at": row.facts_at, "story": _loads(row.story_json),
                "story_hash": row.story_hash,
                "story_at": row.story_at.isoformat() if row.story_at else None,
                "story_source": row.story_source, "story_model": row.story_model,
                "error": row.error}


def save_facts(user_id: int, scope: str, period_key: str, facts: dict) -> None:
    with get_db_session() as db:
        row = _row(db, user_id, scope, period_key, create=True)
        row.facts_json = json.dumps(facts, default=str)
        row.facts_hash = facts.get("facts_hash")
        row.facts_at = datetime.utcnow()
        db.commit()


def save_story(user_id: int, scope: str, period_key: str, facts_hash: str,
               story: dict, source: str, model: Optional[str] = None) -> None:
    with get_db_session() as db:
        row = _row(db, user_id, scope, period_key, create=True)
        row.story_json = json.dumps(story, default=str)
        row.story_hash = facts_hash
        row.story_at = datetime.utcnow()
        row.story_source = source
        row.story_model = model
        row.error = None
        db.commit()


def save_error(user_id: int, scope: str, period_key: str, error: str) -> None:
    with get_db_session() as db:
        row = _row(db, user_id, scope, period_key, create=True)
        row.error = (error or "")[:500]
        db.commit()
