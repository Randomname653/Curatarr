"""
Curatarr — curator-designed COLLECTIONS inside Plex (transport layer).

Sibling of plex_playlists.py: together they are the ONLY Plex-write modules.
Collections differ from playlists in two load-bearing ways, both probe-
verified against the live server (scripts/dev/probe_plex_collections.py,
2026-08-13):

- they are SECTION-scoped and visible to EVERY account → this is household
  curation, pushed once with the OWNER token (no per-user sets). As home
  rows Plex knows the owner's home and "shared" (every other account), no
  per-account choice (Manage Recommendations, COLLECTIONS_ON_HOME);
- a series ratingKey stays a series (childCount counts shows, no episode
  explosion), and deleting a collection (PlexAdapter.remove_collection) removes ONLY the
  collection — the media items stay untouched.

The server also runs Kometa with hundreds of its own collections, so every
mutating operation here touches EXCLUSIVELY collections whose title starts
with COLLECTION_PREFIX.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

COLLECTION_PREFIX = "Curatarr · "


async def push_collections(designs: list[dict], *, adapter=None) -> dict:
    """The designer's shelves into Plex, each library's shelves edited in
    place.

    designs: [{section_key, plex_type, title (WITH prefix), keys, description}]

    The designer names every rotation anew, so a shelf is matched by its
    SLOT, not its title: a library's Curatarr collections, oldest first,
    are renamed, re-described and re-filled by the new designs in order.
    The collection objects (and their rows on the home screens) stay; only
    a library with fewer designs than before loses its surplus shelves, and
    one with more gains new ones. Nothing is deleted before the new shelves
    are written, so a failed push leaves last week's in place. Sections
    named in the previous snapshot are cleaned too, so an emptied category
    strands no shelves. Snapshot in app_state "curatarr_collections"."""
    from src.config import settings
    from src.database.runner import run_read, run_write
    from src.services.app_state import get_state, set_state
    from src.services.media_server import active_kind, get_adapter
    from src.services.media_server.base import Library
    if active_kind() == "jellyfin":
        from src.services.media_server import jellyfin_writes
        return await jellyfin_writes.push_collections(designs)
    adapter = adapter or get_adapter()

    sections = {str(d["section_key"]) for d in designs}
    try:
        prev = json.loads(await run_read(get_state, "curatarr_collections") or "{}")
        sections |= {str(d.get("section_key")) for d in prev.get("designs", [])
                     if d.get("section_key")}
    except (ValueError, TypeError, AttributeError):
        pass   # no usable snapshot: the sections passed in only
    except Exception as e:
        logger.warning("[collections] previous push snapshot unreadable - "
                       "orphans in other sections may stay: %s", e)

    home = (settings.COLLECTIONS_ON_HOME or "all").strip().lower()
    flags = {"own_home": home in ("all", "owner"), "shared_home": home == "all",
             "recommended": home != "off"}
    created = edited = failed = deleted = 0
    results = []
    for sec in sorted(sections):
        mine = [d for d in designs if str(d["section_key"]) == sec]
        try:
            slots = await adapter.collections(COLLECTION_PREFIX, library_key=sec)
        except Exception as e:  # noqa: BLE001 - this library's shelves stay as they were; the others go on; logged
            logger.warning("[collections] shelves of section %s not read: %s", sec, e)
            failed += len(mine)
            continue
        kind = "show" if any(int(d.get("plex_type") or 1) == 2 for d in mine) else "movie"
        lib = Library(key=sec, title="", kind=kind)
        for i, d in enumerate(mine):
            reuse = slots[i]["key"] if i < len(slots) else None
            try:
                key = await adapter.write_collection(lib, d["title"], d["keys"],
                                                     summary=d.get("description") or "", reuse_key=reuse)
            except Exception as e:  # noqa: BLE001 - one shelf's failure must not cost the others; counted
                failed += 1
                logger.warning("[collections] %r not written: %s", d.get("title"), e)
                continue
            if reuse:
                edited += 1
            else:
                created += 1
            try:
                await adapter.show_collection(sec, key, **flags)
            except Exception as e:  # noqa: BLE001 - the shelf exists; only its home row is missing; logged
                logger.warning("[collections] %r written, its home-screen row not set: %s", d.get("title"), e)
            results.append({"title": d["title"], "section_key": sec,
                            "count": len(d["keys"]), "rating_key": key})
            logger.info("[collections] %s %r (%d items)", "edited" if reuse else "created",
                        d["title"], len(d["keys"]))
        if len([r for r in results if r["section_key"] == sec]) == len(mine):
            for extra in slots[len(mine):]:
                try:
                    await adapter.remove_collection(extra["key"])
                    deleted += 1
                except Exception as e:  # noqa: BLE001 - a surplus shelf stays one more week; logged
                    logger.warning("[collections] surplus shelf %r not removed: %s", extra["title"], e)

    await run_write(set_state, "curatarr_collections", json.dumps({
        "pushed_at": datetime.utcnow().isoformat(),
        "designs": results,
    }))
    return {"created": created + edited, "new": created, "edited": edited,
            "deleted": deleted, "failed": failed}
