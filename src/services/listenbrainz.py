"""
ListenBrainz global popularity — music reception evidence.

One keyless GET per artist (``popularity/top-release-groups-for-artist``)
returns listen/listener counts for the artist's whole release-group
catalogue. That single call feeds two evidence surfaces:

  - artist deletion debates: total catalogue listens + the top album's
    audience (``Global listening (ListenBrainz): …`` in the verified block)
  - album chat dossiers: the discussed album's rank within the artist's
    own catalogue by global listens

The ``top-recordings-for-artist`` endpoint exists too (~2,000 entries per
big artist) but adds nothing at artist/album granularity — deliberately
not called. Revisit only if track-level evidence is ever needed.

Etiquette mirrors MusicBrainz (same operator, MetaBrainz): descriptive
User-Agent and ~1 req/s. The limiter here is deliberately SEPARATE from
music_metadata's ``_MB_SEM`` — different host, and sharing the gate would
let ListenBrainz calls stall the already-throttled MusicBrainz lane.

AUTH: a free account token (``LISTENBRAINZ_TOKEN``) is required — LB
locked the popularity endpoints behind auth after AI-scraper abuse (the
401 body says so verbatim; a handful of very popular artists still return
cached 200s keyless, which is a trap, not a contract). Without a token
every fetch short-circuits to the TRANSIENT answer, so nothing is ever
stamped as checked and the evidence appears the moment a token lands.

Raw responses run 100-500 KB; only a digest is ever cached.
"""

import asyncio
import logging
import re
import time
from typing import Optional

import httpx

from src.cache.metadata_cache import MetadataCache
from src.config import settings

logger = logging.getLogger(__name__)

LB_BASE = "https://api.listenbrainz.org/1"
LB_HEADERS = {
    "User-Agent": "Curatarr/1.0 (https://github.com/Randomname653/Curatarr)",
    "Accept": "application/json",
}


def _auth_headers() -> Optional[dict]:
    """Headers incl. the account token, or None when no token is set."""
    token = getattr(settings, "LISTENBRAINZ_TOKEN", None)
    if not token:
        return None
    return {**LB_HEADERS, "Authorization": f"Token {token}"}

_LB_SEM = asyncio.Semaphore(1)
_LB_LAST_REQUEST = 0.0

_TIMEOUT = 20.0
_POSITIVE_TTL_DAYS = 30
_NEGATIVE_TTL_DAYS = 7
# Digest cap: rank precision for anything a debate will plausibly name,
# bounded blob size for pathological catalogues (classical composers).
_ALBUM_CAP = 500


async def _lb_request(client: httpx.AsyncClient, url: str,
                      headers: dict) -> httpx.Response:
    """Rate-limited ListenBrainz request — max ~1 req/sec globally."""
    global _LB_LAST_REQUEST
    async with _LB_SEM:
        elapsed = time.monotonic() - _LB_LAST_REQUEST
        if elapsed < 1.1:
            await asyncio.sleep(1.1 - elapsed)
        _LB_LAST_REQUEST = time.monotonic()
        return await client.get(url, headers=headers)


_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—−"})


def norm_album_title(s: str) -> str:
    """Normalise an album title for rank matching.

    Public on purpose: album_dossier imports it so both sides of the
    lookup (digest ``norm_name`` and the queried title) fold identically —
    the codebase's other ``_norm`` helpers stay module-private because
    nothing else needs to match against their output.
    """
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower().translate(_DASHES)).strip()


def _digest(artist_mbid: str, entries: list) -> dict:
    """Trim a top-release-groups payload to what evidence needs.

    Totals run over the FULL array so ``total_listens`` reflects the whole
    catalogue even though the album list is capped.
    """
    total_listens = 0
    top_user_count = 0
    albums = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        listens = int(e.get("total_listen_count") or 0)
        users = int(e.get("total_user_count") or 0)
        total_listens += listens
        top_user_count = max(top_user_count, users)
        rg = e.get("release_group") or {}
        name = rg.get("name")
        if name:
            albums.append({
                "name": name,
                "norm_name": norm_album_title(name),
                "date": rg.get("date"),
                "type": rg.get("type"),
                "listens": listens,
                "listeners": users,
            })
    albums.sort(key=lambda a: a["listens"], reverse=True)
    return {
        "artist_mbid": artist_mbid,
        "total_listens": total_listens,
        "top_user_count": top_user_count,
        "n_release_groups": len(entries),
        "albums": albums[:_ALBUM_CAP],
    }


