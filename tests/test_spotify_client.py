import asyncio
import time
from unittest.mock import patch, MagicMock, AsyncMock

from src.services.spotify_client import _batch_get, _PERSIST_THRESHOLD

def test_persistent_backoff_on_retry():
    # Mock time and sleep to avoid delays
    with patch("src.services.spotify_client.time.monotonic", return_value=100.0), \
         patch("src.services.spotify_client.asyncio.sleep", new_callable=AsyncMock), \
         patch("src.services.spotify_client._persist_backoff") as mock_persist, \
         patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:

        # Scenario 1: First request fails with short backoff (30s),
        # retry request fails with short backoff (30s)
        # Should NOT persist
        short_429 = MagicMock()
        short_429.status_code = 429
        short_429.headers = {"retry-after": "30"}

        mock_get.side_effect = [short_429, short_429]

        loop = asyncio.get_event_loop()
        results, rate_limited = loop.run_until_complete(
            _batch_get("http://fake", ["id1"], "token")
        )

        assert rate_limited is True
        mock_persist.assert_not_called()

        # Scenario 2: First request fails with short backoff (30s),
        # retry request fails with LONG backoff (exceeds threshold)
        # SHOULD persist
        long_429 = MagicMock()
        long_429.status_code = 429
        # set retry-after way above threshold
        long_429.headers = {"retry-after": str(_PERSIST_THRESHOLD + 100)}

        # reset mocks
        mock_get.reset_mock()
        mock_get.side_effect = [short_429, long_429]

        # Reset backoff state
        import src.services.spotify_client as sc
        sc._backoff_until = 0.0

        results, rate_limited = loop.run_until_complete(
            _batch_get("http://fake", ["id1"], "token")
        )

        assert rate_limited is True
        mock_persist.assert_called_once_with(float(_PERSIST_THRESHOLD + 100) + 2.0)
