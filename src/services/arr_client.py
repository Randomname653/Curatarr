"""
Media Service API Clients - Abstractive layer for Plex, Radarr, Sonarr, Lidarr, Tautulli.

Provides unified async interface with rate limiting, caching, and error handling.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


def classify_sonarr_category(series: dict) -> str:
    """Return ``'anime'`` or ``'show'`` for a Sonarr series object.

    Pass 59: single source of truth. Two pipelines used to disagree —
    ``_collect_arr_items`` (enrichment) keyed only on Sonarr's
    ``seriesType``, while ``_fetch_arr_candidates`` (deletion proposals)
    ALSO checked for an "Anime" genre tag. The drift meant the same
    series got cached under ``enriched:show:*`` by one path and looked
    up under ``enriched:anime:*`` by the other — so a deletion
    discussion missed its own enrichment profile and the curator
    hallucinated from training data (e.g. "Tetsujin 28 FX" → described
    as the 2004 remake instead of the 1992 series).

    Heuristic is the generous one: Sonarr's ``seriesType`` OR an "Anime"
    genre tag. ``seriesType`` alone misses titles Sonarr filed as
    "standard" that are plainly anime; the genre tag catches those. The
    category mainly steers the AniList-vs-TMDB enrichment cascade order,
    so erring toward 'anime' for animated JP titles is the right call.
    """
    if (series.get("seriesType") == "anime"
            or "Anime" in (series.get("genres") or [])):
        return "anime"
    return "show"


class RateLimiter:
    """Token bucket rate limiter."""

    def __init__(self, requests_per_minute: int):
        self.capacity = requests_per_minute
        self.tokens = requests_per_minute
        self.refill_rate = requests_per_minute / 60  # tokens per second
        self.last_refill = datetime.now()
        self.lock = asyncio.Lock()

    async def acquire(self):
        """Wait until token available."""
        async with self.lock:
            elapsed = (datetime.now() - self.last_refill).total_seconds()
            self.tokens = min(
                self.capacity,
                self.tokens + elapsed * self.refill_rate
            )
            self.last_refill = datetime.now()

            if self.tokens < 1:
                wait_time = (1 - self.tokens) / self.refill_rate
                await asyncio.sleep(wait_time)
                self.tokens = 0
            else:
                self.tokens -= 1


class ServiceType(str, Enum):
    """Supported media services."""
    PLEX = "plex"
    TAUTULLI = "tautulli"
    RADARR = "radarr"
    SONARR = "sonarr"
    LIDARR = "lidarr"


class MediaService(ABC):
    """Abstract base for media service clients."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        service_type: ServiceType,
        rate_limit_rpm: int = 20
    ):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.service_type = service_type
        self.rate_limiter = RateLimiter(rate_limit_rpm)
        self.session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, tuple] = {}  # {key: (value, expiry_time)}
        self.cache_ttl_seconds = 300  # 5 minute default

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def request(
        self,
        method: str,
        endpoint: str,
        json_data: Optional[Dict] = None,
        use_cache: bool = False,
        cache_key: Optional[str] = None,
        max_retries: int = 3,
        params: Optional[Dict] = None,
        timeout: float = 30.0,
        max_timeout_retries: int = 1,
    ) -> Dict:
        """Execute API request with rate limiting and caching.

        429 retries are bounded — previously this method recursed on rate-limit,
        which on a persistently throttled service would blow the stack.

        ``timeout`` (Pass 16j) is per-call so heavy endpoints (full library
        listings on a slow Lidarr / busy Sonarr) can opt into longer waits.
        Default 30 s is fine for ``/system/status``, profile lists, lookups.
        Library listings (``get_artists`` / ``get_movies`` / ``get_series``)
        bump it to 90 s.

        ``max_timeout_retries`` (Pass 16j) bounds how often we re-attempt on
        ``asyncio.TimeoutError``. Default 1 means "try twice, then give up"
        — enough to ride out a single transient slow response without
        burning the user's patience. Counted SEPARATELY from ``max_retries``
        (which is for 429s) because a hung arr is a different failure
        mode than rate limiting.

        ``params`` is for query-string values that should NOT appear in log
        messages or error strings (e.g. Tautulli's apikey query param).
        aiohttp serialises them onto the URL at request time, but ``endpoint``
        — which is what we log — stays clean.
        """

        # Check cache
        if use_cache and cache_key:
            cached_value = self._get_cached(cache_key)
            if cached_value is not None:
                logger.debug(f"Cache hit: {cache_key}")
                return cached_value

        headers = self._get_headers()
        url = f"{self.base_url}{endpoint}"

        # Pass 16j: rate-limit retries (429) and timeout retries are tracked
        # independently — a slow arr is a different failure mode than a
        # rate-limited one and shouldn't share the budget. The previous loop
        # used a single attempt counter, so a timeout on a single-shot call
        # (max_retries=0) silently fell through without retrying.
        rate_limit_attempts = 0
        timeout_attempts    = 0

        while True:
            await self.rate_limiter.acquire()
            try:
                async with self.session.request(
                    method,
                    url,
                    json=json_data,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    if resp.status == 429:
                        if rate_limit_attempts >= max_retries:
                            raise Exception(f"API rate-limited after {max_retries} retries")
                        rate_limit_attempts += 1
                        retry_after = int(resp.headers.get('Retry-After', 60))
                        logger.warning(
                            f"Rate limited (attempt {rate_limit_attempts}/{max_retries + 1}), "
                            f"retrying after {retry_after}s"
                        )
                        await asyncio.sleep(retry_after)
                        continue

                    if resp.status >= 400:
                        error_text = await resp.text()
                        raise Exception(f"API error {resp.status}: {error_text}")

                    result = await resp.json()

                    if use_cache and cache_key:
                        self._cache_set(cache_key, result)

                    return result

            except asyncio.TimeoutError:
                # Bounded retry — a single slow response on a flaky arr
                # (Lidarr in particular) shouldn't break the UI when the
                # next attempt usually succeeds.
                timeout_attempts += 1
                if timeout_attempts > max_timeout_retries:
                    logger.error(
                        f"Request timeout after {timeout_attempts} attempts ({timeout}s each): {endpoint}"
                    )
                    raise
                logger.warning(
                    f"Request timeout on {endpoint} (attempt {timeout_attempts}/"
                    f"{max_timeout_retries + 1}, {timeout}s) — retrying"
                )
                # Brief backoff before retry — gives the arr a chance to flush
                # whatever was hanging.
                await asyncio.sleep(2)
                continue

    def _get_headers(self) -> Dict:
        """Get request headers (service-specific)."""
        return {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def _get_cached(self, key: str) -> Optional[Any]:
        """Get value from cache if not expired."""
        if key in self._cache:
            value, expiry = self._cache[key]
            if datetime.now() < expiry:
                return value
            else:
                del self._cache[key]
        return None

    def _cache_set(self, key: str, value: Any):
        """Store value in cache."""
        expiry = datetime.now() + timedelta(seconds=self.cache_ttl_seconds)
        self._cache[key] = (value, expiry)


class PlexClient(MediaService):
    """Plex API client."""

    def __init__(self, base_url: str, token: str):
        super().__init__(
            base_url=base_url,
            api_key=token,
            service_type=ServiceType.PLEX,
            rate_limit_rpm=10
        )
        self.token = token

    def _get_headers(self) -> Dict:
        headers = super()._get_headers()
        headers["X-Plex-Token"] = self.token
        return headers

    async def get_libraries(self) -> List[Dict]:
        """Fetch all libraries."""
        result = await self.request(
            "GET",
            "/library/sections",
            use_cache=True,
            cache_key="plex_libraries"
        )
        return result.get("MediaContainer", {}).get("Directory", [])

    async def get_library_items(self, section_id: int) -> List[Dict]:
        """Fetch all items in a library section."""
        result = await self.request(
            "GET",
            f"/library/sections/{section_id}/all",
            use_cache=True,
            cache_key=f"plex_library_{section_id}"
        )
        return result.get("MediaContainer", {}).get("Metadata", [])

    async def get_watchlist(self) -> List[Dict]:
        """Fetch user's watchlist (GraphQL as fallback)."""
        # Try REST API first (may be deprecated)
        try:
            result = await self.request(
                "GET",
                "/library/watchlist",
                use_cache=True,
                cache_key="plex_watchlist"
            )
            return result.get("MediaContainer", {}).get("Metadata", [])
        except Exception:
            logger.warning("REST watchlist endpoint failed, trying GraphQL")
            # Fallback to GraphQL (not fully implemented)
            return []


class TautulliClient(MediaService):
    """Tautulli (Plex stats) API client."""

    def __init__(self, base_url: str, api_key: str):
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            service_type=ServiceType.TAUTULLI,
            rate_limit_rpm=30
        )

    def _get_headers(self) -> Dict:
        headers = super()._get_headers()
        return headers

    async def get_plays(
        self,
        user_id: Optional[int] = None,
        days: int = 730,  # 24 months
        limit: int = 10000
    ) -> List[Dict]:
        """Get watch history."""
        q = {"action": "get_plays", "limit": str(limit), "apikey": self.api_key}
        if user_id:
            q["user_id"] = str(user_id)

        result = await self.request(
            "GET",
            "/api/v2",
            params=q,
            use_cache=True,
            cache_key=f"tautulli_plays_{user_id}_{days}"
        )
        return result.get("response", {}).get("data", [])

    async def get_media_info(self, media_key: str) -> Dict:
        """Get detailed info for a media item."""
        q = {"action": "get_media_info", "rating_key": media_key, "apikey": self.api_key}
        result = await self.request(
            "GET",
            "/api/v2",
            params=q,
            use_cache=True,
            cache_key=f"tautulli_media_{media_key}"
        )
        return result.get("response", {}).get("data", {})


class RadarrClient(MediaService):
    """Radarr (Movies) API client."""

    def __init__(self, base_url: str, api_key: str):
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            service_type=ServiceType.RADARR,
            rate_limit_rpm=20
        )

    def _get_headers(self) -> Dict:
        headers = super()._get_headers()
        headers["X-Api-Key"] = self.api_key
        return headers

    async def get_movies(self) -> List[Dict]:
        """Fetch all movies. 90s timeout — full library on a busy Radarr can
        take a while as it joins statistics across all entries."""
        result = await self.request(
            "GET",
            "/api/v3/movie",
            use_cache=True,
            cache_key="radarr_movies",
            timeout=90.0,
        )
        return result

    async def get_movie(self, movie_id: int) -> Dict:
        """Fetch single movie details."""
        result = await self.request(
            "GET",
            f"/api/v3/movie/{movie_id}",
            use_cache=True,
            cache_key=f"radarr_movie_{movie_id}"
        )
        return result

    async def delete_movie(self, movie_id: int, delete_files: bool = True) -> Dict:
        """Delete movie and optionally remove files."""
        params = f"?deleteFiles={str(delete_files).lower()}"
        result = await self.request(
            "DELETE",
            f"/api/v3/movie/{movie_id}{params}",
            use_cache=False
        )
        return result

    # ── Pass 16a: Library-Add extensions ─────────────────────────────────
    async def lookup_movie(self, term: str) -> List[Dict]:
        """Search Radarr's TMDB-backed lookup. Returns raw match list."""
        return await self.request(
            "GET", "/api/v3/movie/lookup",
            params={"term": term}, use_cache=False,
        )

    async def list_root_folders(self) -> List[Dict]:
        return await self.request(
            "GET", "/api/v3/rootfolder",
            use_cache=True, cache_key="radarr_root_folders",
        )

    async def list_quality_profiles(self) -> List[Dict]:
        return await self.request(
            "GET", "/api/v3/qualityprofile",
            use_cache=True, cache_key="radarr_quality_profiles",
        )

    async def list_tags(self) -> List[Dict]:
        return await self.request(
            "GET", "/api/v3/tag",
            use_cache=False,
        )

    async def create_tag(self, label: str) -> Dict:
        return await self.request(
            "POST", "/api/v3/tag",
            json_data={"label": label}, use_cache=False,
        )

    async def add_movie(
        self,
        tmdb_id: int,
        title: str,
        year: int,
        root_folder_path: str,
        quality_profile_id: int,
        monitored: bool = True,
        search_for_movie: bool = True,
        tags: Optional[List[int]] = None,
    ) -> Dict:
        """Add a movie to Radarr. Caller must have already resolved
        root_folder_path / quality_profile_id via the list_* helpers."""
        payload = {
            "tmdbId": tmdb_id,
            "title": title,
            "year": year,
            "rootFolderPath": root_folder_path,
            "qualityProfileId": quality_profile_id,
            "monitored": monitored,
            "minimumAvailability": "released",
            "tags": tags or [],
            "addOptions": {
                "searchForMovie": search_for_movie,
            },
        }
        return await self.request(
            "POST", "/api/v3/movie",
            json_data=payload, use_cache=False,
        )

    async def test_connection(self) -> Dict:
        """GET /api/v3/system/status — used by setup wizard / Settings."""
        return await self.request(
            "GET", "/api/v3/system/status",
            use_cache=False,
        )


