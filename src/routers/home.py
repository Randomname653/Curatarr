"""Curatarr - the member's living room: the Tonight card and the one-tap
actions every member surface shares (UX audit 2026-11-08, §2.1 A, §2.3 D).

* GET /tonight - one pick for this moment of the member's week
  (services/tonight.tonight_card), with a link into their media server;
* GET /moments - the Wrap-Up's cards for the moment (services/wrapup_cards);
* POST /watchlist - a title onto the member's own watchlist (the Swipe
  "Want to watch" write), from the Tonight card, a chat reply or a
  recommendation card;
* POST /keep - "never propose this for deletion", explicitly, from a chat
  reply: the protection the chat's keep-detector writes, without the guess.

Every signed-in member, for themselves."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from src.database.models import User
from src.database.runner import run_read
from src.routers.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/tonight")
async def tonight(user: User = Depends(get_current_user)):
    """{"card": {moment, weekday, items: [{title, category, kind, headline,
    why, link?}]} | null}. The first item is the pick; the rest are what
    "Something else" steps through."""
    from src.services.tonight import tonight_card
    from src.services.server_links import attach_links
    card = await run_read(tonight_card, user.id)
    if card:
        await attach_links(card["items"])
        for it in card["items"]:
            it.pop("key", None)          # the link is what the client needs
    return {"card": card}


@router.get("/moments")
async def moments(user: User = Depends(get_current_user)):
    """The Wrap-Up's small moments for the landing (services/wrapup_cards):
    "Your week" from Sunday evening to Monday, "Your month is ready" in a
    month's first days, "Your year is ready" from 1 December - on the
    member's clock, only once the story exists. Reads, never writes one.
    {"cards": [...]}, empty most of the time."""
    from src.services.wrapup_cards import moments as _moments
    return {"cards": await run_read(_moments, user.id)}


class WatchlistBody(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    category: str = Field(pattern="^(movie|show|anime)$")
    year: Optional[int] = Field(default=None, ge=1870, le=2100)


@router.post("/watchlist")
async def watchlist(body: WatchlistBody, user: User = Depends(get_current_user)):
    """A title onto the member's own watchlist - the same write as a Swipe
    "Want to watch" (plex.tv watchlist, or the Jellyfin playlist)."""
    from src.services.plex_watchlist import add_to_watchlist
    res = await add_to_watchlist(user.id, body.title, year=body.year, media_type=body.category)
    return {"ok": bool(res.get("ok")), "matched": res.get("matched"), "error": res.get("error")}


class KeepBody(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    category: str = Field(pattern="^(movie|show|anime|music)$")


def keep_title(user_id: int, title: str, category: str) -> dict:
    """Protect ``title`` for this member: a ProtectedMedia row as the chat's
    keep-detector writes it (identifier = title), source "manual". The judge
    reads every member's protections (pillars: a household member kept it);
    the owner's own keeps hold the title out of the proposals. Idempotent.
    Blocking."""
    from datetime import datetime
    from sqlalchemy import or_
    from src.database.connection import get_db_session
    from src.database.models import ProtectedMedia
    with get_db_session() as db:
        # Idempotent per title AND category: a film and a series of one name
        # are kept one by one (a keep without a category holds for both).
        row = (db.query(ProtectedMedia)
               .filter(ProtectedMedia.user_id == user_id, ProtectedMedia.identifier == title,
                       or_(ProtectedMedia.category == category, ProtectedMedia.category.is_(None))).first())
        if row is not None:
            return {"ok": True, "already": True}
        db.add(ProtectedMedia(user_id=user_id, identifier=title, title=title, category=category,
                              reason="Kept from the chat", source="manual", created_at=datetime.utcnow()))
        db.commit()
    logger.info("[keep] user %d kept %r (%s)", user_id, title, category)
    return {"ok": True, "already": False}


@router.post("/keep")
async def keep(body: KeepBody, user: User = Depends(get_current_user)):
    """"Keep it - never propose it for deletion", in one tap."""
    from src.database.runner import run_write
    return await run_write(keep_title, user.id, body.title, body.category)
