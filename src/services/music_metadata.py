"""
ARR Suite LLM - Music Metadata Fetcher

Sources (all free / no mandatory key):
  1. MusicBrainz  — artist info, genres/tags, release groups (no key needed)
  2. Last.fm      — artist tags, similar artists, top tracks (LASTFM_API_KEY optional)

Falls back gracefully if services are unreachable.
"""

import asyncio
import logging
from typing import Optional

import httpx

from src.config import settings
from src.cache.metadata_cache import MetadataCache

logger = logging.getLogger(__name__)

MB_BASE = "https://musicbrainz.org/ws/2"
LASTFM_BASE = "https://ws.audioscrobbler.com/2.0"
MB_HEADERS = {
    "User-Agent": "Curatarr/1.0 (https://github.com/local/curatarr)",
    "Accept": "application/json",
}

# MusicBrainz requires max 1 request/second globally.
# This semaphore is module-level so all concurrent enrichment tasks share it.
_MB_SEM = asyncio.Semaphore(1)
_MB_LAST_REQUEST = 0.0


async def _mb_request(client: httpx.AsyncClient, url: str, params: dict) -> httpx.Response:
    """Rate-limited MusicBrainz request — max 1 req/sec globally."""
    global _MB_LAST_REQUEST
    async with _MB_SEM:
        # Ensure at least 1.1s between requests
        import time
        elapsed = time.monotonic() - _MB_LAST_REQUEST
        if elapsed < 1.1:
            await asyncio.sleep(1.1 - elapsed)
        _MB_LAST_REQUEST = time.monotonic()
        return await client.get(url, params=params, headers=MB_HEADERS)


# ── MUSICBRAINZ ───────────────────────────────────────────────────────────────

async def fetch_musicbrainz_artist(artist_name: str) -> Optional[dict]:
    """Search MusicBrainz for artist info: genres, tags, disambiguation."""
    cache = MetadataCache()
    cache_key = f"mb:artist:{artist_name[:60].lower()}"
    cached = cache.get_cache(cache_key)
    if cached:
        cache.close()
        return cached["response"]

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Search — rate limited
            r = await _mb_request(
                client, f"{MB_BASE}/artist",
                {"query": f'artist:"{artist_name}"', "limit": 1, "fmt": "json"},
            )
            if r.status_code != 200 or not r.json().get("artists"):
                cache.close()
                return None

            artist = r.json()["artists"][0]
            mbid = artist.get("id")

            if not mbid:
                cache.close()
                return None

            # Fetch full artist details with tags — rate limited
            r2 = await _mb_request(
                client, f"{MB_BASE}/artist/{mbid}",
                {"inc": "tags+genres+ratings", "fmt": "json"},
            )
            if r2.status_code != 200:
                cache.close()
                return None

            data = r2.json()

    except Exception as e:
        logger.debug("MusicBrainz error for %s: %s", artist_name, e)
        cache.close()
        return None

    tags = sorted(
        data.get("tags", []),
        key=lambda t: t.get("count", 0), reverse=True
    )
    genres = [t["name"] for t in data.get("genres", [])]
    tag_names = [t["name"] for t in tags if t.get("count", 0) > 0][:15]

    result = {
        "mbid": mbid,
        "name": data.get("name", artist_name),
        "disambiguation": data.get("disambiguation", ""),
        "type": data.get("type", ""),          # Group / Person / Orchestra
        "country": data.get("country", ""),
        "genres": genres or tag_names[:8],
        "tags": tag_names,
        "rating": data.get("rating", {}).get("value"),
        "source": "musicbrainz",
    }

    cache.set_cache(cache_key, result, days=60)
    cache.close()
    return result


