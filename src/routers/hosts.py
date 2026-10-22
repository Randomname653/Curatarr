"""The machines Curatarr computes on (2026-10-22, NAS move phase 2).

POST /api/hosts/pc/goodbye   the PC agent's shutdown notice. No user login:
                             the agent proves itself with PC_AGENT_TOKEN.
GET  /api/hosts              the PC's state, for admins.
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