async def fetch_artist_popularity(artist_mbid: str,
                                  cache: Optional[MetadataCache] = None):
    """Global popularity digest for one artist. Tri-state:

      - dict with content → real data (cached 30 d)
      - ``{}``            → DEFINITIVE: ListenBrainz tracks nothing for
                            this artist (HTTP 200, empty array; cached 7 d)
      - ``None``          → transient failure — NOT cached, retry later

    Same contract as ``fetch_significance``: silence and emptiness are
    different answers, and only answers get stamped.
    """
    if not artist_mbid:
        return None
    headers = _auth_headers()
    if headers is None:
        # No token configured → the API would 401. Transient by design:
        # nothing is cached or stamped, so configuring a token later makes
        # the evidence appear without any cache surgery.
        logger.debug("[listenbrainz] no LISTENBRAINZ_TOKEN — skipping")
        return None
    owns = cache is None
    if owns:
        cache = MetadataCache()
    try:
        key = f"lb:artist:{artist_mbid}"
        hit = cache.get_cache(key)
        if hit is not None:
            resp = hit.get("response")
            return resp if isinstance(resp, dict) else {}

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await _lb_request(
                    client,
                    f"{LB_BASE}/popularity/top-release-groups-for-artist/{artist_mbid}",
                    headers,
                )
        except Exception as e:
            logger.debug("[listenbrainz] request failed for %s: %s", artist_mbid, e)
            return None

        if r.status_code == 404:
            # Unknown MBID is a definitive answer, not an outage.
            cache.set_cache(key, {}, days=_NEGATIVE_TTL_DAYS)
            return {}
        if r.status_code != 200:
            logger.debug("[listenbrainz] HTTP %s for %s", r.status_code, artist_mbid)
            return None
        try:
            entries = r.json()
        except Exception:
            return None
        if not isinstance(entries, list):
            return None
        if not entries:
            cache.set_cache(key, {}, days=_NEGATIVE_TTL_DAYS)
            return {}

        digest = _digest(artist_mbid, entries)
        cache.set_cache(key, digest, days=_POSITIVE_TTL_DAYS)
        return digest
    finally:
        if owns:
            cache.close()


# ── raw-doc top-up (mirrors reception.topup_reception) ───────────────────────

async def topup_listenbrainz(
    title: str,
    media_type: str,
    *,
    tmdb_id=None, tvdb_id=None, anilist_id=None, anidb_id=None,
    plex_rating_key=None, artist_mbid: Optional[str] = None,
    cache=None,
) -> bool:
    """Fetch global popularity once and store the digest on the artist's
    ``raw:music:*`` cache entries, idempotent via ``lb_checked``. Returns
    True when a popularity digest was actually added.

    No MBID resolvable is a PREREQUISITE gap, not an answer — nothing is
    stamped, so a later debate (after the #41 upgrade pass fills the mbid)
    runs the top-up for real. The re-check until then costs one dict read.
    """
    if media_type != "music":
        return False
    from src.cache.metadata_cache import MetadataCache, write_fields
    from src.services.media_enricher import _RAW_CACHE_DAYS
    owns = cache is None
    if owns:
        cache = MetadataCache()
    try:
        id_keys = [v for v in (anilist_id, anidb_id, tmdb_id, tvdb_id,
                               plex_rating_key) if v]
        t40 = (title or "")[:40]
        if t40:
            id_keys.append(t40)
        targets = []
        for k in id_keys:
            hit = cache.get_cache(f"raw:{media_type}:{k}")
            if not hit or not isinstance(hit.get("response"), dict):
                continue
            raw = hit["response"]
            if raw.get("lb_checked"):
                return False  # already answered for this artist
            targets.append((f"raw:{media_type}:{k}", raw))
        if not targets:
            return False

        doc = targets[0][1]
        mbid = artist_mbid or doc.get("mbid")
        if not mbid:
            return False  # prerequisite gap — do NOT stamp lb_checked

        digest = await fetch_artist_popularity(mbid, cache=cache)
        if digest is None:
            return False  # transient — nothing stamped, retried next debate

        added = False
        for key, raw in targets:
            fields = {"lb_checked": True}
            if digest:
                fields["lb_popularity"] = digest
                added = True
            # Only these fields — the entry was read before a slow fetch,
            # and another walker may have written to it since.
            write_fields(cache, key, raw, fields, days=_RAW_CACHE_DAYS)
        return added
    except Exception as e:
        logger.debug("[listenbrainz] top-up failed for %r: %s", title, e)
        return False
    finally:
        if owns:
            cache.close()
