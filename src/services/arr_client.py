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
        cache_key: Optional[str] = None
    ) -> Dict:
        """Execute API request with rate limiting and caching."""

        # Check cache
        if use_cache and cache_key:
            cached_value = self._get_cached(cache_key)
            if cached_value is not None:
                logger.debug(f"Cache hit: {cache_key}")
                return cached_value

        await self.rate_limiter.acquire()

        try:
            headers = self._get_headers()
            url = f"{self.base_url}{endpoint}"

            async with self.session.request(
                method,
                url,
                json=json_data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 429:
                    # Rate limited, wait and retry
                    retry_after = int(resp.headers.get('Retry-After', 60))
                    logger.warning(f"Rate limited, retrying after {retry_after}s")
                    await asyncio.sleep(retry_after)
                    return await self.request(method, endpoint, json_data, use_cache, cache_key)

                if resp.status >= 400:
                    error_text = await resp.text()
                    raise Exception(f"API error {resp.status}: {error_text}")

                result = await resp.json()

                # Cache result
                if use_cache and cache_key:
                    self._cache_set(cache_key, result)

                return result

        except asyncio.TimeoutError:
            logger.error(f"Request timeout: {endpoint}")
            raise

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
        params = f"?action=get_plays&limit={limit}"
        if user_id:
            params += f"&user_id={user_id}"

        result = await self.request(
            "GET",
            f"/api/v2{params}&apikey={self.api_key}",
            use_cache=True,
            cache_key=f"tautulli_plays_{user_id}_{days}"
        )
        return result.get("response", {}).get("data", [])

    async def get_media_info(self, media_key: str) -> Dict:
        """Get detailed info for a media item."""
        params = f"?action=get_media_info&rating_key={media_key}"
        result = await self.request(
            "GET",
            f"/api/v2{params}&apikey={self.api_key}",
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
        """Fetch all movies."""
        result = await self.request(
            "GET",
            "/api/v3/movie",
            use_cache=True,
            cache_key="radarr_movies"
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
        """Fetch all series."""
        result = await self.request(
            "GET",
            "/api/v3/series",
            use_cache=True,
            cache_key="sonarr_series"
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
        """Fetch all artists."""
        result = await self.request(
            "GET",
            "/api/v1/artist",
            use_cache=True,
            cache_key="lidarr_artists"
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


if __name__ == "__main__":
    # Example usage
    async def demo():
        config = {
            "radarr": {
                "url": "http://radarr.local:7878",
                "api_key": "your_api_key"
            }
        }

        clients = await create_clients(config)

        async with clients["radarr"]:
            movies = await clients["radarr"].get_movies()
            print(f"Found {len(movies)} movies")


    asyncio.run(demo())
