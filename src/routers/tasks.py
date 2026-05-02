"""
Curatarr 1.0 - Tasks Router

Live task monitoring via Server-Sent Events.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials

from src.routers.auth import get_current_user, _decode_jwt
from src.database.models import User
from src.database.connection import get_db_session
from src.services.task_monitor import task_monitor

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/")
async def get_tasks(user: User = Depends(get_current_user)):
    return {"tasks": task_monitor.get_all()}


@router.get("/running")
async def get_running(user: User = Depends(get_current_user)):
    return {"tasks": task_monitor.get_running()}


@router.get("/history")
async def get_task_history(user: User = Depends(get_current_user)):
    """Last completed run per category (in-memory, resets on restart)."""
    return {"last_runs": task_monitor.last_runs}


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, user: User = Depends(get_current_user)):
    """Request cancellation of a running task."""
    ok = task_monitor.cancel(task_id)
    return {"ok": ok, "task_id": task_id}


@router.get("/stream")
async def stream_tasks(token: str = Query(...)):
    """
    SSE stream — accepts token as query param because
    EventSource cannot set custom headers.
    """
    # Validate token manually
    user_id = None
    try:
        payload = _decode_jwt(token)
        user_id = int(payload.get("sub", 0))
    except Exception:
        pass

    if not user_id:
        # Return SSE error message instead of HTTP 401 (EventSource can't read 401 body)
        async def auth_error():
            yield f"data: {json.dumps({'error': 'auth_failed'})}\n\n"
        return StreamingResponse(auth_error(), media_type="text/event-stream")

    queue = task_monitor.subscribe()

    async def generate():
        try:
            snapshot = task_monitor.get_all()
            yield f"data: {json.dumps(snapshot)}\n\n"

            while True:
                try:
                    snapshot = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(snapshot)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                except asyncio.CancelledError:
                    break
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
