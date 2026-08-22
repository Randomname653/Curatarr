"""
Curatarr - Stats Router

The curation report: aggregated debate history, storage wins, stubbornness,
redundancy and taste evolution (services/curation_stats). Admin-only —
this is the curation cockpit, not a household page.
"""

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends

from src.database.models import User
from src.routers.auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter()


def _narrative_key(user_id: int, year: int) -> str:
    return f"curation_narrative:{user_id}:{year}"


@router.get("/curation")
async def curation_stats(months: int = 12,
                         user: User = Depends(require_admin)):
    from src.services.app_state import get_state
    from src.services.curation_stats import build_curation_stats
    data = build_curation_stats(user.id, months=max(1, min(int(months or 12), 24)))
    try:
        cached = json.loads(
            get_state(_narrative_key(user.id, datetime.utcnow().year)) or "{}")
        data["narrative"] = cached.get("text")
    except Exception:
        data["narrative"] = None
    return data


@router.post("/curation/narrative")
async def write_narrative(user: User = Depends(require_admin)):
    """The curator writes ONE paragraph over this year's numbers (cached per
    user+year in app_state; regenerating overwrites)."""
    from src.services.app_state import set_state
    from src.services.curation_stats import build_curation_stats
    from src.services.recommendations_engine import _call_llm

    data = build_curation_stats(user.id, months=12)
    t = data["totals"]
    stubborn_titles = ", ".join(s["title"] for s in data["stubbornness"][:5]) or "none"
    prompt = f"""[MODE: YEARLY CURATION REVIEW]
You are Curatarr, the household's opinionated media curator, writing your yearly review of the CURATION work (not their viewing habits).
THE NUMBERS: {t['deleted']} titles deleted ({t['gb_freed']} GB freed), {t['kept']} kept after debate, {t['consensus']} consensus resolutions vs {t['overrides']} owner overrides.
STUBBORNNESS LIST (kept over your objection, untouched 90+ days): {stubborn_titles}.
REDUNDANT STORAGE STILL ON DISK: {data['duplicates'].get('total_redundant_gb', 0)} GB.
Write ONE paragraph (4-6 sentences, English, dry wit allowed): what the numbers say about this collection and the owner, one pointed observation about the stubbornness list, one about what next year should bring. No bullet lists, no headers."""
    text = await _call_llm(prompt, max_tokens=400)
    if not text:
        return {"ok": False, "error": "curator unavailable"}
    text = text.strip()
    set_state(_narrative_key(user.id, datetime.utcnow().year),
              json.dumps({"text": text,
                          "written_at": datetime.utcnow().isoformat()}))
    return {"ok": True, "narrative": text}
