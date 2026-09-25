"""
Curatarr - Tasks Router

Live task monitoring via Server-Sent Events.
"""

import asyncio
import json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from src.routers.auth import get_current_user, require_admin
from src.database.models import User
from src.services.task_monitor import task_monitor
from src.services.stream_tickets import create_ticket, redeem_ticket

logger = logging.getLogger(__name__)
router = APIRouter()


# Tasks are server-wide, and their names say what the server is doing for
# whom ("Deletion analysis: anime", "Plex sync", a memory extraction for a
# thread). A member sees the tasks that are theirs — the ids carry the user
# id for those — and nothing else; an admin sees everything (2026-09-25).
_OWNED_TASK_ID = re.compile(r"^(?:del-analysis|recs-cache)-(\d+)$|^memx-(\d+)-")


def _owner_of(task_id: str):
    m = _OWNED_TASK_ID.match(task_id or "")
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def _visible(tasks: list, user_id: int, is_admin: bool) -> list:
    if is_admin:
        return tasks
    return [t for t in tasks if _owner_of(t.get("id", "")) == user_id]


def _is_admin(user_id: int) -> bool:
    from src.database.connection import get_db_session
    with get_db_session() as db:
        row = db.query(User.is_admin).filter(User.id == user_id).first()
    return bool(row and row[0])


@router.get("/")
async def get_tasks(user: User = Depends(get_current_user)):
    return {"tasks": _visible(task_monitor.get_all(), user.id, bool(user.is_admin))}


@router.get("/running")
async def get_running(user: User = Depends(get_current_user)):
    return {"tasks": _visible(task_monitor.get_running(), user.id, bool(user.is_admin))}


@router.get("/history")
async def get_task_history(user: User = Depends(get_current_user)):
    """Last completed run per category (in-memory, resets on restart).
    Per category means one entry can be another user's run, so members get
    none; their own tasks are in the list above."""
    return {"last_runs": task_monitor.last_runs if user.is_admin else {}}


@router.get("/ticket")
async def get_stream_ticket(user: User = Depends(get_current_user)):
    """Issue a short-lived one-time ticket for the SSE stream (avoids JWT in URL)."""
    ticket = create_ticket(user.id)
    return {"ticket": ticket}


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, _admin: User = Depends(require_admin)):
    """Request cancellation of a running task. Admin only.

    Tasks are server-wide (sync, enrichment, music pipeline, etc.); cancelling
    them affects every user, so this is gated behind admin auth.
    """
    ok = task_monitor.cancel(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="task_not_found_or_already_finished")
    return {"ok": True, "task_id": task_id}


@router.get("/stream")
async def stream_tasks(ticket: str = Query(None)):
    """
    SSE stream. Authenticates via a one-time ticket only — fetch one with
    ``GET /api/tasks/ticket`` first.

    The legacy ``?token=<JWT>`` fallback is gone: the frontend has used
    tickets exclusively for some time, and accepting a raw JWT in a query
    string was an unnecessary attack surface (proxies/access logs persist
    URLs).
    """
    user_id = redeem_ticket(ticket) if ticket else None

    if not user_id:
        # 401 is the right answer; an SSE client treats it as a normal HTTP
        # error and surfaces it on `EventSource.onerror`. Returning a 200
        # SSE frame containing {"error": "auth_failed"} (the previous
        # behavior) made the failure look like a successful stream.
        raise HTTPException(status_code=401, detail="missing_or_invalid_ticket")

    is_admin = _is_admin(user_id)
    queue = task_monitor.subscribe()

    async def generate():
        from src.services.task_monitor import shutdown_event
        try:
            snapshot = _visible(task_monitor.get_all(), user_id, is_admin)
            yield f"data: {json.dumps(snapshot)}\n\n"

            while not shutdown_event.is_set():
                # Wait on the queue AND the shutdown event: an endless
                # while-True keepalive loop held uvicorn's graceful shutdown
                # open ("Waiting for connections to close") until the user
                # closed every browser tab by hand.
                get_t = asyncio.create_task(queue.get())
                shut_t = asyncio.create_task(shutdown_event.wait())
                try:
                    done, pending = await asyncio.wait(
                        {get_t, shut_t}, timeout=15.0,
                        return_when=asyncio.FIRST_COMPLETED)
                except asyncio.CancelledError:
                    get_t.cancel(); shut_t.cancel()
                    break
                for p in pending:
                    p.cancel()
                if shut_t in done:
                    break
                if get_t in done:
                    yield f"data: {json.dumps(_visible(get_t.result(), user_id, is_admin))}\n\n"
                else:
                    yield ": keepalive\n\n"
        finally:
            task_monitor.unsubscribe(queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
