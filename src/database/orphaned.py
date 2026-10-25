"""Plays of media gone from Plex: kept, and hidden from the live app.

The orphan cleanup deleted every ``watch_history`` row whose item had left
all Plex libraries. A title deleted in March then lost its plays before the
year's Wrap-Up was written. The cleanup now sets ``orphaned_at`` instead.

Every ORM SELECT that touches ``WatchHistoryEntry`` gets
``orphaned_at IS NULL`` added here, so the history page, the taste vectors,
the recommendations and the deletion scan see what they saw after a delete.
The readers that are about what happened, not what is in the library, ask
for the marked rows too: ``include(db)`` on their session, or the execution
option ``include_orphaned=True`` on one statement. The Wrap-Up,
the taste snapshots and the Plex sync's duplicate check do; the sync would
otherwise import a hidden play a second time.

Bulk UPDATE and DELETE are not filtered: a re-attribution moves the marked
rows with the rest, and a delete removes them too.
"""
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

INFO_KEY = "include_orphaned"


def include(db: Session) -> Session:
    """Let ``db`` read the marked rows too. Returns it."""
    db.info[INFO_KEY] = True
    return db


def install() -> None:
    from src.database.models import WatchHistoryEntry

    @event.listens_for(Session, "do_orm_execute")
    def _hide_orphaned(state):
        if (not state.is_select or state.is_column_load or state.is_relationship_load
                or state.execution_options.get(INFO_KEY)
                or state.session.info.get(INFO_KEY)):
            return
        state.statement = state.statement.options(with_loader_criteria(
            WatchHistoryEntry, lambda cls: cls.orphaned_at.is_(None),
            include_aliases=True))
