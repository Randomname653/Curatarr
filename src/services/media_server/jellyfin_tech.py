"""The library walk on Jellyfin (docs/JELLYFIN_DESIGN.md §6.3, phase 4).

The counterpart of ``plex_sync.sync_tech_profiles``: one walk of the mapped
libraries fills the tables everything downstream reads, in the same shape
the Plex walk writes them:

- ``MediaTechProfile`` - one row per film and per series (episodes summed):
  size, runtime, resolution, codec, HDR, languages, versions. Recommendations
  and the collection designer take their server keys from here, so without
  this walk a Jellyfin install had nothing to put in a playlist;
- the library ledger (``library_ledger.persist_ledger``) - arrivals by
  ``DateCreated``, titles that left;
- the id memory (``server_id_map``) a server switch runs on.

Stale profiles are pruned only for categories whose walk finished: an
adapter walk raises on any failed page, so a category is either complete or
not counted at all.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

from src.services.media_server.base import normalize_jellyfin_id

logger = logging.getLogger(__name__)

_KINDS = {"movie": ("movie",), "show": ("series", "episode"), "anime": ("series", "episode"),
          "music": ("track",)}
_HDR_RANGES = {"hdr", "hdr10", "hdr10plus", "dovi", "dolbyvision", "hlg"}


def _res_bucket(height) -> Optional[str]:
    try:
        h = int(height or 0)
    except (TypeError, ValueError):
        return None
    if h <= 0:
        return None
    if h >= 1800:
        return "4k"
    if h >= 900:
        return "1080"
    if h >= 650:
        return "720"
    return "sd"


def media_of(raw: dict) -> dict:
    """``plex_sync._parse_media``'s answer from a BaseItemDto's MediaSources:
    total size of every version, codec / resolution / HDR / bitrate basis
    from the largest one."""
    sources = raw.get("MediaSources") or []
    duration_ms = int(raw.get("RunTimeTicks") or 0) // 10_000
    if not sources:
        return {"size_bytes": 0, "primary_bytes": 0, "duration_ms": duration_ms, "resolution": None,
                "codec": None, "hdr": False, "audio": set(), "subs": set(), "versions": 0, "remux": False}
    primary = max(sources, key=lambda s: int(s.get("Size") or 0))
    total = sum(int(s.get("Size") or 0) for s in sources)
    video = next((st for st in primary.get("MediaStreams") or [] if st.get("Type") == "Video"), {})
    rng = {str(video.get("VideoRange") or "").lower(), str(video.get("VideoRangeType") or "").lower()}
    streams = primary.get("MediaStreams") or []
    return {
        "size_bytes": total, "primary_bytes": int(primary.get("Size") or 0), "duration_ms": duration_ms,
        "resolution": _res_bucket(video.get("Height")),
        "codec": (str(video.get("Codec") or "").lower() or None),
        "hdr": bool(rng & _HDR_RANGES),
        "audio": {st["Language"] for st in streams if st.get("Type") == "Audio" and st.get("Language")},
        "subs": {st["Language"] for st in streams if st.get("Type") == "Subtitle" and st.get("Language")},
        "versions": len(sources), "remux": "remux" in str(primary.get("Path") or "").lower(),
    }


def versions_of(raw: dict) -> list:
    """Each version's file, as the redundancy panel lists Plex's Media[]:
    ``{"resolution", "size_gb", "file"}`` (the file's name, not its path)."""
    out = []
    for src in raw.get("MediaSources") or []:
        video = next((st for st in src.get("MediaStreams") or [] if st.get("Type") == "Video"), {})
        path = str(src.get("Path") or "")
        out.append({"resolution": _res_bucket(video.get("Height")),
                    "size_gb": round(int(src.get("Size") or 0) / 1024 ** 3, 1),
                    "file": path.replace("\\", "/").rsplit("/", 1)[-1]})
    return out


def _unix(iso) -> Optional[int]:
    from src.services.media_server.jellyfin_adapter import _ts
    dt = _ts(iso)
    return int(dt.replace(tzinfo=timezone.utc).timestamp()) if dt else None


def aggregate(category: str, raws: list, agg: dict) -> None:
    """Fold one library's walk into the Plex walk's accumulator shape."""
    from src.services.library_ledger import note_arrival
    series = {}
    for raw in raws:
        t = raw.get("Type")
        if t == "Series":
            series[normalize_jellyfin_id(raw.get("Id"))] = raw
            continue
        if t == "Movie" or t == "Audio":
            ck, title = normalize_jellyfin_id(raw.get("Id")), raw.get("Name")
            ids = raw.get("ProviderIds") or {}
            tmdb, tvdb = ids.get("Tmdb"), None
        elif t == "Episode":
            ck, title, tmdb, tvdb = normalize_jellyfin_id(raw.get("SeriesId")), raw.get("SeriesName"), None, None
        else:
            continue
        if not ck:
            continue
        a = agg.setdefault(ck, {
            "rating_key": ck, "media_type": category, "title": title,
            "tmdb_id": int(tmdb) if str(tmdb or "").isdigit() else None,
            "tvdb_id": int(tvdb) if str(tvdb or "").isdigit() else None,
            "size_bytes": 0, "primary_bytes": 0, "redundant_bytes": 0, "duration_ms": 0,
            "res": Counter(), "codec": Counter(), "hdr": False, "audio": set(), "subs": set(),
            "count": 0, "versions": 1, "remux_files": 0})
        m = media_of(raw)
        note_arrival(a.setdefault("arrivals", {}), _unix(raw.get("DateCreated")), m["size_bytes"])
        a["size_bytes"] += m["size_bytes"]
        a["primary_bytes"] += m["primary_bytes"]
        a["redundant_bytes"] += m["size_bytes"] - m["primary_bytes"]
        a["versions"] = max(a["versions"], m["versions"])
        a["duration_ms"] += m["duration_ms"]
        if m["resolution"]:
            a["res"][m["resolution"]] += 1
        if m["codec"]:
            a["codec"][m["codec"]] += 1
        a["hdr"] = a["hdr"] or m["hdr"]
        a["audio"] |= m["audio"]
        a["subs"] |= m["subs"]
        a["count"] += 1
        if m["remux"]:
            a["remux_files"] += 1
    for sk, raw in series.items():          # series ids and title onto their episodes' row
        if sk in agg:
            ids = raw.get("ProviderIds") or {}
            for col, key in (("tmdb_id", "Tmdb"), ("tvdb_id", "Tvdb")):
                if not agg[sk][col] and str(ids.get(key) or "").isdigit():
                    agg[sk][col] = int(ids[key])
            agg[sk]["title"] = agg[sk]["title"] or raw.get("Name")


async def sync_tech_profiles(force: bool = False, include_music: bool = False, *, adapter=None) -> dict:
    """Twice a day at most, like the Plex walk (``last_tech_sync_at``)."""
    from src.database.connection import get_db_session
    from src.database.models import LibraryConfig
    from src.database.runner import run_read, run_write
    from src.services.app_state import get_datetime, set_datetime
    from src.services.library_ledger import persist_ledger
    from src.services.media_server import get_adapter, id_map
    from src.services.media_server.jellyfin_adapter import to_media_item
    from src.services.plex_sync import _persist_tech_profiles, _prune_stale_tech_profiles

    last = await run_read(get_datetime, "last_tech_sync_at")
    if not force and last and (datetime.utcnow() - last).total_seconds() < 12 * 3600:
        return {"skipped": True, "last": last.isoformat()}
    adapter = adapter or get_adapter()

    def _configs() -> dict:
        with get_db_session() as db:
            return {c.plex_section_key: c.media_category for c in db.query(LibraryConfig).all()}

    configs = await run_read(_configs)
    agg, scanned, id_items = {}, set(), []
    for lib in await adapter.libraries():
        cat = configs.get(lib.key)
        if cat not in _KINDS or (cat == "music" and not include_music):
            continue
        raws = [r async for r in adapter.raw_items(lib, kinds=_KINDS[cat])]
        aggregate(cat, raws, agg)
        id_items += [it for it in (to_media_item(r, lib.key) for r in raws) if it]
        if raws:
            scanned.add(cat)
    written = await run_write(_persist_tech_profiles, agg)
    try:
        await run_write(persist_ledger, agg, scanned, None, None)
    except Exception as e:  # noqa: BLE001 - the profiles stand; the next walk catches the ledger up
        logger.warning("[jellyfin] library ledger not written this walk (%s)", e)
    pruned = await run_write(_prune_stale_tech_profiles, set(agg), scanned)
    try:
        ids = await id_map.record_walk("jellyfin", id_items, adapter=adapter)
    except Exception as e:  # noqa: BLE001 - the profiles stand without it; the next walk files the ids
        logger.warning("[jellyfin] server ids not filed this walk (%s)", e)
        ids = None
    await run_write(set_datetime, "last_tech_sync_at", datetime.utcnow())
    logger.info("[jellyfin] tech walk: %d profiles, %d pruned, ids %s", written, pruned, ids)
    return {"profiles": written, "items": len(agg), "pruned": pruned, "ids": ids}
