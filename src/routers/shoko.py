"""
Curatarr - Shoko routes (services/shoko_sync, services/shoko_pins). Admin only.

  GET    /api/shoko/unmatched      series and seasons whose plays found no Shoko
                                   episode, most plays first, and every pin
  GET    /api/shoko/search?q=      anime in the Shoko collection (fuzzy, or an AniDB id)
  POST   /api/shoko/pins           link a series + season to an AniDB anime, or skip it
  DELETE /api/shoko/pins/{id}      lift a pin

The Shoko key never leaves the server; the browser only sees titles and ids.
"""
import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.database.models import User
from src.database.runner import run_read, run_write
from src.routers.auth import require_admin
from src.services import shoko_client, shoko_pins

logger = logging.getLogger(__name__)
router = APIRouter()


def _require_shoko() -> str:
    if not shoko_client._configured():
        raise HTTPException(status_code=409, detail="Shoko is not set up (Settings -> Integrations)")
    return shoko_client.api_keys()[0]


@router.get("/unmatched")
async def unmatched(_admin: User = Depends(require_admin)):
    data = await run_read(shoko_pins.overview)
    return {"configured": shoko_client._configured(), **data}


@router.get("/search")
async def search(q: str = Query(..., min_length=1, max_length=200),
                 _admin: User = Depends(require_admin)):
    key = _require_shoko()
    async with shoko_client.new_client() as c:
        results = await shoko_client.ShokoSession(c, key).search_collection(q.strip())
    return {"results": results}


class PinRequest(BaseModel):
    series: str = Field(..., min_length=1, max_length=512)
    season: Optional[int] = Field(None, ge=0, le=1000)
    action: Literal["link", "skip"]
    anidb_id: Optional[int] = Field(None, ge=1)
    # The season's episode that is the AniDB entry's episode 1: AniDB splits
    # a season into cours, a second pin starts where the first entry ends.
    from_episode: int = Field(1, ge=1, le=10000)


@router.post("/pins")
async def set_pin(req: PinRequest, admin: User = Depends(require_admin)):
    title = None
    if req.action == "link":
        if not req.anidb_id:
            raise HTTPException(status_code=422, detail="A link needs an AniDB id")
        key = _require_shoko()
        async with shoko_client.new_client() as c:
            series = await shoko_client.ShokoSession(c, key).series_by_anidb(req.anidb_id)
        if not series:
            raise HTTPException(status_code=404,
                                detail="That anime is not in your Shoko collection")
        title = series.get("Name") or ((series.get("AniDB") or {}).get("Title"))
    try:
        res = await run_write(shoko_pins.set_pin, req.series, req.season, req.action,
                              anidb_id=req.anidb_id, shoko_title=title, user_id=admin.id,
                              from_episode=req.from_episode)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    logger.info("[shoko] pin %s: %r season %s -> %s (%d plays requeued)", req.action,
                req.series, req.season, req.anidb_id, res["requeued"])
    return {"ok": True, **res, "shoko_title": title}


@router.delete("/pins/{pin_id}")
async def delete_pin(pin_id: int, _admin: User = Depends(require_admin)):
    res = await run_write(shoko_pins.remove_pin, pin_id)
    if res is None:
        raise HTTPException(status_code=404, detail="No such pin")
    return {"ok": True, **res}
