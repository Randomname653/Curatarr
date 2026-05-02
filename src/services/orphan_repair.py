"""
Curatarr 1.0 - Orphaned Library Repair Service

Finds Plex history entries whose librarySectionID no longer exists
in the library config, and allows the user to remap them.
"""

import asyncio
import logging
from collections import Counter
from datetime import datetime
from typing import Optional

import httpx

from src.config import settings
from src.database.connection import get_db_session
from src.database.models import LibraryConfig, WatchHistoryEntry

logger = logging.getLogger(__name__)

PLEX_TYPE_NUM = {"movie": "1", "show": "4", "anime": "4", "music": "10"}
TYPE_FROM_PLEX = {"movie": "movie", "episode": "show", "track": "music"}


async def detect_orphaned_sections() -> list:
    """
    Fetch Plex history, find librarySectionIDs not in our config.
    Returns list of {section_id, count, plex_types, sample_titles, suggested_category}
    """
    plex_url = settings.effective_plex_url
    plex_token = settings.effective_plex_token
    if not plex_url or not plex_token:
        return []

    headers = {
        "Accept": "application/json",
        "X-Plex-Token": plex_token,
        "X-Plex-Client-Identifier": settings.PLEX_CLIENT_ID,
    }

    with get_db_session() as db:
        known_keys = {c.plex_section_key for c in db.query(LibraryConfig).all()}

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{plex_url}/status/sessions/history/all",
            headers=headers,
            params={"sort": "viewedAt:desc"},
        )
    if r.status_code != 200:
        return []

    items = r.json().get("MediaContainer", {}).get("Metadata", []) or []

    # Group unknown sections
    unknown: dict = {}
    for item in items:
        sid = str(item.get("librarySectionID", "?"))
        if sid in known_keys or sid == "?":
            continue
        if sid not in unknown:
            unknown[sid] = {"count": 0, "types": Counter(), "titles": set(), "items": []}
        unknown[sid]["count"] += 1
        unknown[sid]["types"][item.get("type", "?")] += 1
        title = item.get("grandparentTitle") or item.get("title", "")
        if title:
            unknown[sid]["titles"].add(title)
        if len(unknown[sid]["items"]) < 3:
            unknown[sid]["items"].append(item)

    result = []
    for sid, data in sorted(unknown.items(), key=lambda x: -x[1]["count"]):
        # Auto-suggest category based on dominant Plex type
        dominant_type = data["types"].most_common(1)[0][0] if data["types"] else "?"
        suggested = TYPE_FROM_PLEX.get(dominant_type, "show")

        # Refine suggestion from sample titles
        sample_titles = list(data["titles"])[:8]
        anime_hints = {"One Piece", "Berserk", "Dan Da Dan", "Apothecary", "Dimensional"}
        if any(any(h in t for h in anime_hints) for t in sample_titles):
            suggested = "anime"

        result.append({
            "section_id": sid,
            "count": data["count"],
            "plex_types": dict(data["types"]),
            "sample_titles": sample_titles,
            "suggested_category": suggested,
        })

    return result


async def apply_orphan_mapping(mappings: list) -> dict:
    """
    Apply user-confirmed mappings for orphaned sections.
    mappings: [{section_id, category, title}]

    Steps:
    1. Add LibraryConfig entries for the orphaned sections
    2. Re-fetch history for those sections and insert missing entries
    """
    plex_url = settings.effective_plex_url
    plex_token = settings.effective_plex_token
    headers = {
        "Accept": "application/json",
        "X-Plex-Token": plex_token,
        "X-Plex-Client-Identifier": settings.PLEX_CLIENT_ID,
    }

    # Filter out "ignore" mappings
    active = [m for m in mappings if m.get("category") != "ignore"]
    if not active:
        return {"saved": 0, "synced": 0}

    # NOTE: We do NOT add orphaned sections to LibraryConfig
    # because those Library keys no longer exist in Plex.
    # Adding them would cause 404 errors on every sync.
    # We only import the history entries directly from session history.
    with get_db_session() as db:
        from src.database.models import User
        admin = db.query(User).filter_by(is_admin=True).first()
        admin_id = admin.id if admin else None

    # Now fetch history for each section from session history
    # (session history has librarySectionID per entry)
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(
            f"{plex_url}/status/sessions/history/all",
            headers=headers,
            params={"sort": "viewedAt:desc"},
        )
    if r.status_code != 200:
        return {"saved": len(active), "synced": 0, "error": "History fetch failed"}

    all_items = r.json().get("MediaContainer", {}).get("Metadata", []) or []
    section_map = {m["section_id"]: m["category"] for m in active}

    # Find admin user
    with get_db_session() as db:
        from src.database.models import User
        admin = db.query(User).filter_by(is_admin=True).first()
        admin_user_id = admin.id if admin else None
        admin_plex_id = admin.plex_user_id if admin else "1"

        # Get existing keys to deduplicate
        existing_keys = set()
        for row in db.query(
            WatchHistoryEntry.user_id,
            WatchHistoryEntry.plex_item_id,
            WatchHistoryEntry.viewed_at,
        ).all():
            existing_keys.add((row.user_id, row.plex_item_id, row.viewed_at))

        synced = 0
        for item in all_items:
            sid = str(item.get("librarySectionID", "?"))
            if sid not in section_map:
                continue

            category = section_map[sid]
            rating_key = str(item.get("ratingKey", ""))
            viewed_at_ts = item.get("viewedAt")
            if not rating_key or not viewed_at_ts:
                continue

            viewed_at = datetime.fromtimestamp(int(viewed_at_ts))
            dedup = (admin_user_id, rating_key, viewed_at)
            if dedup in existing_keys:
                continue

            plex_type = item.get("type", "")
            genres_list = [g.get("tag", "") for g in (item.get("Genre") or [])]

            db.add(WatchHistoryEntry(
                user_id=admin_user_id,
                plex_user_id=str(admin_plex_id),
                plex_item_id=rating_key,
                title=item.get("title", ""),
                media_type=category,
                series_title=item.get("grandparentTitle"),
                season=int(item["parentIndex"]) if item.get("parentIndex") else None,
                episode=int(item["index"]) if item.get("index") else None,
                viewed_at=viewed_at,
                duration_ms=item.get("duration"),
                view_offset_ms=item.get("duration"),  # completed
                completed=True,
                genres=",".join(g for g in genres_list if g),
                tmdb_id=None,
            ))
            existing_keys.add(dedup)
            synced += 1

        db.commit()

    logger.info("Orphan repair: saved %d mappings, synced %d entries", len(active), synced)
    return {"saved": len(active), "synced": synced}
