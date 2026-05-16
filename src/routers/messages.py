"""Curatarr 1.0 - Proactive Messages Router"""

from fastapi import APIRouter, Depends
from src.routers.auth import get_current_user
from src.database.models import User
from src.services.proactive_messages import get_unread_messages, mark_message_read

router = APIRouter()


@router.get("/unread")
async def get_unread(user: User = Depends(get_current_user)):
    result = await get_unread_messages(user.id)
    return result


@router.post("/{message_id}/read")
async def read_message(message_id: int, user: User = Depends(get_current_user)):
    ok = await mark_message_read(message_id, user.id)
    return {"ok": ok}
