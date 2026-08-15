"""
Curatarr — SoulSync→Lidarr catalog sync ("Modell B", owner decision
2026-08-15).

Division of labour: SoulSync OWNS the music files (naming, moving,
acquisition) and is the primary metadata source; Lidarr runs in catalog
mode (renaming/tagging/indexers disarmed by the owner) as the passive
STRUCTURE index — sizes, paths, internal ids, monitored flags, history,
and the delete write-path Curatarr depends on.

This nightly job keeps the index complete without ever letting Lidarr
fight SoulSync over files:

  1. enumerate the full SoulSync catalogue (page-paginated),
  2. map to Lidarr by MusicBrainz id,
  3. create what's missing DISARMED — monitored=False,
     addOptions.monitor="none", searchForMissingAlbums=False,
     monitorNewItems="none" — so Lidarr indexes the folder but never
     downloads or renames anything,
  4. HEAL stale index entries with surgical RefreshArtist commands
     (capped, fire-and-forget — Lidarr works its command queue serially):
       - freshly added artists (otherwise they sit at 0 files forever:
         scheduled refreshes no longer touch the disk in catalog mode),
       - artists with zero track files where SoulSync knows albums
         (owner case 1: SoulSync downloaded into an artist Lidarr
         indexed as empty — Lidarr would NEVER notice on its own),
       - artists whose SoulSync album count exceeds Lidarr's (grew
         since the last scan),
  5. report what REMAINS mismatched after the healing queue = genuine
     folder-name drift the owner fixes by pointing the artist path at
     the actual SoulSync folder.

Owner case 2 (SoulSync creates a brand-new artist Lidarr never heard
of) is steps 3+4a: next nightly run adds it disarmed AND refreshes it,
so files and sizes appear without Lidarr ever acting on its own. The
per-delete freshness guard (recommendations._lidarr_freshness_guard)
covers the remaining window between runs.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

_ADD_CAP_PER_RUN = 50      # never storm-add half a library in one tick
_REFRESH_CAP_PER_RUN = 15  # each refresh = MB roundtrip + disk scan (~40-120s
                           # serverside, serial) — a nightly budget, not a storm
_STATE_KEY = "music_catalog_sync"


async def sync_soulsync_to_lidarr() -> dict:
    from src.services import soulsync_client
    from src.services.app_state import set_state

    if not soulsync_client._configured():
        logger.info("[catalog-sync] SoulSync not configured — nothing to do")
        return {"ok": True, "skipped": "no_soulsync"}

    # ── 1. full SoulSync catalogue ───────────────────────────────────────
    ss_artists: list = []
    page = 1
    while True:
        body = await soulsync_client.list_artists_page(page=page, limit=200)
        if not body:
            break
        ss_artists.extend(body["artists"])
        if not (body.get("pagination") or {}).get("has_next"):
            break
        page += 1
    if not ss_artists:
        return {"ok": False, "reason": "soulsync_empty_or_unreachable"}

    # ── 2. Lidarr side, mapped by MBID ───────────────────────────────────
    from src.routers.library import _get_arr_url_key, _make_client, _read_defaults
    url, key = _get_arr_url_key("lidarr")
    if not url or not key:
        return {"ok": True, "skipped": "no_lidarr"}
    client = _make_client("lidarr", url, key)
    async with client:
        lidarr_artists = await client.get_artists() or []
        by_mbid = {a.get("foreignArtistId"): a for a in lidarr_artists
                   if a.get("foreignArtistId")}

        # ── 3. create missing, DISARMED ──────────────────────────────────
        defaults = _read_defaults("lidarr")
        added, added_ids, add_failed, no_mbid = [], [], 0, 0
        can_add = bool(defaults.get("root_folder_path")
                       and defaults.get("quality_profile_id")
                       and defaults.get("metadata_profile_id"))
        for a in ss_artists:
            mbid = a.get("musicbrainz_id")
            name = a.get("name")
            if not mbid:
                no_mbid += 1
                continue
            if mbid in by_mbid or not name:
                continue
            if not can_add:
                continue
            if len(added) >= _ADD_CAP_PER_RUN:
                break
            try:
                created = await client.add_artist(
                    mbid=mbid, artist_name=name,
                    root_folder_path=defaults["root_folder_path"],
                    quality_profile_id=defaults["quality_profile_id"],
                    metadata_profile_id=defaults["metadata_profile_id"],
                    monitored=False,
                    monitor_option="none",
                    search_for_missing=False,   # THE footgun: without this
                    monitor_new_items="none",   # Lidarr starts hunting albums
                )
                added.append(name)
                if isinstance(created, dict) and created.get("id"):
                    added_ids.append(int(created["id"]))
            except Exception as e:
                add_failed += 1
                logger.debug("[catalog-sync] add %r failed: %s", name, e)

        # ── 4. HEAL stale entries with surgical refreshes ────────────────
        # In catalog mode Lidarr NEVER rescans on its own (owner set
        # rescan = After Manual Refresh) — so a SoulSync download into a
        # 0-byte artist, or new albums under a known artist, stay
        # invisible until WE queue a refresh. Fire-and-forget: Lidarr
        # drains its command queue serially; next night's run verifies.
        ss_albums_by_mbid = {a.get("musicbrainz_id"): a.get("album_count") or 0
                             for a in ss_artists if a.get("musicbrainz_id")}
        refresh_ids = list(added_ids)               # new artists scan first
        stale_named, mismatches = [], []
        for mbid, a in by_mbid.items():
            stats = a.get("statistics") or {}
            files = stats.get("trackFileCount") or 0
            ss_albums = ss_albums_by_mbid.get(mbid, 0)
            if files == 0 and ss_albums > 0:
                # owner case 1: SoulSync filled a folder Lidarr indexed
                # as empty — refresh; whatever STAYS empty next run is
                # genuine folder-name drift for the report.
                mismatches.append(a.get("artistName"))
                if a.get("id"):
                    refresh_ids.append(int(a["id"]))
            elif ss_albums > (stats.get("albumCount") or 0) > 0:
                # grew since the last scan (never shrink-triggered:
                # the two sides count album types differently)
                stale_named.append(a.get("artistName"))
                if a.get("id"):
                    refresh_ids.append(int(a["id"]))
        refreshed = 0
        for aid in refresh_ids[:_REFRESH_CAP_PER_RUN]:
            try:
                await client.refresh_artist(aid)
                refreshed += 1
            except Exception as e:
                logger.debug("[catalog-sync] refresh %s failed: %s", aid, e)

    result = {
        "ok": True,
        "soulsync_artists": len(ss_artists),
        "lidarr_artists": len(lidarr_artists),
        "added_disarmed": len(added),
        "add_failed": add_failed,
        "no_mbid": no_mbid,
        "refreshes_queued": refreshed,
        "refresh_backlog": max(0, len(refresh_ids) - _REFRESH_CAP_PER_RUN),
        "grown_artists": sorted(s for s in stale_named if s)[:20],
        "path_mismatches": sorted(m for m in mismatches if m)[:40],
        "at": datetime.utcnow().isoformat(),
    }
    set_state(_STATE_KEY, json.dumps(result))
    if mismatches:
        logger.warning("[catalog-sync] %d artists indexed but FILE-LESS while "
                       "SoulSync knows albums — folder-name drift; fix the "
                       "artist Path in Lidarr (first: %s)",
                       len(mismatches), ", ".join(result["path_mismatches"][:5]))
    logger.info("[catalog-sync] soulsync=%d lidarr=%d added=%d refreshed=%d "
                "(backlog %d) grown=%d mismatches=%d",
                len(ss_artists), len(lidarr_artists), len(added), refreshed,
                result["refresh_backlog"], len(stale_named), len(mismatches))
    return result