async def fetch_musicbrainz_album(mbid: str, album_name: str, artist_name: str) -> Optional[dict]:
    """
    Fetch album-level metadata from MusicBrainz.
    Returns tags, release year, label, track count.
    Rate-limited via _mb_request (1 req/sec globally).
    """
    from src.cache.metadata_cache import MetadataCache
    cache = MetadataCache()
    cache_key = f"mb:album:{mbid or album_name[:40].lower()}"
    cached = cache.get_cache(cache_key)
    if cached:
        cache.close()
        return cached["response"]

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if mbid:
                r = await _mb_request(client, f"{MB_BASE}/release-group/{mbid}",
                    {"inc": "tags+genres+ratings", "fmt": "json"})
            else:
                # Search by album + artist
                r = await _mb_request(client, f"{MB_BASE}/release-group",
                    {"query": f'release:"{album_name}" AND artist:"{artist_name}"',
                     "limit": 1, "fmt": "json"})
                hits = r.json().get("release-groups", [])
                if not hits:
                    cache.close()
                    return None
                rg = hits[0]
                r = await _mb_request(client, f"{MB_BASE}/release-group/{rg['id']}",
                    {"inc": "tags+genres+ratings", "fmt": "json"})

        if r.status_code != 200:
            cache.close()
            return None

        data = r.json()
        tags = sorted(data.get("tags", []), key=lambda t: t.get("count", 0), reverse=True)
        result = {
            "mbid": data.get("id"),
            "title": data.get("title", album_name),
            "artist": artist_name,
            "type": data.get("primary-type", ""),        # Album / Single / EP / Live
            "secondary_types": data.get("secondary-types", []),
            "year": (data.get("first-release-date") or "")[:4] or None,
            "tags": [t["name"] for t in tags if t.get("count", 0) > 0][:12],
            "rating": data.get("rating", {}).get("value"),
            "source": "musicbrainz_album",
        }
        cache.set_cache(cache_key, result, days=60)
        cache.close()
        return result

    except Exception as e:
        logger.debug("MusicBrainz album error for %s/%s: %s", artist_name, album_name, e)
        cache.close()
        return None


async def enrich_album(album_name: str, artist_name: str, mbid: str = None) -> Optional[dict]:
    """
    Full album enrichment: MusicBrainz album tags + Last.fm album tags.
    Returns merged profile for embedding.
    """
    from src.cache.metadata_cache import MetadataCache
    cache = MetadataCache()
    cache_key = f"enriched:album:{artist_name[:30].lower()}:{album_name[:30].lower()}"
    cached = cache.get_cache(cache_key)
    if cached and cached.get("response", {}).get("source"):
        cache.close()
        return cached["response"]
    cache.close()

    mb_data = await fetch_musicbrainz_album(mbid, album_name, artist_name)

    # Last.fm album tags
    lastfm_key = getattr(settings, "LASTFM_API_KEY", None)
    lastfm_tags = []
    if lastfm_key:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(LASTFM_BASE, params={
                    "method": "album.gettoptags",
                    "artist": artist_name,
                    "album": album_name,
                    "api_key": lastfm_key,
                    "format": "json",
                    "limit": 10,
                })
            if r.status_code == 200:
                tags_data = r.json().get("toptags", {}).get("tag", [])
                lastfm_tags = [t["name"].lower() for t in tags_data
                               if int(t.get("count", 0)) >= 25][:8]
        except Exception:
            pass

    all_tags = list(dict.fromkeys(
        (mb_data.get("tags", []) if mb_data else []) + lastfm_tags
    ))

    result = {
        "title": album_name,
        "artist": artist_name,
        "type": mb_data.get("type", "") if mb_data else "",
        "year": mb_data.get("year") if mb_data else None,
        "tags": all_tags,
        "embedding_text": (
            f"{artist_name} — {album_name}"
            + (f" ({mb_data['year']})" if mb_data and mb_data.get("year") else "")
            + (f" [{mb_data['type']}]" if mb_data and mb_data.get("type") else "")
            + (f": {', '.join(all_tags[:8])}" if all_tags else "")
        ),
        "source": "musicbrainz_album",
    }

    from src.cache.metadata_cache import MetadataCache
    _c = MetadataCache()
    _c.set_cache(cache_key, result, days=60)
    _c.close()
    return result


# ── LAST.FM ───────────────────────────────────────────────────────────────────

async def fetch_lastfm_artist(artist_name: str) -> Optional[dict]:
    """Fetch Last.fm artist info: tags, similar artists, bio."""
    lastfm_key = getattr(settings, "LASTFM_API_KEY", None)
    if not lastfm_key:
        return None

    cache = MetadataCache()
    cache_key = f"lastfm:artist:{artist_name[:60].lower()}"
    cached = cache.get_cache(cache_key)
    if cached:
        cache.close()
        return cached["response"]

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                LASTFM_BASE,
                params={
                    "method": "artist.getinfo",
                    "artist": artist_name,
                    "api_key": lastfm_key,
                    "format": "json",
                    "autocorrect": 1,
                },
            )
            if r.status_code != 200:
                cache.close()
                return None
            data = r.json().get("artist", {})
            if not data:
                cache.close()
                return None

            # Similar artists
            r2 = await client.get(
                LASTFM_BASE,
                params={
                    "method": "artist.getSimilar",
                    "artist": artist_name,
                    "api_key": lastfm_key,
                    "format": "json",
                    "limit": 8,
                    "autocorrect": 1,
                },
            )
            similar = []
            if r2.status_code == 200:
                similar = [
                    a.get("name", "")
                    for a in r2.json().get("similarartists", {}).get("artist", [])
                ]

    except Exception as e:
        logger.debug("Last.fm error for %s: %s", artist_name, e)
        cache.close()
        return None

    tags = [t.get("name", "") for t in data.get("tags", {}).get("tag", [])]
    bio = data.get("bio", {}).get("summary", "").split("<a href")[0].strip()

    result = {
        "name": data.get("name", artist_name),
        "tags": tags,
        "genres": tags[:6],
        "similar_artists": similar,
        "listeners": data.get("stats", {}).get("listeners"),
        "playcount": data.get("stats", {}).get("playcount"),
        "bio": bio[:500] if bio else "",
        "source": "lastfm",
    }

    cache.set_cache(cache_key, result, days=30)
    cache.close()
    return result


