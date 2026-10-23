"""The machines Curatarr computes on (2026-10-22, NAS move phase 2).

POST /api/hosts/pc/goodbye   the PC agent's shutdown notice. No user login:
                             the agent proves itself with PC_AGENT_TOKEN.
GET  /api/hosts              the PC's state, for admins.
POST /api/hosts/pc/update    admins: the PC agent fast-forwards its checkout
                             to main and restarts (src/self_update).
"""
import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from src.database.models import User
from src.routers.auth import require_admin
from src.services import pc_host

logger = logging.getLogger(__name__)
router = APIRouter()


class Goodbye(BaseModel):
    reason: str = Field(default="shutdown", max_length=40)


@router.post("/pc/goodbye")
def pc_goodbye(body: Goodbye, x_agent_token: str = Header(default="")):
    """The PC is going away: stop sending it work now, not after two
    missed polls."""
    tok = pc_host.token()
    if not tok or not x_agent_token or not hmac.compare_digest(
            x_agent_token.encode(), tok.encode()):
        raise HTTPException(status_code=401, detail="Agent token required")
    pc_host.goodbye(body.reason)
    return {"ok": True}


@router.get("")
def hosts_status(user: User = Depends(require_admin)):
    return {"pc": pc_host.status()}


@router.post("/pc/update")
async def pc_update(user: User = Depends(require_admin)):
    """Update the gaming PC's Curatarr: its agent fast-forwards to main and
    restarts. Nothing is sent but the token - the agent fetches from its
    own origin, and refuses on a branch, local changes or local commits."""
    if not pc_host.configured():
        raise HTTPException(status_code=404, detail="No PC agent is configured")
    if not pc_host.available():
        raise HTTPException(status_code=409, detail="The PC is not reachable right now")
    import httpx
    try:
        code, body = await pc_host.request_update()
    except httpx.HTTPError as e:
        logger.warning("[pc] update request failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=502, detail="The PC agent did not answer")
    if code == 501:
        raise HTTPException(status_code=409, detail="The PC runs an agent too old to update "
                                                    "itself: run git pull there once")
    if code != 200:
        raise HTTPException(status_code=409, detail=str(body.get("reason") or f"HTTP {code}")[:200])
    from src.services.notifications import forget_pc_update
    forget_pc_update()
    logger.info("[pc] update: %s", {k: body.get(k) for k in ("ok", "updated", "reason")})
    return {"ok": bool(body.get("ok")), "updated": bool(body.get("updated")),
            "from": body.get("from"), "to": body.get("to"), "reason": body.get("reason") or ""}
