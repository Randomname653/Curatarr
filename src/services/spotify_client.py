"""
Spotify Client Credentials helper.

Uses the app-level (non-user) token flow — no OAuth redirect needed:
  POST https://accounts.spotify.com/api/token
  Authorization: Basic base64(client_id:client_secret)
  grant_type=client_credentials

Token lifetime: 3600s.  We refresh automatically with a 60s safety buffer.

Public API:
  resolve_track_genres(track_ids, client_id, client_secret)
    → {track_id: [genre, ...]}  (only tracks that resolved to ≥1 genre)
"""

import asyncio
import base64
import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_TOKEN_URL   = "https://accounts.spotify.com/api/token"
_TRACKS_URL  = "https://api.spotify.com/v1/tracks"
_ARTISTS_URL = "https://api.spotify.com/v1/artists"

# Module-level token cache (process lifetime)
_token: Optional[str] = None
_token_expires_at: float = 0.0

# 429 backoff state (monotonic clock)
_backoff_until: float = 0.0


async def _get_token(client_id: str, client_secret: str) -> Optional[str]:
    global _token, _token_expires_at

    if _token and time.time() < _token_expires_at - 60:
        return _token

    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                _TOKEN_URL,
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials"},
            )
        if r.status_code != 200:
            # Pass 97: log status code only, never the response body.
            # Spotify's token endpoint usually echoes nothing sensitive,
            # but defense-in-depth — if they ever start including the
            # request payload or any account hint in an error, we don't
            # want it landing in the curatarr log.
            logger.warning("[spotify] Token fetch failed: HTTP %s", r.status_code)
            return None
        data = r.json()
        _token = data["access_token"]
        _token_expires_at = time.time() + data.get("expires_in", 3600)
        logger.info("[spotify] Token obtained (expires in %ds)", data.get("expires_in", 3600))
        return _token
    except Exception as exc:
        logger.warning("[spotify] Token fetch error: %s", exc)
        return None


async def _batch_get(
    url: str,
    ids: list[str],
    token: str,
    chunk: int = 50,
) -> tuple[list[dict], bool]:
    """GET url?ids=id1,id2,... in chunks. Returns (all non-null items, rate_limited_flag).

    The boolean flag is True when Spotify rate-limited us hard enough that the
    caller should give up and hand the remaining work to the next stage in the
    pipeline (Last.fm), rather than burn time on backoff. Triggered when:
    - two consecutive 429 responses (per call site), or
    - a single Retry-After > 60s (Spotify is asking for a long pause).
    """
    global _backoff_until
    headers = {"Authorization": f"Bearer {token}"}
    results: list[dict] = []
    consecutive_429 = 0
    for i in range(0, len(ids), chunk):
        # Honour any active backoff before each batch
        wait = _backoff_until - time.monotonic()
        if wait > 0:
            if wait > 60:
                logger.warning("[spotify] backoff > 60s — bailing out so Last.fm can take over")
                return results, True
            logger.info("[spotify] backoff active — sleeping %.0fs", wait)
            await asyncio.sleep(wait)

        batch = ids[i:i + chunk]
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                r = await client.get(url, headers=headers, params={"ids": ",".join(batch)})
            if r.status_code == 200:
                consecutive_429 = 0
                # Response body has a single key ("tracks" or "artists") → list
                for items in r.json().values():
                    if isinstance(items, list):
                        results.extend(item for item in items if item)
            elif r.status_code == 429:
                consecutive_429 += 1
                retry_after = float(r.headers.get("retry-after", "30")) + 2
                _backoff_until = time.monotonic() + retry_after
                if retry_after > 60 or consecutive_429 >= 2:
                    logger.warning(
                        "[spotify] 429 — giving up after %d consecutive (retry-after=%.0fs); "
                        "Last.fm will take the remaining tracks",
                        consecutive_429, retry_after,
                    )
                    return results, True
                logger.warning("[spotify] 429 — backing off %.0fs", retry_after)
                await asyncio.sleep(retry_after)
                # Retry this batch once after the backoff
                try:
                    async with httpx.AsyncClient(timeout=12) as client:
                        r2 = await client.get(url, headers=headers, params={"ids": ",".join(batch)})
                    if r2.status_code == 200:
                        consecutive_429 = 0
                        for items in r2.json().values():
                            if isinstance(items, list):
                                results.extend(item for item in items if item)
                    elif r2.status_code == 429:
                        consecutive_429 += 1
                        logger.warning(
                            "[spotify] retry also 429 (%d consecutive) — bailing",
                            consecutive_429,
                        )
                        return results, True
                    else:
                        logger.warning("[spotify] %s retry HTTP %s", url.split("/")[-1], r2.status_code)
                except Exception as exc:
                    logger.warning("[spotify] batch_get retry error: %s", exc)
            else:
                logger.warning("[spotify] %s HTTP %s", url.split("/")[-1], r.status_code)
        except Exception as exc:
            logger.warning("[spotify] batch_get error: %s", exc)
    return results, False