# ── COMBINED ENRICHMENT ───────────────────────────────────────────────────────

async def enrich_artist(artist_name: str) -> Optional[dict]:
    """
    Fetch and merge artist metadata from MusicBrainz + Last.fm.
    Returns a unified profile dict.
    """
    cache = MetadataCache()
    cache_key = f"artist_profile:{artist_name[:60].lower()}"
    cached = cache.get_cache(cache_key)
    if cached:
        cache.close()
        return cached["response"]
    cache.close()

    mb, lfm = await asyncio.gather(
        fetch_musicbrainz_artist(artist_name),
        fetch_lastfm_artist(artist_name),
        return_exceptions=True,
    )
    if isinstance(mb, Exception):
        mb = None
    if isinstance(lfm, Exception):
        lfm = None

    if not mb and not lfm:
        return None

    # Merge: prefer MusicBrainz for factual data, Last.fm for tags/similar
    genres = list(dict.fromkeys(
        (mb or {}).get("genres", []) + (lfm or {}).get("genres", [])
    ))[:10]
    tags = list(dict.fromkeys(
        (mb or {}).get("tags", []) + (lfm or {}).get("tags", [])
    ))[:15]
    similar = (lfm or {}).get("similar_artists", [])

    profile = {
        "name": artist_name,
        "mbid": (mb or {}).get("mbid"),
        "type": (mb or {}).get("type", ""),
        "country": (mb or {}).get("country", ""),
        "genres": genres,
        "tags": tags,
        "similar_artists": similar,
        "bio": (lfm or {}).get("bio", ""),
        "listeners": (lfm or {}).get("listeners"),
        "rating": (mb or {}).get("rating"),
        "embedding_text": (
            f"{artist_name} — {', '.join(genres[:6])}. "
            f"Tags: {', '.join(tags[:8])}. "
            + (f"Similar to: {', '.join(similar[:5])}. " if similar else "")
            + ((lfm or {}).get("bio", "")[:200])
        ),
    }

    cache = MetadataCache()
    cache.set_cache(f"artist_profile:{artist_name[:60].lower()}", profile, days=30)
    cache.close()
    return profile


async def enrich_track(title: str, artist: str, album: str = "") -> Optional[dict]:
    """
    Fetch track-level tags from Last.fm.
    Falls back to artist profile if track not found.
    """
    lastfm_key = getattr(settings, "LASTFM_API_KEY", None)
    if not lastfm_key:
        return await enrich_artist(artist)

    cache = MetadataCache()
    cache_key = f"track:{artist[:40].lower()}:{title[:40].lower()}"
    cached = cache.get_cache(cache_key)
    if cached:
        cache.close()
        return cached["response"]
    cache.close()

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                LASTFM_BASE,
                params={
                    "method": "track.getInfo",
                    "artist": artist,
                    "track": title,
                    "api_key": lastfm_key,
                    "format": "json",
                    "autocorrect": 1,
                },
            )
        if r.status_code != 200:
            return await enrich_artist(artist)

        track = r.json().get("track", {})
        if not track:
            return await enrich_artist(artist)

        tags = [t.get("name", "") for t in track.get("toptags", {}).get("tag", [])]
        result = {
            "title": title,
            "artist": artist,
            "album": album,
            "tags": tags,
            "genres": tags[:6],
            "duration_ms": track.get("duration"),
            "listeners": track.get("listeners"),
            "playcount": track.get("playcount"),
            "embedding_text": f"{title} by {artist}. Tags: {', '.join(tags[:8])}.",
        }

        cache = MetadataCache()
        cache.set_cache(cache_key, result, days=30)
        cache.close()
        return result

    except Exception as e:
        logger.debug("Track enrich error %s - %s: %s", artist, title, e)
        return await enrich_artist(artist)
