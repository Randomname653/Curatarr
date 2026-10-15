"""Model updates (admin): is there new weights for a model in use, test old
against new, keep the pin or take the update. Logic: services/model_updates."""
import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.database.models import User
from src.routers.auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter()

# Single-worker app: an in-process flag is enough and resets on reload,
# like the OMDb backfill guard (routers/enrichment.py).
# ``kind`` says what runs ("test" = pull + both sides + review, "review"
# = the review alone), so the bell and Maintenance can say which.
_running: dict = {"role": None, "kind": None, "since": None}


def _start(role: str, kind: str) -> None:
    from datetime import datetime, timezone
    _running.update(role=role, kind=kind, since=datetime.now(timezone.utc).isoformat(timespec="seconds"))


def _stop() -> None:
    _running.update(role=None, kind=None, since=None)


def _running_view() -> dict | None:
    return dict(_running) if _running["role"] else None


@router.get("")
async def status():
    from src.services import model_updates as mu
    return {"models": mu.public_view(mu.load_state()), "running": _running_view()}


@router.post("/check")
async def check():
    from src.services import model_updates as mu
    await mu.check_updates()
    return {"models": mu.public_view(mu.load_state()), "running": _running_view()}


def _note_eval_error(role: str, e: Exception) -> None:
    from src.services import model_updates as mu
    state = mu.load_state()
    if role in state:
        state[role]["eval_error"] = f"{type(e).__name__}: {e}"[:300]
        mu.save_state(state)


async def _compare_bg(role: str, user_id: int) -> None:
    from src.services import model_updates as mu
    from src.services.task_monitor import task_monitor
    task = task_monitor.create(name=f"Model update test ({role})", category="models")
    task_monitor.start(task)
    try:
        res = await mu.run_comparison(role, user_id, task=task)
        s = res["summary"]
        rec = ""
        try:
            ev = await mu.run_evaluation(role, user_id, task=task)
            rec = f"; the current model recommends: {ev['recommendation']}"
        except Exception as e:   # the comparison stands even if the review fails
            logger.warning("[model update] %s review failed: %s", role, e)
            _note_eval_error(role, e)
            rec = "; the review failed — run it again from Maintenance"
        task_monitor.done(task, f"Old vs new: {s['verdicts_same']}/{s['verdicts_compared']} "
                                f"verdicts unchanged, wording {s['wording_similarity_pct']} % alike{rec}")
    except Exception as e:
        logger.warning("[model update] %s comparison failed: %s", role, e)
        state = mu.load_state()
        state.setdefault(role, {})["compare_error"] = str(e)
        mu.save_state(state)
        task_monitor.done(task, f"Model update test failed: {e}")
    finally:
        _stop()


@router.post("/{role}/compare")
async def compare(role: Literal["curator", "pitcher"], fresh: bool = False,
                  user: User = Depends(require_admin)):
    """Start the old-vs-new test. ``fresh``: a result exists and the owner
    wants the whole thing again — the result moves to the history and the
    new run draws titles the earlier ones did not use."""
    from src.services import model_updates as mu
    if _running["role"]:
        raise HTTPException(409, f"already running: the {_running['kind']} for the {_running['role']}")
    row = mu.load_state().get(role) or {}
    if row.get("status") != "update":
        raise HTTPException(409, "no update pending for this model")
    if row.get("comparison") and not fresh:
        raise HTTPException(409, "a result exists — decide, review it again, or run a fresh test")
    _start(role, "test")
    state = mu.load_state()
    if fresh:
        mu.archive_comparison(state[role])
    state[role].pop("compare_error", None)
    state[role].pop("eval_error", None)
    mu.save_state(state)
    asyncio.create_task(_compare_bg(role, user.id))
    return {"started": True}


async def _evaluate_bg(role: str, user_id: int) -> None:
    from src.services import model_updates as mu
    from src.services.task_monitor import task_monitor
    task = task_monitor.create(name=f"Model update review ({role})", category="models")
    task_monitor.start(task)
    try:
        ev = await mu.run_evaluation(role, user_id, task=task)
        task_monitor.done(task, f"Review done — recommendation: {ev['recommendation']}")
    except Exception as e:
        logger.warning("[model update] %s review failed: %s", role, e)
        _note_eval_error(role, e)
        task_monitor.done(task, f"Review failed: {e}")
    finally:
        _stop()


@router.post("/{role}/evaluate")
async def evaluate(role: Literal["curator", "pitcher"], user: User = Depends(require_admin)):
    """Review an existing comparison again (or for the first time, for a
    result from before the review existed)."""
    from src.services import model_updates as mu
    if _running["role"]:
        raise HTTPException(409, f"already running: the {_running['kind']} for the {_running['role']}")
    if not (mu.load_state().get(role) or {}).get("comparison"):
        raise HTTPException(409, "compare first")
    _start(role, "review")
    state = mu.load_state()
    state[role].pop("eval_error", None)
    mu.save_state(state)
    asyncio.create_task(_evaluate_bg(role, user.id))
    return {"started": True}


class DecideRequest(BaseModel):
    action: Literal["update", "stay"]


@router.post("/{role}/decide")
async def decide(role: Literal["curator", "pitcher", "summarizer"], req: DecideRequest):
    from src.services import model_updates as mu
    if _running["role"]:
        raise HTTPException(409, "wait for the running test to finish")
    try:
        await mu.decide(role, req.action)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"models": mu.public_view(mu.load_state())}
