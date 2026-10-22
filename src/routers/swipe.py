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
    want: bool = False                     # not seen, wants to watch it
    year: Optional[int] = Field(default=None, ge=1870, le=2100)   # disambiguates the watchlist match


def _skip_ids(skip: str) -> set:
    return {d for d in (skip or "").split(",") if d.strip()}


async def _posters(cards: list, cat: str) -> None:
    from src.routers.recommendations import _fetch_tmdb
    # One round for a full stack (n <= 8): the live log had /next at
    # 0.6-1.5 s with two rounds of TMDB round-trips behind a limit of 4.
    sem = asyncio.Semaphore(8)

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


@router.get("/next")
async def next_cards(category: str = Query("show"), n: int = Query(8, ge=1, le=20),
                     seed: Optional[str] = Query(None, max_length=128),
                     skip: str = Query("", max_length=4000),
                     user: User = Depends(get_current_user)):
    """The next cards for this user, nearest to their taste first. Empty with
    a reason when there is no taste profile for the category yet.

    ``seed``: a doc id the user just loved - the cards are its nearest
    unanswered neighbours instead (empty, no reason, when it has none).
    ``skip``: comma-separated doc ids already waiting in the client's stack."""
    cat = _category(category)
    uid = user.id
    if seed:
        cards = await sw.chain_cards(uid, cat, seed, min(n, 5), _skip_ids(skip))
        await _posters(cards, cat)
        return {"cards": cards}
    cards = await sw.next_cards(uid, cat, n, _skip_ids(skip))
    if not cards:
        return {"cards": [], "reason": "no_taste"
                if not await run_read(sw._taste_queries, uid, cat) else "done"}
    await _posters(cards, cat)
    return {"cards": cards}


@router.post("/answer")
async def answer(body: SwipeAnswer, user: User = Depends(get_current_user)):
    """Store one answer. Every REBUILD_EVERY seen answers in a category
    rebuild that category's taste vector in the background (no LLM
    summaries). A want-to-watch answer also goes on the user's plex.tv
    watchlist; the response says whether that worked."""
    cat = _category(body.category)
    if not body.seen and body.stars is not None:
        raise HTTPException(status_code=400, detail="Stars only go with 'seen'")
    if body.seen and body.want:
        raise HTTPException(status_code=400, detail="'Want to watch' only goes with 'not seen'")
    uid = user.id
    count = await run_write(sw.save_answer, uid, cat, body.doc_id, body.title,
                            body.seen, body.stars, body.want)
    rebuild = count >= sw.REBUILD_EVERY
    if rebuild:
        await run_write(sw.reset_rebuild_counter, uid, cat)
        from src.services.bg_tasks import track_task
        from src.services.taste_engine import compute_all_taste_vectors
        track_task(compute_all_taste_vectors(uid, [cat], skip_summaries=True),
                   name=f"swipe_taste_rebuild_{cat}")
    out = {"ok": True, "taste_rebuild": rebuild,
           "since_rebuild": 0 if rebuild else count, "rebuild_every": sw.REBUILD_EVERY}
    if body.want and body.title:
        # The same write chat makes for "I want to watch X": the user's own
        # plex.tv watchlist, so it shows in their Plex apps. Never raises;
        # the answer is stored either way and the card says what happened.
        from src.services.plex_watchlist import add_to_watchlist
        res = await add_to_watchlist(uid, body.title, year=body.year, media_type=cat)
        if not res.get("ok"):
            logger.info("[swipe] %r not added to the watchlist: %s", body.title, res.get("error"))
        out["watchlist"] = {"ok": bool(res.get("ok")), "matched": res.get("matched"),
                            "error": res.get("error")}
    return out


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
    """Answers per category for this user, and how far each is from its next
    taste rebuild. A plain def: FastAPI runs it on a worker thread, so the
    read stays off the event loop."""
    return {"stats": sw.stats(user.id), "since_rebuild": sw.since_rebuild(user.id),
            "rebuild_every": sw.REBUILD_EVERY}
