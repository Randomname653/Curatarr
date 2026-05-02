"""
ARR Suite LLM - Library Setup Router

Discovers Plex library sections and lets the admin configure
what each library contains (music / anime / show / movie).
"""

import logging
from datetime import datetime

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.config import settings
from src.database import get_db
from src.database.models import LibraryConfig, User
from src.routers.auth import get_current_user, require_admin

logger = logging.getLogger(__name__)
router = APIRouter()

VALID_CATEGORIES = {"music", "anime", "show", "movie", "ignore"}

PLEX_TYPE_DEFAULTS = {
    "artist": "music",
    "movie":  "movie",
    "show":   "show",   # will be overridden by title heuristic
}

ANIME_TITLE_HINTS = {"anime", "アニメ", "animation"}


def _guess_category(section: dict) -> str:
    """Heuristic guess based on library type and title."""
    ptype = section.get("type", "").lower()
    title = section.get("title", "").lower()

    if ptype == "artist":
        return "music"
    if ptype == "movie":
        return "movie"
    if ptype == "show":
        if any(hint in title for hint in ANIME_TITLE_HINTS):
            return "anime"
        return "show"
    return "ignore"


# ── DISCOVER ─────────────────────────────────────────────────────────────────

@router.get("/discover")
async def discover_libraries(user: User = Depends(get_current_user)):
    """
    Fetch all Plex library sections with stats: item count, size, scan time, paths.
    """
    plex_url = settings.effective_plex_url
    plex_token = settings.effective_plex_token

    if not plex_url or not plex_token:
        raise HTTPException(status_code=503, detail="Plex not configured")

    headers = {
        "Accept": "application/json",
        "X-Plex-Token": plex_token,
        "X-Plex-Client-Identifier": settings.PLEX_CLIENT_ID,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{plex_url}/library/sections", headers=headers)

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Plex returned {resp.status_code}")

    sections = resp.json().get("MediaContainer", {}).get("Directory", []) or []

    # Fetch item counts per section from Plex (one call per section, parallel)
    async def _fetch_section_count(client: httpx.AsyncClient, key: str) -> int:
        try:
            r = await client.get(
                f"{plex_url}/library/sections/{key}/all",
                params={"X-Plex-Container-Size": "0", "X-Plex-Container-Start": "0"},
                headers=headers,
            )
            if r.status_code == 200:
                return r.json().get("MediaContainer", {}).get("totalSize", 0)
        except Exception:
            pass
        return 0

    import asyncio as _asyncio
    async with httpx.AsyncClient(timeout=15) as client:
        counts = await _asyncio.gather(
            *[_fetch_section_count(client, s.get("key", "")) for s in sections],
            return_exceptions=True,
        )

    # DB counts per media category (WatchHistoryEntry has no section_id column)
    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry, EnrichmentStatus
    from sqlalchemy import func as _func

    with get_db_session() as db:
        db_counts = dict(
            db.query(WatchHistoryEntry.media_type, _func.count(WatchHistoryEntry.id))
            .group_by(WatchHistoryEntry.media_type)
            .all()
        )
        enriched_counts = dict(
            db.query(EnrichmentStatus.media_category,
                     _func.count(EnrichmentStatus.id))
            .filter(EnrichmentStatus.enriched == True)
            .group_by(EnrichmentStatus.media_category)
            .all()
        )

    result = []
    for i, s in enumerate(sections):
        key = s.get("key")
        cat = _guess_category(s)
        plex_count = counts[i] if not isinstance(counts[i], Exception) else 0

        # Parse timestamps
        def _ts(unix):
            if not unix:
                return None
            try:
                return datetime.utcfromtimestamp(int(unix)).isoformat() + "Z"
            except Exception:
                return None

        locations = [loc.get("path", "") for loc in (s.get("Location") or [])]

        result.append({
            "key": key,
            "title": s.get("title"),
            "type": s.get("type"),
            "suggested_category": cat,
            "plex_item_count": plex_count,
            "db_entry_count": db_counts.get(cat, 0),
            "enriched_count": enriched_counts.get(cat, 0),
            "scanned_at": _ts(s.get("scannedAt")),
            "updated_at": _ts(s.get("updatedAt")),
            "created_at": _ts(s.get("createdAt")),
            "locations": locations,
            "agent": s.get("agent", ""),
            "language": s.get("language", ""),
        })

    return {"sections": result}


# ── SAVE CONFIG ───────────────────────────────────────────────────────────────

class LibraryConfigItem(BaseModel):
    key: str
    title: str
    plex_type: str
    category: str   # music / anime / show / movie / ignore


class LibraryConfigRequest(BaseModel):
    libraries: list[LibraryConfigItem]


@router.post("/configure")
async def configure_libraries(
    req: LibraryConfigRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Save library category mappings (admin only)."""
    for item in req.libraries:
        if item.category not in VALID_CATEGORIES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid category '{item.category}'. Must be one of {VALID_CATEGORIES}"
            )
        existing = db.query(LibraryConfig).filter(
            LibraryConfig.plex_section_key == item.key
        ).first()
        if existing:
            existing.media_category = item.category
            existing.plex_section_title = item.title
            existing.configured_at = datetime.utcnow()
            existing.configured_by = user.id
        else:
            db.add(LibraryConfig(
                plex_section_key=item.key,
                plex_section_title=item.title,
                plex_section_type=item.plex_type,
                media_category=item.category,
                configured_by=user.id,
            ))
    db.commit()
    return {"status": "ok", "configured": len(req.libraries)}


@router.get("/config")
async def get_library_config(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return current library configuration."""
    configs = db.query(LibraryConfig).all()
    return {
        "configured": len(configs) > 0,
        "libraries": [
            {
                "key": c.plex_section_key,
                "title": c.plex_section_title,
                "plex_type": c.plex_section_type,
                "category": c.media_category,
            }
            for c in configs
        ]
    }


# ── ORPHAN REPAIR ─────────────────────────────────────────────────────────────

@router.get("/orphaned")
async def get_orphaned_sections(user: User = Depends(get_current_user)):
    """Detect history entries from unknown library sections."""
    from src.services.orphan_repair import detect_orphaned_sections
    sections = await detect_orphaned_sections()
    return {"orphaned": sections, "has_orphans": len(sections) > 0}


class OrphanMapping(BaseModel):
    section_id: str
    title: str
    category: str
    plex_type: str = "show"


class OrphanRepairRequest(BaseModel):
    mappings: list[OrphanMapping]


@router.post("/repair-orphans")
async def repair_orphans(
    req: OrphanRepairRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_admin),
):
    """Apply mappings for orphaned sections and import missing entries."""
    from src.services.orphan_repair import apply_orphan_mapping
    result = await apply_orphan_mapping([m.dict() for m in req.mappings])
    return result
