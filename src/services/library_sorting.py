"""
Curatarr - Library sorting (anime ↔ TV reclassification).

Detects Sonarr series filed in the wrong library and proposes a move:

  * **Western cartoons in the Anime library** → TV Shows
    (Futurama, Rick and Morty, Avatar: TLA, the X-Men/Star Wars cartoons, …)
  * **Asian anime in the TV library** → Anime  (the reverse, two-way)

Heuristic — a series counts as *anime* when EITHER:
  * its ``tvdbId`` is in the anime-lists AniDB mapping (``tvdb_to_anidb``), OR
  * its TMDB origin country is Asian (JP/CN/KR/TW/HK).

So a series in the Anime library that is in neither bucket — not on AniDB AND
Western-origin — is the move-out target. The mapping alone is too noisy (it
misses old/OVA Japanese titles), which is why the TMDB origin is the tie-break
that keeps real anime put.

This module is **read-only** (the scan / dry-run). The actual Sonarr move
(`PUT seriesType`, root folder, ``moveFiles=true``) lives in the router so the
write path stays explicit and admin-gated.
"""

import asyncio
import logging
from typing import Optional

import httpx

from src.config import settings
from src.services.anime_mapping import get_anime_mapping
from src.services.app_state import get_state, set_state

logger = logging.getLogger(__name__)

ASIAN_ORIGINS = {"JP", "CN", "KR", "TW", "HK"}


def _poster_url(series: dict) -> Optional[str]:
    """First poster image URL from a Sonarr series object."""
    for im in series.get("images", []) or []:
        if im.get("coverType") == "poster":
            return im.get("remoteUrl") or im.get("url")
    return None


def _pick_roots(series: list, rootfolders: list) -> tuple[Optional[str], Optional[str]]:
    """Return (anime_root_path, tv_root_path).

    Decided by where each ``seriesType`` actually lives: the root holding the
    most ``anime`` series is the anime root; the most ``standard`` is the TV
    root. A path-name hint ("anime") breaks ties so a brand-new / empty setup
    still resolves sensibly.
    """
    paths = [rf.get("path") for rf in rootfolders if rf.get("path")]
    anime_count = {p: 0 for p in paths}
    std_count = {p: 0 for p in paths}
    for s in series:
        p = s.get("rootFolderPath")
        if p not in anime_count:
            continue
        if s.get("seriesType") == "anime":
            anime_count[p] += 1
        else:
            std_count[p] += 1

    anime_root = max(paths, key=lambda p: (anime_count[p], "anime" in p.lower()), default=None)
    tv_candidates = [p for p in paths if p != anime_root] or paths
    tv_root = max(tv_candidates, key=lambda p: (std_count[p], "tv" in p.lower() or "show" in p.lower()),
                  default=None)
    return anime_root, tv_root


async def _tmdb_origin(client: httpx.AsyncClient, tvdb_id: int) -> list:
    """origin_country list for a tvdbId via TMDB ``/find``. Cached in app_state
    (``tvdb_origin:<id>``) so repeat scans don't re-hit TMDB."""
    ck = f"tvdb_origin:{tvdb_id}"
    cached = get_state(ck)
    if cached is not None:
        return [c for c in cached.split(",") if c]
    origin: list = []
    try:
        r = await client.get(
            f"https://api.themoviedb.org/3/find/{tvdb_id}",
            params={"external_source": "tvdb_id", "api_key": settings.TMDB_API_KEY},
        )
        if r.status_code == 200:
            res = r.json().get("tv_results") or []
            if res:
                origin = res[0].get("origin_country") or []
    except Exception as e:
        logger.debug("[lib-sort] TMDB origin lookup failed for tvdb %s: %s", tvdb_id, e)
    set_state(ck, ",".join(origin))
    return origin


