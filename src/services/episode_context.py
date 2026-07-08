"""
Curatarr — stop-point episode context (Sonarr, LAN, on demand).

The viewing pattern says WHERE the user stopped (S1E9); this says WHAT that
means: the title/synopsis of the episode they stopped at and the one that
awaits them. "You stopped at S1E9 'A Once in a Lifetime Chance' — next up
'I Want to Know More About You'" turns bare coordinates into conversation.

One LAN call per discussion (~50 ms), no cache — Sonarr is the live truth
for episode lists (airing shows grow), same reasoning as the Lidarr
discography line.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Optional

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—−"})


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower().translate(_DASHES)).strip()


def _sonarr_series_id(series_title: str) -> Optional[int]:
    """Resolve via the warm arr_library cache — no Sonarr round-trip."""
    try:
        from src.cache.metadata_cache import MetadataCache
        mc = MetadataCache()
        try:
            hit = mc.get_cache("arr_library:sonarr")
        finally:
            mc.close()
        resp = (hit or {}).get("response")
        rows = (resp if isinstance(resp, list)
                else (resp or {}).get("items_raw") or [])
        want = _norm(series_title)
        for it in rows:
            if isinstance(it, dict) and _norm(it.get("title")) == want:
                return it.get("id")
    except Exception as e:
        logger.debug("[episode-ctx] series id lookup failed: %s", e)
    return None


async def stop_point_context(series_title: str, season: int,
                             episode: int) -> Optional[str]:
    """Two-line context around the user's stop point, or None (not a Sonarr
    series / API down / episode unknown)."""
    base = (settings.SONARR_URL or "").rstrip("/")
    key = settings.SONARR_API_KEY
    if not base or not key:
        return None
    sid = _sonarr_series_id(series_title)
    if not sid:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{base}/api/v3/episode",
                                 params={"seriesId": sid, "apikey": key})
        if r.status_code != 200:
            return None
        eps = r.json()
    except Exception as e:
        logger.debug("[episode-ctx] fetch failed for %r: %s", series_title, e)
        return None

    def _find(s: int, e: int) -> Optional[dict]:
        return next((x for x in eps
                     if x.get("seasonNumber") == s and x.get("episodeNumber") == e),
                    None)

    def _fmt(x: dict) -> str:
        ov = re.sub(r"\s+", " ", (x.get("overview") or "").strip())
        ov = (ov[:170].rsplit(" ", 1)[0] + "…") if len(ov) > 170 else ov
        line = f"S{x['seasonNumber']}E{x['episodeNumber']} '{x.get('title') or '?'}'"
        return f"{line} — {ov}" if ov else line

    cur = _find(season, episode)
    if cur is None:
        return None
    # next episode: same season +1, else next season E1
    nxt = _find(season, episode + 1) or _find(season + 1, 1)
    out = f"stopped at {_fmt(cur)}"
    if nxt:
        out += f"\nNext up: {_fmt(nxt)}"
        if not nxt.get("hasFile"):
            out += " (episode NOT on disk)"
    return out
