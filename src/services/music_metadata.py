"""
ARR Suite LLM - Music Metadata Fetcher

Sources (all free / no mandatory key):
  1. MusicBrainz  — artist info, genres/tags, release groups (no key needed)
  2. Last.fm      — artist tags, similar artists, top tracks (LASTFM_API_KEY optional)

Falls back gracefully if services are unreachable.
"""

import asyncio
import logging
import re
from typing import Optional

import httpx

from src.config import settings
from src.cache.metadata_cache import MetadataCache

logger = logging.getLogger(__name__)

MB_BASE = "https://musicbrainz.org/ws/2"
LASTFM_BASE = "https://ws.audioscrobbler.com/2.0"
MB_HEADERS = {
    "User-Agent": "Curatarr/1.0 (https://github.com/Randomname653/curatarr)",
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


# Match a Deezer artist URL → numeric ID. Deezer relations on MusicBrainz
# look like ``https://www.deezer.com/artist/12345`` (sometimes with a
# ``/en/`` locale segment). Anchored on the digits so a release/album
# Deezer link can't be mistaken for an artist one.
_DEEZER_ARTIST_URL_RE = re.compile(r"deezer\.com/(?:[a-z]{2}/)?artist/(\d+)")


async def fetch_deezer_id_via_mbid(mbid: str) -> Optional[str]:
    """Resolve a MusicBrainz artist MBID to its Deezer artist ID.

    Pass 52: Lidarr items carry the MusicBrainz artist MBID
    (``foreignArtistId``). Deezer — our artist-image source — doesn't
    speak MBIDs, so we bridge through MusicBrainz url-relationships:
    artists commonly link their Deezer profile, and that URL embeds the
    numeric Deezer ID. With the ID we can hit ``/artist/{id}`` directly
    instead of a popularity-sorted ``/search/artist`` guess.

    Returns the Deezer artist ID as a string, or None when the artist
    has no linked Deezer profile. Result (including the None outcome) is
    cached 60 days — the MB↔Deezer link is stable, and caching None
    stops us re-querying linkless artists on every deletion batch.
    """
    if not mbid:
        return None
    cache = MetadataCache()
    cache_key = f"mb:deezerid:{mbid}"
    cached = cache.get_cache(cache_key)
    if cached is not None:
        cache.close()
        return cached["response"]   # may legitimately be None

    deezer_id = None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await _mb_request(
                client, f"{MB_BASE}/artist/{mbid}",
                {"inc": "url-rels", "fmt": "json"},
            )
        if r.status_code == 200:
            for rel in r.json().get("relations", []):
                resource = (rel.get("url") or {}).get("resource", "") or ""
                m = _DEEZER_ARTIST_URL_RE.search(resource)
                if m:
                    deezer_id = m.group(1)
                    break
    except Exception as e:
        logger.debug("MB→Deezer resolve failed for mbid %s: %s", mbid, e)

    # Cache the outcome — including a None result, so linkless artists
    # don't trigger a fresh rate-limited MB call on every batch.
    cache.set_cache(cache_key, deezer_id, days=60)
    cache.close()
    return deezer_id


# ── MUSICBRAINZ ───────────────────────────────────────────────────────────────

async def fetch_musicbrainz_artist(artist_name: str, mbid: Optional[str] = None) -> Optional[dict]:
    """Search MusicBrainz for artist info: genres, tags, disambiguation.

    When ``mbid`` is given (Lidarr's foreignArtistId) we fetch THAT artist
    directly and skip the name search — the only reliable way to disambiguate the
    many name-colliding artists (the five "Solstice"s: 90s doom metal vs the
    euphoric-hardstyle act). The name search takes the first hit and got it wrong.

    Pass 80: negative outcomes are cached for 7 days. Before, an artist MB
    can't find (e.g. names with characters its Lucene parser dislikes — the
    canonical case is "Axwell /\\ Ingrosso", whose ``/\\`` triggers nothing
    on a query but still returns 200) hit the rate-limited MB endpoint on
    every single retry. The script then logged a MISS and immediately
    came back for the same row, so we burned MB's 1 req/s budget for hours
    on one un-findable name. The shorter TTL (vs 60d for positive hits)
    keeps the door open for MB to add the artist later, or for us to fix
    the query escaping.
    """
    cache = MetadataCache()
    cache_key = f"mb:artist:{mbid}" if mbid else f"mb:artist:{artist_name[:60].lower()}"
    cached = cache.get_cache(cache_key)
    if cached:
        cache.close()
        return cached["response"]   # may legitimately be None (neg-cached)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if not mbid:
                # No id from the arr — fall back to a name search (ambiguous:
                # takes the first hit, which is the name-collision risk).
                r = await _mb_request(
                    client, f"{MB_BASE}/artist",
                    {"query": f'artist:"{artist_name}"', "limit": 1, "fmt": "json"},
                )
                if r.status_code != 200 or not r.json().get("artists"):
                    cache.set_cache(cache_key, None, days=7)
                    cache.close()
                    return None

                mbid = r.json()["artists"][0].get("id")
                if not mbid:
                    cache.set_cache(cache_key, None, days=7)
                    cache.close()
                    return None

            # Fetch full artist details with tags — rate limited
            r2 = await _mb_request(
                client, f"{MB_BASE}/artist/{mbid}",
                {"inc": "tags+genres+ratings", "fmt": "json"},
            )
            if r2.status_code != 200:
                cache.set_cache(cache_key, None, days=7)
                cache.close()
                return None

            data = r2.json()

    except Exception as e:
        logger.debug("MusicBrainz error for %s: %s", artist_name, e)
        # No neg-cache on exception (timeout, DNS, etc.) — transient.
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

    NOT YET WIRED (deliberate backlog, follow-up audit 2026-07-08): built for
    album-level evidence, but music proposals are currently artist-level.
    Wire it up when album debates (Bomber-class) get their own dossier.
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
        return cached["response"]   # may legitimately be None (neg-cached)

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
            # Pass 80: cache the "no record" outcome for 7 days. Last.fm
            # also returns 200 with an empty body for artists it doesn't
            # know — the old code dropped that on the floor and re-queried
            # forever on every retry tick (same hammer-loop as MB above).
            if r.status_code != 200:
                cache.set_cache(cache_key, None, days=7)
                cache.close()
                return None
            data = r.json().get("artist", {})
            if not data:
                cache.set_cache(cache_key, None, days=7)
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
    # Strip HTML link tail then scrub Wikipedia-style footnote citations like [4][5]
    bio_raw = data.get("bio", {}).get("summary", "").split("<a href")[0].strip()
    bio = re.sub(r'\[\d+\]', '', bio_raw).strip()
    # Collapse multiple spaces/newlines left over after stripping
    bio = re.sub(r'[ \t]{2,}', ' ', bio).strip()

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

async def enrich_artist(artist_name: str, skip_mb: bool = False, mbid: Optional[str] = None) -> Optional[dict]:
    """
    Fetch and merge artist metadata from MusicBrainz + Last.fm.
    Returns a unified profile dict.

    ``mbid`` (Lidarr's foreignArtistId) pins MusicBrainz to the EXACT artist
    instead of a name search — without it, name-colliding artists resolve to the
    wrong act (the "Solstice" doom-metal-vs-hardstyle bug).

    Phase 2 #38b: ``skip_mb=True`` runs Last.fm only — bypasses the
    MusicBrainz ``Semaphore(1)`` + 1 req/sec floor, which is the
    music-lane throughput killer (66% of library × 1 req/s = ~4 h).
    The returned profile has ``mbid=None`` / ``country=None`` /
    ``type=""`` / ``rating=None`` (the MB-sourced fields). Caller is
    expected to flag the row ``provisional=True`` so the #41 scheduler
    later upgrades it with a real MB call. We also bypass the
    ``artist_profile:`` MetadataCache for skip_mb fetches so a fast
    pass cannot poison a later full pass — the full pass would
    otherwise read the cached fast profile and short-circuit before
    MB ever ran.
    """
    cache_key = f"artist_profile:{mbid}" if mbid else f"artist_profile:{artist_name[:60].lower()}"

    if not skip_mb:
        # Full mode: regular read-through cache.
        cache = MetadataCache()
        cached = cache.get_cache(cache_key)
        if cached:
            cache.close()
            return cached["response"]
        cache.close()

        # SoulSync FIRST (owner decision 2026-08-15): the LAN neighbour
        # aggregates 10 providers in ONE call and is the richer, actively
        # maintained source — its fields WIN the merge below. MusicBrainz
        # stays for what SoulSync lacks (type/country/rating); Last.fm is
        # skipped entirely when SoulSync already carries its fields, and
        # both remain the full fallback for installations WITHOUT SoulSync
        # (the client degrades to None when unconfigured).
        ss = None
        try:
            from src.services.soulsync_client import artist_info
            ss = await artist_info(artist_name)
        except Exception:
            ss = None
        _ss_covers_lastfm = bool(ss and ss.get("lastfm_tags")
                                 and ss.get("lastfm_bio")
                                 and ss.get("similar_artists"))
        mb, lfm = await asyncio.gather(
            fetch_musicbrainz_artist(artist_name, mbid=mbid),
            (fetch_lastfm_artist(artist_name) if not _ss_covers_lastfm
             else asyncio.sleep(0, result=None)),
            return_exceptions=True,
        )
        if isinstance(mb, Exception):
            mb = None
        if isinstance(lfm, Exception):
            lfm = None
    else:
        # Fast mode: skip MB entirely + skip the artist_profile cache
        # so a fast result can't shadow a later full fetch. The outer
        # raw:music:{id_key} cache at fetch_and_prepare_raw level still
        # short-circuits identical fast requests on the same item.
        mb = None
        ss = None
        try:
            lfm = await fetch_lastfm_artist(artist_name)
        except Exception:
            lfm = None

    if not mb and not lfm and not ss:
        # Pass 80: cache the merged-None outcome as well. The two sub-fetches
        # now neg-cache on their own — this layer's neg-cache is the
        # belt-and-braces: a single ``get_cache`` lookup short-circuits the
        # whole gather() so we don't even pay the await/dispatch cost when
        # an artist is reliably unfindable. 7 days matches the sub-fetches.
        # #38b: skip the cache write in fast mode — a fast-mode "no data"
        # may flip to "found" once MB is consulted in the upgrade pass.
        if not skip_mb:
            cache = MetadataCache()
            cache.set_cache(f"artist_profile:{artist_name[:60].lower()}", None, days=7)
            cache.close()
        return None

    # Merge: SoulSync fields WIN (primary), MusicBrainz supplies the factual
    # fields SoulSync lacks (type/country/rating), Last.fm fills what's left.
    _ss = ss or {}
    genres = list(dict.fromkeys(
        _ss.get("genres", []) + _ss.get("lastfm_tags", [])
        + (mb or {}).get("genres", []) + (lfm or {}).get("genres", [])
    ))[:10]
    tags = list(dict.fromkeys(
        _ss.get("lastfm_tags", [])
        + (mb or {}).get("tags", []) + (lfm or {}).get("tags", [])
    ))[:15]
    similar = _ss.get("similar_artists") or (lfm or {}).get("similar_artists", [])

    # Bio comes pre-cleaned from fetch_lastfm_artist (citations stripped).
    # Apply a second-pass strip here as well in case the profile was cached
    # before the fix was deployed.
    raw_bio = _ss.get("lastfm_bio") or (lfm or {}).get("bio", "") or ""
    clean_bio = re.sub(r'\[\d+\]', '', raw_bio).strip()

    profile = {
        "name": artist_name,
        "mbid": (mb or {}).get("mbid") or _ss.get("musicbrainz_id"),
        "type": (mb or {}).get("type", ""),
        "country": (mb or {}).get("country", ""),
        "genres": genres,
        "tags": tags,
        "similar_artists": similar,
        "bio": clean_bio,
        "listeners": _ss.get("lastfm_listeners") or (lfm or {}).get("listeners"),
        "rating": (mb or {}).get("rating"),
        # SoulSync extras — mood for prompts, deezer_id lets the poster
        # path skip the MB→Deezer bridge. NOT part of embedding_text: the
        # text template stays byte-stable so the index doesn't drift.
        "mood": _ss.get("mood") or None,
        "deezer_id": (_ss.get("external_ids") or {}).get("deezer_id"),
        "metadata_source": "soulsync" if ss else ("mb+lastfm" if (mb or lfm) else "none"),
        "embedding_text": (
            f"{artist_name} — {', '.join(genres[:6])}. "
            f"Tags: {', '.join(tags[:8])}. "
            + (f"Similar to: {', '.join(similar[:5])}. " if similar else "")
            + (clean_bio[:200] if clean_bio else "")
        ),
    }

    # #38b: only write the artist_profile cache in full mode. A fast-mode
    # profile is intentionally not cached here so a subsequent full pass
    # for the same artist sees an empty profile cache + runs MB.
    if not skip_mb:
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

    # Pass 80b: helper to cache the artist-profile fallback at the *track*
    # cache_key when Last.fm doesn't know the track. Without this, the daily
    # scheduler warmer (`_warm_top_track_metadata`) and the chat track-
    # discussion path (`chat.py:1516`) re-queried Last.fm on every call for
    # any track it didn't know — same sibling bug class as the artist
    # hammer-loop, just at lower amplitude. Caching None instead would have
    # broken chat.py's ``if enr:`` truthy check (line 1520) — the user would
    # silently lose the artist-profile fallback context. So we cache the
    # actual artist fallback. 7-day TTL keeps the door open for Last.fm to
    # add the track later. Footprint cost: a per-track copy of the artist
    # profile (~1-5KB each), bounded by realistic top-N track counts.
    async def _cache_artist_fallback() -> Optional[dict]:
        fb = await enrich_artist(artist)
        _c = MetadataCache()
        _c.set_cache(cache_key, fb, days=7)
        _c.close()
        return fb

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
            return await _cache_artist_fallback()

        track = r.json().get("track", {})
        if not track:
            return await _cache_artist_fallback()

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
