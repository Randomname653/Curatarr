"""
Curatarr - Stats Router

The curation ledger: aggregated debate history, storage wins, stubbornness,
redundancy and taste evolution (services/curation_stats). Admin-only — it
renders as the owner's section at the bottom of the Wrap-Up; the yearly
paragraph it used to write is the Wrap-Up story now (routers/wrapup).
"""

import logging

from fastapi import APIRouter, Depends

from src.database.models import User
from src.routers.auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/curation")
def curation_stats(months: int = 12,
                         user: User = Depends(require_admin)):
    from src.services.curation_stats import build_curation_stats
    return build_curation_stats(user.id, months=max(1, min(int(months or 12), 24)))