class SonarrClient(MediaService):
    """Sonarr (TV/Anime) API client."""

    def __init__(self, base_url: str, api_key: str):
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            service_type=ServiceType.SONARR,
            rate_limit_rpm=20
        )

    def _get_headers(self) -> Dict:
        headers = super()._get_headers()
        headers["X-Api-Key"] = self.api_key
        return headers

    async def get_series(self) -> List[Dict]:
        """Fetch all series. 90s timeout — full library + season statistics
        on a busy Sonarr can hang briefly."""
        result = await self.request(
            "GET",
            "/api/v3/series",
            use_cache=True,
            cache_key="sonarr_series",
            timeout=90.0,
        )
        return result

    async def get_series_details(self, series_id: int) -> Dict:
        """Fetch series with statistics."""
        result = await self.request(
            "GET",
            f"/api/v3/series/{series_id}?includeSeasonStatistics=true",
            use_cache=True,
            cache_key=f"sonarr_series_{series_id}"
        )
        return result

    async def get_episodes(self, series_id: int) -> List[Dict]:
        """Fetch all episodes for series."""
        result = await self.request(
            "GET",
            f"/api/v3/episode?seriesId={series_id}",
            use_cache=True,
            cache_key=f"sonarr_episodes_{series_id}"
        )
        return result

    async def delete_series(
        self,
        series_id: int,
        delete_files: bool = True,
        ignore_monitored_status: bool = False
    ) -> Dict:
        """Delete series and optionally remove files."""
        params = f"?deleteFiles={str(delete_files).lower()}"
        if ignore_monitored_status:
            params += "&ignoreMissingArticles=true"

        result = await self.request(
            "DELETE",
            f"/api/v3/series/{series_id}{params}",
            use_cache=False
        )
        return result

    # ── Pass 16a: Library-Add extensions ─────────────────────────────────
    async def lookup_series(self, term: str) -> List[Dict]:
        """TVDB-backed lookup. Returns raw match list."""
        return await self.request(
            "GET", "/api/v3/series/lookup",
            params={"term": term}, use_cache=False,
        )

    async def list_root_folders(self) -> List[Dict]:
        return await self.request(
            "GET", "/api/v3/rootfolder",
            use_cache=True, cache_key="sonarr_root_folders",
        )

    async def list_quality_profiles(self) -> List[Dict]:
        return await self.request(
            "GET", "/api/v3/qualityprofile",
            use_cache=True, cache_key="sonarr_quality_profiles",
        )

    async def list_language_profiles(self) -> List[Dict]:
        """v3 may or may not expose this depending on Sonarr version."""
        try:
            return await self.request(
                "GET", "/api/v3/languageprofile",
                use_cache=True, cache_key="sonarr_language_profiles",
            )
        except Exception:
            return []

    async def list_tags(self) -> List[Dict]:
        return await self.request(
            "GET", "/api/v3/tag", use_cache=False,
        )

    async def create_tag(self, label: str) -> Dict:
        return await self.request(
            "POST", "/api/v3/tag",
            json_data={"label": label}, use_cache=False,
        )

    async def add_series(
        self,
        tvdb_id: int,
        title: str,
        root_folder_path: str,
        quality_profile_id: int,
        language_profile_id: Optional[int] = None,
        monitored: bool = True,
        season_folder: bool = True,
        search_for_missing: bool = True,
        series_type: str = "standard",  # "standard" | "anime" | "daily"
        tags: Optional[List[int]] = None,
    ) -> Dict:
        """Add a series to Sonarr."""
        payload = {
            "tvdbId": tvdb_id,
            "title": title,
            "rootFolderPath": root_folder_path,
            "qualityProfileId": quality_profile_id,
            "monitored": monitored,
            "seasonFolder": season_folder,
            "seriesType": series_type,
            "tags": tags or [],
            "addOptions": {
                "ignoreEpisodesWithFiles": False,
                "ignoreEpisodesWithoutFiles": False,
                "searchForMissingEpisodes": search_for_missing,
            },
        }
        if language_profile_id is not None:
            payload["languageProfileId"] = language_profile_id
        return await self.request(
            "POST", "/api/v3/series",
            json_data=payload, use_cache=False,
        )

    async def test_connection(self) -> Dict:
        return await self.request(
            "GET", "/api/v3/system/status", use_cache=False,
        )


