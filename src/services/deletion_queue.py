"""An Analyze that waits for the node (2026-10-23).

On a split install the deletion run judges on the node only - never on the
server's smaller model. "Run analysis" while the node is off, busy or
playing used to answer "run it again later", and nothing reminded anyone.
Now the request is kept (one per install: the run judges every category the
request names) and started by itself on the first poll that finds the node
free (pc_host.poll_once calls ``kick``). It survives a restart in app_state
and expires after MAX_AGE_S, so a node that stays away for a week does not
fire a stale analysis.

The scheduled scan (scheduler's ARR sync) needs none of this: it stays due
and retries every tick on its own.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

STATE_KEY = "deletion_run_queued"
MAX_AGE_S = 7 * 86400

_mem: dict = {"loaded": False, "req": None, "task": None}


def _parse(raw) -> Optional[dict]:
    try:
        req = json.loads(raw or "null")
    except (TypeError, ValueError):
        return None
    return req if isinstance(req, dict) and req.get("user_id") else None


async def _load() -> None:
    if _mem["loaded"]:
        return
    from src.services.app_state import aget_state
    _mem["req"] = _parse(await aget_state(STATE_KEY))
    _mem["loaded"] = True


async def queue(user_id: int, category: Optional[str], recent_only: bool, recent_days: int) -> dict:
    """Keep the request; a newer one replaces the older (the last click wins)."""
    from src.services.app_state import aset_state
    req = {"user_id": int(user_id), "category": category or None,
           "recent_only": bool(recent_only), "recent_days": int(recent_days),
           "at": time.time()}
    await aset_state(STATE_KEY, json.dumps(req))
    _mem.update(loaded=True, req=req)
    logger.info("[deletions] analysis queued (%s) until the node is free", category or "all")
    return req


async def pending() -> Optional[dict]:
    """The queued request, if any (expired ones are dropped)."""
    await _load()
    req = _mem["req"]
    if req and time.time() - float(req.get("at") or 0) > MAX_AGE_S:
        await _clear()
        logger.info("[deletions] the queued analysis expired unrun")
        return None
    return req


async def _clear() -> None:
    from src.services.app_state import aset_state
    await aset_state(STATE_KEY, "")
    _mem["req"] = None


def note(req: Optional[dict]) -> Optional[str]:
    """The banner text for a queued request."""
    if not req:
        return None
    from src.services.pc_host import node_name
    what = req.get("category") or "all categories"
    return (f"Analysis of {what} is queued: it starts by itself as soon as {node_name()} "
            f"is free. The proposals below stay until then.")


async def kick() -> bool:
    """Start the queued analysis when the node can take it. Called every
    poll; cheap while nothing is queued. True when a run was started."""
    if _mem["loaded"] and _mem["req"] is None:
        return False
    t = _mem["task"]
    if t is not None and not t.done():
        return False
    from src.services.ollama_hosts import judge_ready
    if not judge_ready():
        return False
    req = await pending()
    if not req:
        return False
    await _clear()
    from src.services.bg_tasks import track_task
    _mem["task"] = track_task(_run(req), name="deletion_run_queued")
    logger.info("[deletions] the node is free - starting the queued analysis (%s)",
                req.get("category") or "all")
    return True


async def _run(req: dict) -> None:
    from src.database.connection import get_db_session
    from src.database.models import User
    from src.database.runner import run_read
    from src.routers.recommendations import get_deletion_proposals

    def _user():
        with get_db_session() as db:
            u = db.query(User).filter(User.id == req["user_id"]).first()
            if u is not None:
                db.expunge(u)
            return u

    user = await run_read(_user)
    if user is None or not user.is_admin:
        logger.info("[deletions] the queued analysis's user is gone or no admin - dropped")
        return
    try:
        out = await get_deletion_proposals(category=req.get("category"), refresh=True,
                                           recent_only=bool(req.get("recent_only")),
                                           recent_days=int(req.get("recent_days") or 7),
                                           user=user)
        logger.info("[deletions] queued analysis done: %s",
                    out.get("message") or f"{len(out.get('proposals') or [])} proposals")
    except Exception as e:  # noqa: BLE001 - a background run; the log and the run banner say why
        logger.warning("[deletions] queued analysis failed: %s", e, exc_info=True)
