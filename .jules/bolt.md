## 2024-05-18 - Concurrent SoulSync Artist Info fetching
**Learning:** Found sequential API calls to `soulsync_client.artist_info` inside `_get_music_neighbors` in `src/services/recommendations_engine.py` (which were capped at 5 artists but still sequential).
**Action:** Changed this to parallel fetch using `asyncio.gather`. Be careful to handle `return_exceptions=True` since it makes HTTP requests that could fail individually without bringing down the others.
