"""
Curatarr — curator-designed COLLECTIONS inside Plex (transport layer).

Sibling of plex_playlists.py: together they are the ONLY Plex-write modules.
Collections differ from playlists in two load-bearing ways, both probe-
verified against the live server (scripts/dev/probe_plex_collections.py,
2026-08-13):

- they are SECTION-scoped and visible to EVERY account → this is household
  curation, pushed once with the OWNER token (no per-user sets);
- a series ratingKey stays a series (childCount counts shows, no episode
  explosion), and DELETE /library/collections/{key} removes ONLY the
  collection — the media items stay untouched.

The server also runs Kometa with hundreds of its own collections, so every
mutating operation here touches EXCLUSIVELY collections whose title starts
with COLLECTION_PREFIX.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

import httpx

from src.services.plex_playlists import (_base, _headers, _owner_token,
                                         get_machine_identifier)

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0

COLLECTION_PREFIX = "Curatarr · "


async def list_collections(section_key: str) -> list[dict]:
    token = _owner_token()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(f"{_base()}/library/sections/{section_key}/collections",
                        headers=_headers(token))
    if r.status_code != 200:
        raise RuntimeError(f"collection list HTTP {r.status_code}")
    return (r.json().get("MediaContainer") or {}).get("Metadata") or []


async def delete_collection(rating_key: str) -> None:
    token = _owner_token()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        await c.delete(f"{_base()}/library/collections/{rating_key}",
                       headers=_headers(token))


async def create_collection(section_key: str, plex_type: int, title: str,
                            keys: list[str]) -> Optional[dict]:
    """Create a dumb collection from item ratingKeys (movies type=1,
    shows/anime type=2 with SERIES keys). Returns the collection metadata
    or None; a 400 refetches the machineIdentifier once (server reinstall).

    Generous timeout: show-collection creates kick off server-side metadata
    work and can exceed the module default (a 20s timeout dropped one of
    four shelves on the first live run)."""
    token = _owner_token()
    mid = await get_machine_identifier()
    if not token or not mid or not keys:
        return None
    async with httpx.AsyncClient(timeout=60.0) as c:
        def params(m):
            uri = (f"server://{m}/com.plexapp.plugins.library"
                   f"/library/metadata/{','.join(keys)}")
            return {"type": str(plex_type), "smart": "0", "title": title,
                    "sectionId": str(section_key), "uri": uri}
        r = await c.post(f"{_base()}/library/collections",
                         headers=_headers(token), params=params(mid))
        if r.status_code == 400:
            mid = await get_machine_identifier(force=True)
            if mid:
                r = await c.post(f"{_base()}/library/collections",
                                 headers=_headers(token), params=params(mid))
    if r.status_code != 200:
        raise RuntimeError(f"collection create HTTP {r.status_code}")
    meta = (r.json().get("MediaContainer") or {}).get("Metadata") or [{}]
    return meta[0]


async def push_collections(designs: list[dict]) -> dict:
    """Find-delete-recreate the whole "Curatarr · " collection set.

    designs: [{section_key, plex_type, title (WITH prefix), keys, description}]
    Cleans ALL sections passed in *plus* any sections named in the previous
    push snapshot, so an emptied category doesn't strand orphans. Snapshot
    lands in app_state "curatarr_collections"."""
    from src.services.app_state import get_state, set_state

    sections = {str(d["section_key"]) for d in designs}
    try:
        prev = json.loads(get_state("curatarr_collections") or "{}")
        sections |= {str(d.get("section_key")) for d in prev.get("designs", [])
                     if d.get("section_key")}
    except Exception:
        pass

    deleted = 0
    for sec in sections:
        try:
            for coll in await list_collections(sec):
                if (coll.get("title") or "").startswith(COLLECTION_PREFIX):
                    await delete_collection(str(coll.get("ratingKey")))
                    deleted += 1
        except Exception as e:
            logger.warning("[collections] cleanup of section %s failed: %s", sec, e)

    created, results = 0, []
    for d in designs:
        try:
            meta = await create_collection(d["section_key"], d["plex_type"],
                                           d["title"], d["keys"])
            if meta:
                created += 1
                results.append({"title": d["title"],
                                "section_key": str(d["section_key"]),
                                "count": len(d["keys"]),
                                "rating_key": meta.get("ratingKey")})
                logger.info("[collections] created %r (%d items)",
                            d["title"], len(d["keys"]))
        except Exception as e:
            logger.warning("[collections] create %r failed: %s", d.get("title"), e)

    set_state("curatarr_collections", json.dumps({
        "pushed_at": datetime.utcnow().isoformat(),
        "designs": results,
    }))
    return {"created": created, "deleted": deleted}
