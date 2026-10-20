"""
Curatarr — retention for rows that only ever accumulate (2026-10 audit).

Soft-retirement keeps ids stable and leaves an audit trail, but nothing
ever removed the retired rows, and every one of them rides along in each of
the seven daily backups. The daily ``arr_sync`` alone supersedes a full set
of proposals (each with synopsis, reason and score-factor JSON) every day.

Removed here, and nothing else:
  * deletion proposals ``superseded`` more than 90 days ago, unless a
    discussion thread points at them;
  * proactive messages read or retired more than 90 days ago, same proviso;
  * conversation starters used or expired more than 30 days ago;
  * the per-day OMDb call counters older than a week (``app_state``).

Kept on purpose: ``deleted`` / ``rejected`` / ``error`` proposals (tombstones
the returns check and the catalog sync read, and the owner's keep
decisions), chat history, memories, the resolution log.

Safe to hard-delete now: deletion_proposals is AUTOINCREMENT (models.py),
so a freed id is never handed to a new proposal — the reuse that made Pass
90b switch to soft deletes cannot happen.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

PROPOSAL_SUPERSEDED_DAYS = 90
PROACTIVE_READ_DAYS = 90
STARTER_RETIRED_DAYS = 30
OMDB_COUNTER_DAYS = 7


def _thread_of(prefix: str, id_col):
    """``'<prefix>' || CAST(id AS TEXT)`` — SQL string concatenation.

    Spelled with ``||`` on purpose: ``prefix + cast(...)`` can compile to
    SQLite's arithmetic ``+``, where ``'deletion_proposal:' + '42'`` is 42,
    which matches no thread and would delete rows that HAVE a discussion."""
    from sqlalchemy import String, cast, literal
    return literal(prefix, type_=String).op("||")(cast(id_col, String))


def _undiscussed(prefix: str, id_col):
    from sqlalchemy import exists
    from src.database.models import ConversationMessage as CM
    return ~exists().where(CM.thread_id == _thread_of(prefix, id_col))


def retention_pass(now: datetime | None = None) -> dict:
    """One transaction on the writer thread; returns rows removed per kind."""
    from sqlalchemy import and_, or_
    from src.database.connection import get_db_session
    from src.database.models import AppState, ChatStarter, DeletionProposal, ProactiveMessage
    now = now or datetime.utcnow()
    out = {}
    with get_db_session() as db:
        cut = now - timedelta(days=PROPOSAL_SUPERSEDED_DAYS)
        out["proposals"] = (db.query(DeletionProposal)
            .filter(DeletionProposal.status == "superseded",
                    DeletionProposal.resolved_at.isnot(None),
                    DeletionProposal.resolved_at < cut,
                    _undiscussed("deletion_proposal:", DeletionProposal.id))
            .delete(synchronize_session=False))

        cut = now - timedelta(days=PROACTIVE_READ_DAYS)
        out["proactive"] = (db.query(ProactiveMessage)
            .filter(ProactiveMessage.read == True,  # noqa: E712
                    ProactiveMessage.created_at < cut,
                    _undiscussed("proactive_message:", ProactiveMessage.id))
            .delete(synchronize_session=False))

        cut = now - timedelta(days=STARTER_RETIRED_DAYS)
        out["starters"] = (db.query(ChatStarter)
            .filter(or_(ChatStarter.used_at < cut,
                        and_(ChatStarter.expires_at.isnot(None),
                             ChatStarter.expires_at < cut)))
            .delete(synchronize_session=False))

        # Keys are omdb_calls:YYYY-MM-DD (media_enricher._omdb_day, UTC), so
        # a string compare is a date compare.
        oldest = (now - timedelta(days=OMDB_COUNTER_DAYS)).strftime("%Y-%m-%d")
        out["app_state"] = (db.query(AppState)
            .filter(AppState.key.like("omdb_calls:%"),
                    AppState.key < f"omdb_calls:{oldest}")
            .delete(synchronize_session=False))
        db.commit()
    return out


async def run_retention(task=None) -> bool:
    from src.database.runner import run_write
    from src.services.task_monitor import task_monitor
    removed = await run_write(retention_pass)
    msg = ", ".join(f"{k}: {v}" for k, v in removed.items())
    if task is not None:
        task_monitor.update(task, message=f"Removed — {msg}")
    logger.info("[retention] removed %s", msg)
    return True