class LidarrClient(MediaService):
    """Lidarr (Music) API client."""

    def __init__(self, base_url: str, api_key: str):
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            service_type=ServiceType.LIDARR,
            rate_limit_rpm=20
        )

    def _get_headers(self) -> Dict:
        headers = super()._get_headers()
        headers["X-Api-Key"] = self.api_key
        return headers

    async def get_artists(self) -> List[Dict]:
        """Fetch all artists. 90s timeout — Lidarr's /artist endpoint is
        notoriously slow on bigger libraries (Pass 16j: bumped from 30s
        after a real-world 30s timeout on a working Lidarr)."""
        result = await self.request(
            "GET",
            "/api/v1/artist",
            use_cache=True,
            cache_key="lidarr_artists",
            timeout=90.0,
        )
        return result

    async def get_artist_details(self, artist_id: int) -> Dict:
        """Fetch artist with details."""
        result = await self.request(
            "GET",
            f"/api/v1/artist/{artist_id}",
            use_cache=True,
            cache_key=f"lidarr_artist_{artist_id}"
        )
        return result

    async def delete_artist(
        self,
        artist_id: int,
        delete_files: bool = True
    ) -> Dict:
        """Delete artist and optionally remove files."""
        params = f"?deleteFiles={str(delete_files).lower()}"
        result = await self.request(
            "DELETE",
            f"/api/v1/artist/{artist_id}{params}",
            use_cache=False
        )
        return result

    # ── Pass 16a: Library-Add extensions ─────────────────────────────────
    async def lookup_artist(self, term: str) -> List[Dict]:
        """MusicBrainz-backed artist lookup (term=name OR mbid:UUID)."""
        return await self.request(
            "GET", "/api/v1/artist/lookup",
            params={"term": term}, use_cache=False,
        )

    async def list_root_folders(self) -> List[Dict]:
        return await self.request(
            "GET", "/api/v1/rootfolder",
            use_cache=True, cache_key="lidarr_root_folders",
        )

    async def list_quality_profiles(self) -> List[Dict]:
        return await self.request(
            "GET", "/api/v1/qualityprofile",
            use_cache=True, cache_key="lidarr_quality_profiles",
        )

    async def list_metadata_profiles(self) -> List[Dict]:
        """Lidarr-specific — controls which release types/quality get
        downloaded (Studio Album / EP / Single etc.)."""
        return await self.request(
            "GET", "/api/v1/metadataprofile",
            use_cache=True, cache_key="lidarr_metadata_profiles",
        )

    async def list_tags(self) -> List[Dict]:
        return await self.request(
            "GET", "/api/v1/tag", use_cache=False,
        )

    async def create_tag(self, label: str) -> Dict:
        return await self.request(
            "POST", "/api/v1/tag",
            json_data={"label": label}, use_cache=False,
        )

    async def add_artist(
        self,
        mbid: str,
        artist_name: str,
        root_folder_path: str,
        quality_profile_id: int,
        metadata_profile_id: int,
        monitored: bool = True,
        monitor_option: str = "all",  # "all" | "future" | "missing" | "existing" | "first" | "latest" | "none"
        search_for_missing: bool = True,
        tags: Optional[List[int]] = None,
        monitor_new_items: str = "all",   # "none" for catalog-mode adds
    ) -> Dict:
        """Add an artist to Lidarr by MusicBrainz ID.

        Lidarr's add flow normally needs an artist record from /artist/lookup;
        we synthesise a minimal payload from mbid + name + the user's defaults.
        Lidarr fills in the rest from MusicBrainz on its side.
        """
        payload = {
            "foreignArtistId": mbid,
            "artistName": artist_name,
            "qualityProfileId": quality_profile_id,
            "metadataProfileId": metadata_profile_id,
            "rootFolderPath": root_folder_path,
            "monitored": monitored,
            "monitorNewItems": monitor_new_items,
            "tags": tags or [],
            "addOptions": {
                "monitor": monitor_option,
                "searchForMissingAlbums": search_for_missing,
            },
        }
        return await self.request(
            "POST", "/api/v1/artist",
            json_data=payload, use_cache=False,
        )

    # ── Catalog-mode helpers (SoulSync owns the files, Lidarr is the
    #    passive structure index) ──────────────────────────────────────────
    async def refresh_artist(self, artist_id: int) -> Dict:
        """POST /api/v1/command RefreshArtist — rescans ONE artist incl.
        disk. An API-issued command counts as 'Manual' for the
        rescan-after-refresh setting, which is exactly what catalog mode
        relies on (scheduled refreshes no longer touch the disk)."""
        return await self.request(
            "POST", "/api/v1/command",
            json_data={"name": "RefreshArtist", "artistId": artist_id},
            use_cache=False,
        )

    async def get_command(self, command_id: int) -> Dict:
        """Poll a queued Lidarr command (status: queued/started/completed)."""
        return await self.request(
            "GET", f"/api/v1/command/{command_id}", use_cache=False,
        )

    async def get_trackfiles(self, artist_id: int) -> List[Dict]:
        """Per-file truth (path + size in bytes) for one artist. Unlike the
        aggregated statistics.sizeOnDisk this cannot lie about stale
        state after a refresh — the freshness guard reads THIS before any
        delete."""
        return await self.request(
            "GET", "/api/v1/trackfile",
            params={"artistId": artist_id}, use_cache=False,
        )

    async def test_connection(self) -> Dict:
        return await self.request(
            "GET", "/api/v1/system/status", use_cache=False,
        )