async def resolve_track_genres(
    track_ids: list[str],
    client_id: str,
    client_secret: str,
) -> tuple[dict[str, list[str]], bool]:
    """
    Resolve Spotify genre tags for a list of Spotify track IDs.

    Two-hop resolution:
      track IDs → /v1/tracks (→ artist IDs) → /v1/artists (→ genres)

    Returns (genre_map, rate_limited):
      genre_map     {track_id: [genre, ...]} — only entries with at least one genre.
      rate_limited  True iff Spotify pushed us into a hard backoff and the caller
                    should hand the remaining work to Last.fm.

    Genres are artist-level (Spotify does not expose track-level genres).
    """
    if not track_ids or not client_id or not client_secret:
        return {}, False

    token = await _get_token(client_id, client_secret)
    if not token:
        return {}, False

    # ── Hop 1: track_id → artist_ids ─────────────────────────────────────────
    # Pass 16m: prefer ``linked_from.id`` over ``id``. Spotify "track
    # relinking" can replace a relinked track's ID with the regional/
    # licensed alternate's ID in the response payload — but the original
    # ID we asked about is preserved under ``linked_from.id``. Without
    # this, the resolver returns a key that's not in the caller's
    # track-id-to-plays map, and ``track_plays[track_id]`` raises
    # KeyError mid-pipeline.
    tracks, rate_limited = await _batch_get(_TRACKS_URL, track_ids, token)
    track_artist_map: dict[str, list[str]] = {}
    all_artist_ids: list[str] = []

    for track in tracks:
        # Use the original ID we asked about, not the relinked one
        original_id = (track.get("linked_from") or {}).get("id") or track.get("id")
        if not original_id:
            continue
        artist_ids = [a["id"] for a in (track.get("artists") or []) if a.get("id")]
        if artist_ids:
            track_artist_map[original_id] = artist_ids
            all_artist_ids.extend(artist_ids)

    if not all_artist_ids:
        return {}, rate_limited

    # ── Hop 2: artist_id → genres ─────────────────────────────────────────────
    # Skip the second hop if hop 1 already triggered a hard rate-limit — no point
    # piling more 429s on the same pool.
    if rate_limited:
        logger.info("[spotify] resolve_track_genres: rate-limited at hop 1, skipping hop 2")
        return {}, True

    unique_artist_ids = list(dict.fromkeys(all_artist_ids))  # deduplicate, preserve order
    artists, rate_limited2 = await _batch_get(_ARTISTS_URL, unique_artist_ids, token)
    artist_genre_map: dict[str, list[str]] = {
        a["id"]: a["genres"]
        for a in artists
        if a.get("id") and a.get("genres")
    }

    # ── Map genres back to track IDs ──────────────────────────────────────────
    result: dict[str, list[str]] = {}
    for tid, artist_ids in track_artist_map.items():
        genres: list[str] = []
        for aid in artist_ids:
            genres.extend(artist_genre_map.get(aid, []))
        if genres:
            # deduplicate while preserving order, cap at 8 tags
            result[tid] = list(dict.fromkeys(genres))[:8]

    logger.info("[spotify] resolve_track_genres: %d/%d tracks resolved%s",
                len(result), len(track_ids),
                " (rate-limited — partial)" if rate_limited2 else "")
    return result, rate_limited2
