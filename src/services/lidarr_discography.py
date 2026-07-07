"""
Curatarr — Lidarr discography summary: the artist's ON-DISK structure.

Music debates argued about "the discography" without knowing its shape.
One LAN call to Lidarr's /api/v1/album answers it: how many albums / EPs /
singles, how much disk, and how many releases are merely MONITORED with no
files (the pending Mike WiLL proposal: "Creed II: The Album, 0/15 tracks" —
a ghost entry). Probe results that motivated this (tests via live API):
Dr. Peacock = 64 singles, 36 EPs, 7 albums, 6.2 GB.

No cache: the Lidarr call is LAN-local (~50 ms) and per-discussion/judged
candidate, and disk state changes with every grab — fresh is correct here.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Optional

import httpx

from src.config import settings

logger = logging.getLogger(__name__)


async def discography_summary(artist_mbid: str = None,
                              artist_name: str = None) -> Optional[str]:
    """One evidence line describing the artist's on-disk discography, or
    None when Lidarr is unreachable / the artist isn't in it. Resolves by
    MBID first (robust against typographic-dash names), name second."""
    base = (settings.LIDARR_URL or "").rstrip("/")
    key = settings.LIDARR_API_KEY
    if not base or not key or not (artist_mbid or artist_name):
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{base}/api/v1/artist", params={"apikey": key})
            if r.status_code != 200:
                return None
            artists = r.json()
            artist = None
            if artist_mbid:
                artist = next((a for a in artists
                               if a.get("foreignArtistId") == artist_mbid), None)
            if artist is None and artist_name:
                low = artist_name.strip().lower()
                artist = next((a for a in artists
                               if (a.get("artistName") or "").strip().lower() == low), None)
            if artist is None:
                return None
            r2 = await client.get(f"{base}/api/v1/album",
                                  params={"apikey": key, "artistId": artist["id"]})
            if r2.status_code != 200:
                return None
            albums = r2.json()
    except Exception as e:
        logger.debug("[discography] lidarr fetch failed for %r: %s",
                     artist_mbid or artist_name, e)
        return None

    on_disk = Counter()
    ghosts = 0            # monitored releases with zero files on disk
    size_gb = 0.0
    for al in albums:
        st = al.get("statistics") or {}
        files = st.get("trackFileCount") or 0
        if files:
            on_disk[al.get("albumType") or "Release"] += 1
            size_gb += (st.get("sizeOnDisk") or 0) / 1e9
        elif al.get("monitored"):
            ghosts += 1
    if not on_disk and not ghosts:
        return None
    parts = [f"{n} {t.lower()}{'s' if n != 1 else ''}"
             for t, n in on_disk.most_common()]
    line = (f"on disk: {', '.join(parts) if parts else 'nothing'}"
            f" ({size_gb:.1f} GB)")
    if ghosts:
        line += f"; {ghosts} monitored release(s) with NO files"
    return line
