"""Curatarr - Proactive Messages Router"""

from fastapi import APIRouter, Depends
from src.routers.auth import get_current_user
from src.database.models import User
from src.services.proactive_messages import get_unread_messages, mark_message_read

router = APIRouter()


@router.get("/unread")
async def get_unread(seen: bool = False, user: User = Depends(get_current_user)):
    # seen=1 only from the open notification panel: a background badge poll
    # is not an impression (see get_unread_messages).
    result = await get_unread_messages(user.id, seen=seen)
    return result


@router.get("/followup")
async def get_followup(user: User = Depends(get_current_user)):
    """The chat landing's follow-up card: the newest unread "how did it
    land?" message with its one-tap answers, or null."""
    from src.database.runner import run_read
    from src.services.proactive_messages import followup_card
    return {"card": await run_read(followup_card, user.id)}


@router.post("/{message_id}/read")
async def read_message(message_id: int, user: User = Depends(get_current_user)):
    ok = await mark_message_read(message_id, user.id)
    return {"ok": ok}
