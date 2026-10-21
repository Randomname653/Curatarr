"""Swipe API: the next cards, one answer at a time, undo, counts (2026-10-21).

Every signed-in user swipes for themselves: answers are per user and only
shape that user's taste vector (services/swipe.py)."""
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.database.models import User
from src.database.runner import run_read, run_write
from src.routers.auth import get_current_user
from src.services import swipe as sw

logger = logging.getLogger(__name__)
router = APIRouter()


def _category(category: str) -> str:
    if category not in sw.SWIPE_CATEGORIES:
        raise HTTPException(status_code=400, detail="Category must be movie, show or anime")
    return category


class SwipeAnswer(BaseModel):
    category: str
    doc_id: str = Field(min_length=1, max_length=128)
    title: str = Field(default="", max_length=512)
    seen: bool
    stars: Optional[int] = Field(default=None, ge=1, le=10)


@router.get("/next")
async def next_cards(category: str = Query("show"), n: int = Query(8, ge=1, le=20),
                     user: User = Depends(get_current_user)):
    """The next cards for this user, nearest to their taste first. Empty with
    a reason when there is no taste profile for the category yet."""
    cat = _category(category)
    uid = user.id
    cards = await sw.next_cards(uid, cat, n)
    if not cards:
        return {"cards": [], "reason": "no_taste"
                if not await run_read(sw._taste_queries, uid, cat) else "done"}
    from src.routers.recommendations import _fetch_tmdb
    sem = asyncio.Semaphore(4)

    async def _poster(c):
        async with sem:
            try:
                poster, synopsis = await _fetch_tmdb(c["title"], cat, tmdb_id=c.get("tmdb_id"),
                                                     year=c.get("year"))
            except Exception as e:  # noqa: BLE001 - a missing poster never costs the card
                logger.warning("[swipe] poster for %r failed: %s", c["title"], e)
                poster, synopsis = None, None
            c["poster_url"] = poster
            if not c.get("synopsis") and synopsis:
                c["synopsis"] = synopsis[:600]
    await asyncio.gather(*(_poster(c) for c in cards))
    return {"cards": cards}


@router.post("/answer")
async def answer(body: SwipeAnswer, user: User = Depends(get_current_user)):
    """Store one answer. Every REBUILD_EVERY answers in a category rebuild
    that category's taste vector in the background (no LLM summaries)."""
    cat = _category(body.category)
    if not body.seen and body.stars is not None:
        raise HTTPException(status_code=400, detail="Stars only go with 'seen'")
    uid = user.id
    count = await run_write(sw.save_answer, uid, cat, body.doc_id, body.title,
                            body.seen, body.stars)
    rebuild = count >= sw.REBUILD_EVERY
    if rebuild:
        await run_write(sw.reset_rebuild_counter, uid, cat)
        from src.services.bg_tasks import track_task
        from src.services.taste_engine import compute_all_taste_vectors
        track_task(compute_all_taste_vectors(uid, [cat], skip_summaries=True),
                   name=f"swipe_taste_rebuild_{cat}")
    return {"ok": True, "taste_rebuild": rebuild}


@router.post("/undo")
async def undo(category: str = Query(...), user: User = Depends(get_current_user)):
    """Take back the latest answer in this category; it comes back as a card."""
    cat = _category(category)
    last = await run_write(sw.undo_last, user.id, cat)
    if last is None:
        raise HTTPException(status_code=404, detail="Nothing to undo")
    return {"ok": True, "undone": last}


@router.get("/stats")
def swipe_stats(user: User = Depends(get_current_user)):
    """Answers per category for this user. A plain def: FastAPI runs it on a
    worker thread, so the read stays off the event loop."""
    return {"stats": sw.stats(user.id)}
