"""
Curatarr — manual trigger for the music recommendation pipeline.

Thin wrapper around ``src.services.music_matcher.run_music_pipeline``
for one-shot runs from the command line. The same pipeline runs daily
under the scheduler; use this script when you want to kick it off
immediately (after a fresh Spotify import, after fixing a config, etc.).

Usage::

    python run_pipeline_spotify.py
"""

import asyncio
import logging

from src.services.music_matcher import run_music_pipeline

# Logging must be at INFO so pipeline progress is visible.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


async def trigger():
    print("Starting the music pipeline...")
    # Adjust user_id and lastfm_batch if needed (e.g. 1000 for a larger run).
    result = await run_music_pipeline(user_id=1, lastfm_batch=1000)
    print("Done:", result)


if __name__ == "__main__":
    asyncio.run(trigger())
