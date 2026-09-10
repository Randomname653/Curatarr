"""
Curatarr - Process Monitor Router

Endpoints for the "Is this a game?" classification UI.
No LLM calls — pure process list + SQLite reads/writes.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.database import get_db
from src.database.connection import get_db_session
from src.database.models import GameProcess
from src.routers.auth import get_current_user
from src.database.models import User
from src.services.ttl_memo import ttl_response

logger = logging.getLogger(__name__)
router = APIRouter()


class ClassifyRequest(BaseModel):
    name: str
    is_game: bool


@router.get("/unknown")
@ttl_response(10)
async def get_unknown_processes(_user: User = Depends(get_current_user)):
    """
    Return running processes that are not yet classified.
    Frontend polls this every 30s and shows a toast for each result.

    Memoized 10s: the cost is the psutil process walk, not the DB read
    (a bot PR cached the wrong half). Classify/delete invalidate it so a
    just-answered prompt never re-appears for a stale TTL.
    """
    from src.services.process_monitor import get_unknown_processes as _scan
    return {"processes": _scan()}


def _forget_process_views() -> None:
    """A classification changed — the next poll must see it."""
    from src.services.process_monitor import invalidate_process_cache
    invalidate_process_cache()
    get_unknown_processes.invalidate()
    game_status.invalidate()


@router.post("/classify")
async def classify_process(
    req: ClassifyRequest,
    _user: User = Depends(get_current_user),
):
    """Persist user's game/not-game decision for a process name."""
    name_lower = req.name.strip().lower()
    if not name_lower:
        raise HTTPException(status_code=400, detail="Process name required")

    with get_db_session() as db:
        row = db.query(GameProcess).filter(
            GameProcess.process_name == name_lower
        ).first()
        if row:
            row.is_game  = req.is_game
            row.added_at = datetime.utcnow()
        else:
            db.add(GameProcess(
                process_name=name_lower,
                is_game=req.is_game,
            ))
        db.commit()

    _forget_process_views()
    logger.info("Process classified: %s → is_game=%s", name_lower, req.is_game)
    return {"ok": True, "name": name_lower, "is_game": req.is_game}


@router.get("/list")
async def list_classified(_user: User = Depends(get_current_user)):
    """Return all user-classified processes (for settings UI)."""
    with get_db_session() as db:
        rows = db.query(GameProcess).order_by(GameProcess.added_at.desc()).all()
        return {
            "processes": [
                {
                    "name": r.process_name,
                    "is_game": r.is_game,
                    "added_at": r.added_at.isoformat() if r.added_at else None,
                }
                for r in rows
            ]
        }


@router.delete("/{process_name}")
async def delete_classification(
    process_name: str,
    _user: User = Depends(get_current_user),
):
    """Remove a classification so the process may be prompted again."""
    name_lower = process_name.strip().lower()
    with get_db_session() as db:
        deleted = db.query(GameProcess).filter(
            GameProcess.process_name == name_lower
        ).delete()
        db.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="Not found")
    _forget_process_views()
    return {"ok": True}


@router.get("/status")
@ttl_response(10)
async def game_status(_user: User = Depends(get_current_user)):
    """Return whether a game is currently running (for the UI badge).

    Pass 75: pure read, no side effects. VRAM unloading is now owned by the
    server-side game watcher (``scheduler.job_game_watcher``), which runs
    every ~30 s regardless of whether the web UI is open — the old
    transition logic here could not guarantee that, and its persistent
    ``game_was_running`` flag could go stale (a running→stopped transition
    the UI never observed left it stuck, so the next game-start saw
    ``was_running == True`` and never unloaded). ``models_unloaded`` stays in
    the response shape for frontend compatibility but is always empty now.
    """
    from src.services.process_monitor import is_game_running

    return {
        "game_running": is_game_running(),
        "models_unloaded": [],
    }
