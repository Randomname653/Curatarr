"""
Curatarr — SoulSync client (read-only LAN neighbour).

SoulSync (github.com/Nezreka/SoulSync) runs on the owner's server and
aggregates music metadata through 10 background enrichment workers
(MusicBrainz, Deezer, iTunes, Qobuz, AudioDB, Genius, Last.fm, Spotify, …).
Its /api/v1/library endpoints expose that per-artist and per-album: genres,
last.fm tags/bio/similar, mood/style/label and every external id.

Curatarr CONSUMES this as an extra metadata source — deliberately read-only:
we never call /api/v1/request (download trigger); acquisition stays the
owner's / SoulSync's domain. Every helper degrades to None when SoulSync is
unconfigured or unreachable, so callers can treat it as a best-effort bonus.

Field notes (probed live 2026-07-11): ``genres`` arrives as a list of
JSON-ENCODED strings (['["Electronic"]']) — _norm_genres flattens that.
The instance is young; lastfm_* fields fill in as the workers progress.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0


def _configured() -> bool:
    return bool(settings.SOULSYNC_URL and settings.SOULSYNC_API_KEY)


def _base() -> str:
    return (settings.SOULSYNC_URL or "").rstrip("/") + "/api/v1"


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.SOULSYNC_API_KEY}"}


_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—−"})


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower().translate(_DASHES)).strip()


def _norm_genres(raw) -> list[str]:
    """Flatten SoulSync's genre field: a list whose elements may themselves be
    JSON-encoded arrays ('["Electronic"]') or plain strings."""
    out: list[str] = []
    for g in (raw or []):
        if isinstance(g, str) and g.startswith("["):
            try:
                out.extend(x for x in json.loads(g) if isinstance(x, str))
                continue
            except Exception:
                pass
        if isinstance(g, str) and g.strip():
            out.append(g.strip())
    seen = set()
    return [g for g in out if not (g.lower() in seen or seen.add(g.lower()))]


async def _get(path: str, params: dict = None) -> Optional[dict]:
    if not _configured():
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(_base() + path, headers=_headers(), params=params)
        if r.status_code != 200:
            logger.debug("[soulsync] %s -> HTTP %d", path, r.status_code)
            return None
        body = r.json()
        return body.get("data") if isinstance(body, dict) and "data" in body else body
    except Exception as e:
        logger.debug("[soulsync] %s failed: %s", path, e)
        return None


async def artist_info(name: str) -> Optional[dict]:
    """SoulSync's aggregated view of one artist (exact name match after
    normalisation), or None. Normalised fields:
    {name, genres[], lastfm_tags[], lastfm_bio, lastfm_listeners,
     similar_artists[], mood, musicbrainz_id, external_ids{}, album_count}"""
    if not name:
        return None
    data = await _get("/library/artists", {"search": name, "limit": 5})
    want = _norm(name)
    hit = next((a for a in (data or {}).get("artists") or []
                if _norm(a.get("name")) == want), None)
    if not hit:
        return None
    tags = _norm_genres(hit.get("lastfm_tags"))
    similar = _norm_genres(hit.get("lastfm_similar"))
    ext = {k: hit.get(k) for k in
           ("musicbrainz_id", "deezer_id", "itunes_artist_id", "qobuz_id",
            "audiodb_id", "genius_url", "lastfm_url") if hit.get(k)}
    return {
        "id": hit.get("id"),
        "name": hit.get("name"),
        "genres": _norm_genres(hit.get("genres")),
        "lastfm_tags": tags,
        "lastfm_bio": hit.get("lastfm_bio") or None,
        "lastfm_listeners": hit.get("lastfm_listeners"),
        "similar_artists": similar,
        "mood": hit.get("mood") or None,
        "musicbrainz_id": hit.get("musicbrainz_id") or None,
        "external_ids": ext,
        "album_count": hit.get("album_count"),
        "is_watched": hit.get("is_watched"),
    }


async def album_info(artist_name: str, album_title: str) -> Optional[dict]:
    """SoulSync's aggregated view of one album of one artist, or None.
    Normalised: {title, genres[], lastfm_tags[], style, mood, label,
    record_type, year, track_count, musicbrainz_release_id}"""
    if not artist_name or not album_title:
        return None
    art = await artist_info(artist_name)
    if not art or not art.get("id"):
        return None
    data = await _get(f"/library/artists/{art['id']}/albums")
    want = _norm(album_title)
    hit = next((a for a in (data or {}).get("albums") or []
                if _norm(a.get("title")) == want), None)
    if not hit:
        return None
    year = None
    for k in ("year", "release_date", "created_at"):
        v = str(hit.get(k) or "")
        m = re.match(r"(\d{4})", v)
        if m and k != "created_at":
            year = m.group(1)
            break
    return {
        "id": hit.get("id"),
        "title": hit.get("title"),
        "genres": _norm_genres(hit.get("genres")),
        "lastfm_tags": _norm_genres(hit.get("lastfm_tags")),
        "style": hit.get("style") or None,
        "mood": hit.get("mood") or None,
        "label": hit.get("label") or None,
        "record_type": hit.get("record_type") or None,
        "year": year,
        "track_count": hit.get("track_count"),
        "musicbrainz_release_id": hit.get("musicbrainz_release_id") or None,
    }
