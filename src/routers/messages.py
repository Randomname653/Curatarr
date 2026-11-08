"""Curatarr - Proactive Messages Router"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
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


class VerdictBody(BaseModel):
    verdict: str


@router.post("/{message_id}/verdict")
async def post_verdict(message_id: int, body: VerdictBody, user: User = Depends(get_current_user)):
    """The follow-up card's one-tap answer: a verdict on the play the message
    asked about (services/post_watch). It retires the message, and the
    category's taste vector is rebuilt in the background without the
    curator's summaries - the same rebuild a tenth Swipe answer starts - so
    the verdict counts now, not at the next sync with new plays."""
    from src.database.runner import run_write
    from src.services import post_watch
    try:
        out = await run_write(post_watch.record_from_message, user.id, message_id, body.verdict)
    except ValueError:
        raise HTTPException(422, f"verdict must be one of {', '.join(post_watch.VERDICTS)}")
    except post_watch.NoPlay:
        raise HTTPException(404, "There is no play of this title on record to weigh")
    except LookupError:
        raise HTTPException(404, "No such message, or it takes no verdict")
    from src.services.bg_tasks import track_task
    from src.services.taste_engine import compute_all_taste_vectors
    track_task(compute_all_taste_vectors(user.id, [out["category"]], skip_summaries=True),
               name=f"verdict_taste_rebuild_{out['category']}")
    return out


@router.post("/{message_id}/read")
async def read_message(message_id: int, user: User = Depends(get_current_user)):
    ok = await mark_message_read(message_id, user.id)
    return {"ok": ok}
