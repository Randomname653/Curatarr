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
_running: dict = {"role": None}


@router.get("")
async def status():
    from src.services import model_updates as mu
    return {"models": mu.public_view(mu.load_state()), "running": _running["role"]}


@router.post("/check")
async def check():
    from src.services import model_updates as mu
    await mu.check_updates()
    return {"models": mu.public_view(mu.load_state()), "running": _running["role"]}


async def _compare_bg(role: str, user_id: int) -> None:
    from src.services import model_updates as mu
    from src.services.task_monitor import task_monitor
    task = task_monitor.create(name=f"Model update test ({role})", category="models")
    task_monitor.start(task)
    try:
        res = await mu.run_comparison(role, user_id, task=task)
        s = res["summary"]
        task_monitor.done(task, f"Old vs new: {s['verdicts_same']}/{s['verdicts_compared']} "
                                f"verdicts unchanged, wording {s['wording_similarity_pct']} % alike")
    except Exception as e:
        logger.warning("[model update] %s comparison failed: %s", role, e)
        state = mu.load_state()
        state.setdefault(role, {})["compare_error"] = str(e)
        mu.save_state(state)
        task_monitor.done(task, f"Model update test failed: {e}")
    finally:
        _running["role"] = None


@router.post("/{role}/compare")
async def compare(role: Literal["curator", "pitcher"], user: User = Depends(require_admin)):
    from src.services import model_updates as mu
    if _running["role"]:
        raise HTTPException(409, f"a test is already running ({_running['role']})")
    row = mu.load_state().get(role) or {}
    if row.get("status") != "update":
        raise HTTPException(409, "no update pending for this model")
    _running["role"] = role
    state = mu.load_state()
    state[role].pop("compare_error", None)
    mu.save_state(state)
    asyncio.create_task(_compare_bg(role, user.id))
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
