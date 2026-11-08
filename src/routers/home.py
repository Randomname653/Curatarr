"""Curatarr - the landing's Tonight card (UX audit 2026-11-08, §2.1 A).

One pick for this moment of the member's week (services/tonight.tonight_card)
with a link into their media server and, for a stored pick, a one-tap
watchlist add. Every signed-in member, for themselves."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from src.database.models import User
from src.database.runner import run_read
from src.routers.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()

_PLEX_WEB = "https://app.plex.tv/desktop/#!/server/{mid}/details?key=%2Flibrary%2Fmetadata%2F{key}"


async def _links(items: list) -> None:
    """Set ``item["link"]`` where the item's key opens it on the server: on
    Plex through app.plex.tv (works for every account the server is shared
    with, at home or away), on Jellyfin through the server's own web app."""
    from src.services.media_server import active_kind
    from src.services.media_server.base import is_jellyfin_key, is_plex_key
    try:
        if active_kind() == "jellyfin":
            from src.services.media_server import get_adapter
            jf = get_adapter()
            for it in items:
                if is_jellyfin_key(it.get("key")):
                    it["link"] = jf.deep_link(str(it["key"]))
            return
        from src.services.plex_playlists import get_machine_identifier
        mid = await get_machine_identifier()
        if not mid:
            return
        for it in items:
            if is_plex_key(it.get("key")):
                it["link"] = _PLEX_WEB.format(mid=mid, key=it["key"])
    except Exception as e:  # noqa: BLE001 - the card works without links; logged
        logger.warning("[tonight] no server links: %s", e)


@router.get("/tonight")
async def tonight(user: User = Depends(get_current_user)):
    """{"card": {moment, weekday, items: [{title, category, kind, headline,
    why, link?}]} | null}. The first item is the pick; the rest are what
    "Something else" steps through."""
    from src.services.tonight import tonight_card
    card = await run_read(tonight_card, user.id)
    if card:
        await _links(card["items"])
        for it in card["items"]:
            it.pop("key", None)          # the link is what the client needs
    return {"card": card}


class WatchlistBody(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    category: str = Field(pattern="^(movie|show|anime)$")
    year: Optional[int] = Field(default=None, ge=1870, le=2100)


@router.post("/tonight/watchlist")
async def tonight_watchlist(body: WatchlistBody, user: User = Depends(get_current_user)):
    """A Tonight pick onto the member's own watchlist - the same write as a
    Swipe "Want to watch" (plex.tv watchlist, or the Jellyfin playlist)."""
    from src.services.plex_watchlist import add_to_watchlist
    res = await add_to_watchlist(user.id, body.title, year=body.year, media_type=body.category)
    return {"ok": bool(res.get("ok")), "matched": res.get("matched"), "error": res.get("error")}
