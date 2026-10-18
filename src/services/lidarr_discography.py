"""
Curatarr — Lidarr discography summary: the artist's ON-DISK structure.

Music debates argued about "the discography" without knowing its shape.
One LAN call to Lidarr's /api/v1/album answers it: how many albums / EPs /
singles, how much disk, and how many releases are merely MONITORED with no
files (the pending Mike WiLL proposal: "Creed II: The Album, 0/15 tracks" —
a ghost entry). Probe results that motivated this (tests via live API):
Dr. Peacock = 64 singles, 36 EPs, 7 albums, 6.2 GB.

The album call is LAN-local (~50 ms) and per-discussion/judged candidate,
and disk state changes with every grab — fresh is correct there. The artist
itself is resolved without the full library list (find_lidarr_artist).
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Optional

import httpx

from src.config import settings

logger = logging.getLogger(__name__)


def plex_discography_summary(artist_mbid: str = None, artist_name: str = None) -> Optional[str]:
    """The same evidence line from the Plex music index when Lidarr is not
    configured (2026-09-15): albums, tracks, footprint, years, lyrics on file."""
    try:
        from src.services.lyrics import plex_artist_lookup, plex_artist_albums
        a = plex_artist_lookup(name=artist_name, mbid=artist_mbid)
        if not a:
            return None
        albums = plex_artist_albums(a["artist_key"])
    except Exception as e:
        logger.debug("[discography] plex index failed for %r: %s", artist_mbid or artist_name, e)
        return None
    if not albums:
        return None
    years = sorted(y for y in (al.get("year") for al in albums) if y)
    line = (f"on disk (Plex): {len(albums)} album{'s' if len(albums) != 1 else ''}, "
            f"{a.get('tracks') or 0} tracks ({(a.get('size_bytes') or 0) / 1e9:.1f} GB)")
    if years:
        line += f", {years[0]}–{years[-1]}" if years[0] != years[-1] else f", {years[0]}"
    if a.get("with_text"):
        line += f"; lyrics on file for {a['with_text']} tracks"
    return line


# One artist by MBID is a small, fast Lidarr query; the full /artist list
# is 15-30 MB and ~2 minutes on a big library — far too slow for a chat turn
# or once per judged candidate, which is where these lookups run.
_ONE_ARTIST_TIMEOUT_S = 15
_FULL_LIST_TIMEOUT_S = 60


def cached_lidarr_artists() -> Optional[list]:
    """The Library Manager's cached Lidarr artist list (refreshed behind
    requests, persisted across restarts), or None when there is none."""
    try:
        from src.routers.library import _LIB_CACHE
        items = (_LIB_CACHE.get("lidarr") or {}).get("items_raw")
    except Exception:  # noqa: BLE001
        return None
    return items if isinstance(items, list) and items else None


def _same_name(a: Optional[str], b: Optional[str]) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


async def find_lidarr_artist(client, base: str, key: str, *, mbid: str = None,
                             name: str = None, same_name=_same_name) -> Optional[dict]:
    """Lidarr's artist object for ``mbid`` (first) or ``name``, without
    downloading the whole library on every call:

    1. the cached library list — no network at all;
    2. Lidarr's ``/api/v1/artist?mbId=`` — one artist, short timeout
       (an artist added since the cache was filled);
    3. the full list only when there is no cached copy at all — the old
       behaviour, for an install whose library was never listed.
    """
    def pick(artists):
        if mbid:
            hit = next((a for a in artists if a.get("foreignArtistId") == mbid), None)
            if hit:
                return hit
        if name:
            return next((a for a in artists if same_name(a.get("artistName"), name)), None)
        return None

    cached = cached_lidarr_artists()
    if cached is not None:
        hit = pick(cached)
        if hit:
            return hit
    if mbid:
        try:
            r = await client.get(f"{base}/api/v1/artist", params={"apikey": key, "mbId": mbid},
                                 timeout=_ONE_ARTIST_TIMEOUT_S)
            if r.status_code == 200:
                data = r.json()
                hit = pick(data if isinstance(data, list) else [data])
                if hit:
                    return hit
        except Exception as e:  # noqa: BLE001
            logger.debug("[lidarr] mbId lookup failed for %s: %s", mbid, e)
    if cached is not None:
        return None
    r = await client.get(f"{base}/api/v1/artist", params={"apikey": key},
                         timeout=_FULL_LIST_TIMEOUT_S)
    if r.status_code != 200:
        return None
    return pick(r.json())


async def discography_summary(artist_mbid: str = None,
                              artist_name: str = None) -> Optional[str]:
    """One evidence line describing the artist's on-disk discography, or
    None when Lidarr is unreachable / the artist isn't in it. Resolves by
    MBID first (robust against typographic-dash names), name second."""
    base = (settings.LIDARR_URL or "").rstrip("/")
    key = settings.LIDARR_API_KEY
    if not (artist_mbid or artist_name):
        return None
    if not base or not key:
        return plex_discography_summary(artist_mbid=artist_mbid, artist_name=artist_name)
    try:
        # The artist comes from the cached library or a one-artist query: the
        # full /artist list (15-30 MB, ~2 min on a big Lidarr) per chat turn
        # and per judged candidate left every music answer waiting on it.
        async with httpx.AsyncClient(timeout=_ONE_ARTIST_TIMEOUT_S) as client:
            artist = await find_lidarr_artist(client, base, key,
                                              mbid=artist_mbid, name=artist_name)
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
    # Derivative composition: Lidarr mirrors MusicBrainz secondaryTypes
    # (Live/Compilation/Remix/Soundtrack…) per release — a discography
    # that is mostly re-issues is a deletion signal the judge never saw.
    secondary = Counter()
    secondary_gb = 0.0
    for al in albums:
        st = al.get("statistics") or {}
        files = st.get("trackFileCount") or 0
        if files:
            on_disk[al.get("albumType") or "Release"] += 1
            gb = (st.get("sizeOnDisk") or 0) / 1e9
            size_gb += gb
            secs = [s for s in (al.get("secondaryTypes") or []) if s]
            if secs:
                # one album counts its GB ONCE, even when dual-tagged
                secondary_gb += gb
                for s in secs:
                    secondary[s] += 1
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
    if secondary:
        parts2 = [f"{n} {t.lower()}" for t, n in secondary.most_common()]
        pct = (secondary_gb / size_gb * 100) if size_gb else 0
        line += (f"; derivative re-issues: {' + '.join(parts2)} = "
                 f"{secondary_gb:.1f} GB ({pct:.0f}% of artist total)")
    return line
