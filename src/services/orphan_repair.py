"""
Curatarr - Orphaned Library Repair Service

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
from src.database.runner import run_read, run_write
from src.services.http_client import tls_context

logger = logging.getLogger(__name__)

PLEX_TYPE_NUM = {"movie": "1", "show": "4", "anime": "4", "music": "10"}
TYPE_FROM_PLEX = {"movie": "movie", "episode": "show", "track": "music"}


def _plex_is_active() -> bool:
    """Plex history sections mean nothing on a Jellyfin install, even with a
    Plex still named in .env (a server switch needs both): its sections are
    not this install's libraries."""
    from src.services.media_server import active_kind
    return active_kind() == "plex"


async def detect_orphaned_sections() -> list:
    """
    Fetch Plex history, find librarySectionIDs not in our config.
    Returns list of {section_id, count, plex_types, sample_titles, suggested_category}
    """
    if not _plex_is_active():
        return []
    plex_url = settings.effective_plex_url
    plex_token = settings.effective_plex_token
    if not plex_url or not plex_token:
        return []

    headers = {
        "Accept": "application/json",
        "X-Plex-Token": plex_token,
        "X-Plex-Client-Identifier": settings.PLEX_CLIENT_ID,
    }

    def _known_keys():
        with get_db_session() as db:
            return {c.plex_section_key for c in db.query(LibraryConfig).all()}

    known_keys = await run_read(_known_keys)

    async with httpx.AsyncClient(verify=tls_context(), timeout=30) as client:
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
    if not _plex_is_active():
        return {"error": "Plex sections only: this install runs on Jellyfin"}
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

    # Now fetch history for each section from session history
    # (session history has librarySectionID per entry)
    async with httpx.AsyncClient(verify=tls_context(), timeout=60) as client:
        r = await client.get(
            f"{plex_url}/status/sessions/history/all",
            headers=headers,
            params={"sort": "viewedAt:desc"},
        )
    if r.status_code != 200:
        return {"saved": len(active), "synced": 0, "error": "History fetch failed"}

    all_items = r.json().get("MediaContainer", {}).get("Metadata", []) or []
    section_map = {m["section_id"]: m["category"] for m in active}

    # history/all is EVERY Plex account's plays, each tagged with accountID.
    # Crediting them all to the admin moved a member's watches into the
    # admin's taste vector and drop centroid — and out of Pillar III, the one
    # place their engagement protects their shows. Resolve each account to
    # its local user; only an unresolvable account is parked on the admin
    # (where /admin/re-attribute can still move it).
    def _users() -> list:
        from types import SimpleNamespace
        from src.database.models import User
        with get_db_session() as db:
            return [SimpleNamespace(id=u.id, is_admin=u.is_admin,
                                    plex_username=u.plex_username,
                                    plex_user_id=u.plex_user_id)
                    for u in db.query(User).all()]

    from src.services.plex_sync import _fetch_account_to_user_map, fetch_players
    acct_map = await _fetch_account_to_user_map(plex_url, headers,
                                                await run_read(_users))
    players = await fetch_players(plex_url, headers)

    # Find admin user, dedupe and insert: read-modify-write, so ONE helper
    # on the writer thread.
    def _import_entries() -> int:
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

                viewed_at = datetime.utcfromtimestamp(int(viewed_at_ts))
                acc = str(item.get("accountID") or "")
                owner = acct_map.get(acc) if acc else None
                uid = owner.id if owner else admin_user_id
                plex_uid = ((owner.plex_user_id if owner else None)
                            or acc or str(admin_plex_id))
                dedup = (uid, rating_key, viewed_at)
                # The admin-parked copy an earlier (pre-attribution) repair
                # wrote counts too: re-attribution moves it, a re-run must
                # not add a second copy beside it.
                if dedup in existing_keys or \
                        (admin_user_id, rating_key, viewed_at) in existing_keys:
                    continue

                plex_type = item.get("type", "")
                genres_list = [g.get("tag", "") for g in (item.get("Genre") or [])]

                # Plex history events are typically completed plays. Honor any
                # `viewOffset` returned (in-progress); else assume completed.
                # A history event carries no duration: that is no reason to call
                # it unfinished (every Plexamp play since September read as
                # 0 h in the wrap-up); its length is estimated downstream.
                duration_ms = item.get("duration")
                view_offset_ms = item.get("viewOffset") or duration_ms
                completed = not duration_ms or (
                    view_offset_ms is None or view_offset_ms >= duration_ms * 0.9
                )

                try:
                    _player = players.get(int(item.get("deviceID")), (None, None))
                except (TypeError, ValueError):
                    _player = (None, None)
                db.add(WatchHistoryEntry(
                    user_id=uid,
                    plex_user_id=str(plex_uid),
                    plex_item_id=rating_key,
                    title=item.get("title", ""),
                    media_type=category,
                    series_title=item.get("grandparentTitle"),
                    season=int(item["parentIndex"]) if item.get("parentIndex") else None,
                    episode=int(item["index"]) if item.get("index") else None,
                    viewed_at=viewed_at,
                    duration_ms=duration_ms,
                    view_offset_ms=view_offset_ms,
                    completed=completed,
                    genres=",".join(g for g in genres_list if g),
                    tmdb_id=None,
                    player=_player[0], platform=_player[1],
                ))
                existing_keys.add(dedup)
                synced += 1

            db.commit()
        return synced

    synced = await run_write(_import_entries)

    logger.info("Orphan repair: saved %d mappings, synced %d entries", len(active), synced)
    return {"saved": len(active), "synced": synced}
