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

import asyncio
import json
import logging
import re
import sqlite3
from typing import Optional

import httpx

from src.config import settings
from src.services.http_client import tls_context

logger = logging.getLogger(__name__)

_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—−"})


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower().translate(_DASHES)).strip()


# (stamp, {normalized title: series id}) for the arr_library:sonarr row the
# index was built from. Built in a worker thread and swapped in whole, never
# mutated, so concurrent lookups only ever see a complete index.
_SERIES_ID_MEMO: tuple = (None, {})


def _sonarr_row_stamp(mc) -> Optional[tuple]:
    """Identity of the live arr_library:sonarr row (the one get_cache would
    serve: versioned first, then v1) without decoding it. Every rewrite —
    set_cache and patch_cache alike — moves expires_at."""
    from datetime import datetime
    from src.cache.metadata_cache import _CACHE_VERSION
    key = "arr_library:sonarr"
    versioned = f"{_CACHE_VERSION}:{key}"
    rows = {r[0]: r for r in mc.conn.execute(
        "SELECT cache_key, expires_at, length(response) FROM api_cache "
        "WHERE cache_key IN (?, ?) AND expires_at > ?",
        (versioned, key, datetime.now().isoformat())).fetchall()}
    row = rows.get(versioned) or rows.get(key)
    return (str(mc.cache_path),) + tuple(row) if row else None


def _sonarr_series_id(series_title: str) -> Optional[int]:
    """Resolve via the warm arr_library cache — no Sonarr round-trip.
    Sync (cache file reads + a full blob decode on a miss): async callers run
    it in a thread. The title index is memoized on the row's stamp, so the
    per-judged-show repeats skip the decode and the normalize-every-title
    pass."""
    global _SERIES_ID_MEMO
    try:
        from src.cache.metadata_cache import MetadataCache
        mc = MetadataCache()
        try:
            stamp = _sonarr_row_stamp(mc)
            if stamp is None:
                return None
            memo_stamp, index = _SERIES_ID_MEMO
            if memo_stamp != stamp:
                hit = mc.get_cache("arr_library:sonarr")
                resp = (hit or {}).get("response")
                rows = (resp if isinstance(resp, list)
                        else (resp or {}).get("items_raw") or [])
                index = {}
                for it in rows:
                    if isinstance(it, dict):
                        # first match wins, as the old linear scan did
                        index.setdefault(_norm(it.get("title")), it.get("id"))
                _SERIES_ID_MEMO = (stamp, index)
        finally:
            mc.close()
        return index.get(_norm(series_title))
    except Exception as e:
        logger.warning("[episode-ctx] series id lookup failed: %s", e)
    return None


async def _fetch_episodes(series_title: str) -> Optional[list]:
    """All Sonarr episode rows for a series (one LAN call), or None."""
    base = (settings.SONARR_URL or "").rstrip("/")
    key = settings.SONARR_API_KEY
    if not base or not key:
        return None
    sid = await asyncio.to_thread(_sonarr_series_id, series_title)
    if not sid:
        return None
    try:
        async with httpx.AsyncClient(verify=tls_context(), timeout=10) as client:
            r = await client.get(f"{base}/api/v3/episode",
                                 params={"seriesId": sid, "apikey": key})
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        from src.services.enrichment_state import log_topup_failure
        log_topup_failure(logger, "[episode-ctx]", series_title, e)
        return None


async def series_availability(series_title: str) -> Optional[str]:
    """The series' stock truth in one line: how many episodes exist, how
    many have aired, how many are monitored, how many sit on disk — and
    whether any MONITORED, AIRED episode is missing (named, up to three).
    None when the series isn't in Sonarr."""
    eps = await _fetch_episodes(series_title)
    if not eps:
        return None
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    regular = [e for e in eps if (e.get("seasonNumber") or 0) >= 1]
    total = len(regular)
    aired = [e for e in regular if (e.get("airDateUtc") or "9999") <= now]
    monitored = [e for e in regular if e.get("monitored")]
    on_disk = [e for e in regular if e.get("hasFile")]
    missing = [e for e in monitored
               if not e.get("hasFile") and (e.get("airDateUtc") or "9999") <= now]
    line = (f"{total} episodes known, {len(aired)} aired, "
            f"{len(monitored)} monitored, {len(on_disk)} on disk")
    if missing:
        names = ", ".join(f"S{e['seasonNumber']}E{e['episodeNumber']}"
                          for e in missing[:3])
        more = f" (+{len(missing) - 3} more)" if len(missing) > 3 else ""
        line += f" — {len(missing)} monitored & aired but MISSING: {names}{more}"
    else:
        line += " — all monitored, aired episodes are on disk"
    return line


async def stop_point_context(series_title: str, season: int,
                             episode: int) -> Optional[str]:
    """Two-line context around the user's stop point, or None (not a Sonarr
    series / API down / episode unknown)."""
    eps = await _fetch_episodes(series_title)
    if not eps:
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