# ── Pass 16a: tag helper ──────────────────────────────────────────────────
async def ensure_curatarr_tag(client) -> Optional[int]:
    """Look up — and if missing, create — the 'curatarr' tag in any arr.

    Works for Sonarr/Radarr/Lidarr (they all have list_tags + create_tag
    helpers with the same shape after this pass). Returns the tag id, or
    None on failure (caller proceeds without tag).
    """
    try:
        tags = await client.list_tags()
        for t in tags:
            if (t.get("label") or "").lower() == "curatarr":
                return t.get("id")
        # Not found → create it
        new = await client.create_tag("curatarr")
        return new.get("id")
    except Exception as e:
        logger.warning("ensure_curatarr_tag failed for %s: %s",
                       client.service_type, e)
        return None


# Factory for creating clients
async def create_clients(config: Dict) -> Dict[str, MediaService]:
    """Create all configured API clients."""
    clients = {}

    if "plex" in config:
        clients["plex"] = PlexClient(
            config["plex"]["url"],
            config["plex"]["token"]
        )

    if "tautulli" in config:
        clients["tautulli"] = TautulliClient(
            config["tautulli"]["url"],
            config["tautulli"]["api_key"]
        )

    if "radarr" in config:
        clients["radarr"] = RadarrClient(
            config["radarr"]["url"],
            config["radarr"]["api_key"]
        )

    if "sonarr" in config:
        clients["sonarr"] = SonarrClient(
            config["sonarr"]["url"],
            config["sonarr"]["api_key"]
        )

    if "lidarr" in config:
        clients["lidarr"] = LidarrClient(
            config["lidarr"]["url"],
            config["lidarr"]["api_key"]
        )

    return clients