def _card(series: dict, sonarr_url: str, cur_lib: str, cur_root: str,
          tgt_lib: str, tgt_root: str, tgt_type: str, reason: str, origin: list) -> dict:
    slug = series.get("titleSlug")
    cur_root = series.get("rootFolderPath") or cur_root
    return {
        "sonarr_id":   series.get("id"),
        "title":       series.get("title"),
        "title_slug":  slug,
        "tvdb_id":     series.get("tvdbId"),
        "poster":      _poster_url(series),
        "sonarr_link": f"{sonarr_url.rstrip('/')}/series/{slug}" if slug else None,
        "origin":      origin,
        "reason":      reason,
        "current":     {"library": cur_lib, "root": cur_root, "series_type": series.get("seriesType")},
        "target":      {"library": tgt_lib, "root": tgt_root, "series_type": tgt_type,
                        "move_files": (cur_root != tgt_root)},
    }


async def scan_misclassified() -> dict:
    """Read-only scan. Returns both directions + the resolved root folders.

    ``western_in_anime``: anime-library series that are not on AniDB and have a
    Western TMDB origin → propose move to TV.
    ``anime_in_tv``: TV-library series whose tvdbId IS on AniDB → propose move
    to Anime.
    """
    sonarr = settings.effective_sonarr_url
    key = settings.SONARR_API_KEY
    if not sonarr or not key:
        return {"error": "Sonarr not configured"}

    mapping = await get_anime_mapping()
    headers = {"X-Api-Key": key}
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            series = (await client.get(f"{sonarr}/api/v3/series", headers=headers)).json()
            rootfolders = (await client.get(f"{sonarr}/api/v3/rootfolder", headers=headers)).json()
        except Exception as e:
            logger.warning("[lib-sort] Sonarr fetch failed: %s", e)
            return {"error": "Sonarr unreachable"}

        anime_root, tv_root = _pick_roots(series, rootfolders)

        western_in_anime: list = []
        uncertain: list = []     # anime-library, not on AniDB, no TMDB origin → review
        anime_in_tv: list = []

        for s in series:
            tvdb = s.get("tvdbId")
            on_anidb = bool(tvdb and tvdb in mapping.tvdb_to_anidb)
            stype = s.get("seriesType")

            if stype == "anime":
                if on_anidb:
                    continue  # genuine anime — keep
                origin = await _tmdb_origin(client, tvdb) if tvdb else []
                if origin and (set(origin) & ASIAN_ORIGINS):
                    continue  # Asian animation missing from the mapping — keep
                # Not on AniDB + not Asian-origin → Western cartoon mis-filed.
                # When TMDB had no origin at all we can't be sure (often old
                # JP OVAs missing from both DBs) — surface those separately so
                # they don't get bulk-moved by mistake.
                if origin:
                    western_in_anime.append(
                        _card(s, sonarr, "Anime", anime_root, "TV Shows", tv_root, "standard",
                              f"Not on AniDB · TMDB origin {','.join(origin)}", origin))
                else:
                    uncertain.append(
                        _card(s, sonarr, "Anime", anime_root, "TV Shows", tv_root, "standard",
                              "Not on AniDB · no TMDB origin — verify before moving", origin))

            elif stype in ("standard", "daily"):
                if on_anidb:
                    # An AniDB title living in the TV library → it's anime.
                    anime_in_tv.append(
                        _card(s, sonarr, "TV Shows", tv_root, "Anime", anime_root, "anime",
                              "On AniDB but filed as a standard series", []))

    for lst in (western_in_anime, uncertain, anime_in_tv):
        lst.sort(key=lambda c: c["title"].lower())
    return {
        "anime_root": anime_root,
        "tv_root": tv_root,
        "western_in_anime": western_in_anime,
        "uncertain": uncertain,
        "anime_in_tv": anime_in_tv,
        "counts": {
            "to_tv": len(western_in_anime),
            "uncertain": len(uncertain),
            "to_anime": len(anime_in_tv),
        },
    }
