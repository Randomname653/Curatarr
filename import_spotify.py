"""
Curatarr — Spotify History Importer

Reads Streaming_History_Audio_*.json files from a directory and bulk-inserts
them into the watch_history table as media_type="music" entries.

Usage:
    python import_spotify.py

Configure TARGET_USER_ID and SPOTIFY_JSON_DIR at the bottom before running.
"""

import os
import json
import glob
import logging
from datetime import datetime

from src.database.connection import get_db_session
from src.database.models import WatchHistoryEntry, User

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BATCH_SIZE   = 5000    # rows per bulk-insert
MIN_MS_PLAYED = 30_000  # skip plays shorter than 30 s (accidental taps / skips)


def _parse_ts(ts_str: str) -> datetime:
    """Parse Spotify ISO8601 timestamp → UTC datetime."""
    try:
        return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return datetime.utcnow()


def import_spotify_history(user_id: int, directory: str) -> None:
    # ── 1. Validate user ──────────────────────────────────────────────────────
    with get_db_session() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            logger.error("User ID %d does not exist.", user_id)
            return
        plex_user_id = user.plex_user_id

    # ── 2. Find JSON files ────────────────────────────────────────────────────
    pattern    = os.path.join(directory, "Streaming_History_Audio_*.json")
    json_files = sorted(glob.glob(pattern))
    if not json_files:
        logger.warning("No files found matching: %s", pattern)
        return
    logger.info("Found %d Spotify file(s). Starting import…", len(json_files))

    # ── 3. Load existing (plex_item_id, viewed_at) pairs to avoid duplicates ─
    # Keyed as "uri|YYYY-MM-DDTHH:MM:SS" for fast set lookup.
    logger.info("Loading existing music history for dedup check…")
    with get_db_session() as db:
        existing = {
            f"{r.plex_item_id}|{r.viewed_at.strftime('%Y-%m-%dT%H:%M:%S')}"
            for r in db.query(
                WatchHistoryEntry.plex_item_id,
                WatchHistoryEntry.viewed_at,
            ).filter(
                WatchHistoryEntry.user_id == user_id,
                WatchHistoryEntry.media_type == "music",
            ).all()
        }
    logger.info("  → %d existing rows loaded.", len(existing))

    # ── 4. Parse & insert ─────────────────────────────────────────────────────
    total_imported = 0
    total_skipped  = 0
    total_dupes    = 0
    buffer: list   = []

    with get_db_session() as db:
        for file_path in json_files:
            logger.info("Processing: %s", os.path.basename(file_path))

            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            for item in data:
                ms_played = item.get("ms_played", 0)

                # Skip podcasts / audiobooks
                if item.get("episode_name") or item.get("audiobook_title"):
                    total_skipped += 1
                    continue

                # Skip short plays (fat-finger / auto-play skips)
                if ms_played < MIN_MS_PLAYED:
                    total_skipped += 1
                    continue

                track_name  = item.get("master_metadata_track_name")
                artist_name = item.get("master_metadata_album_artist_name")
                track_uri   = item.get("spotify_track_uri")

                if not track_name or not artist_name:
                    total_skipped += 1
                    continue

                viewed_at = _parse_ts(item.get("ts", ""))
                dedup_key = f"{track_uri}|{viewed_at.strftime('%Y-%m-%dT%H:%M:%S')}"

                if dedup_key in existing:
                    total_dupes += 1
                    continue

                # Mark as seen so a re-run of the same file is also safe
                existing.add(dedup_key)

                # "trackdone" = played to the end; skipped=False + >= 2 min is also fine
                is_completed = (
                    item.get("reason_end") == "trackdone"
                    or (not item.get("skipped") and ms_played >= 120_000)
                )

                buffer.append({
                    "user_id":        user_id,
                    "plex_user_id":   plex_user_id,
                    # plex_item_id holds the truncated URI as placeholder until
                    # music_matcher.py resolves the real Plex ratingKey
                    "plex_item_id":   (track_uri or "")[:64],
                    "title":          track_name[:512],
                    "series_title":   artist_name[:512],  # artist → series slot
                    "media_type":     "music",
                    "season":         None,
                    "episode":        None,
                    "viewed_at":      viewed_at,
                    "duration_ms":    None,        # not in Spotify export
                    "view_offset_ms": ms_played,
                    "completed":      is_completed,
                    "genres":         None,        # filled by music_matcher enrichment
                    "tmdb_id":        None,
                    "spotify_uri":    (track_uri or "")[:100],  # full URI for matching
                    "source":         "spotify",
                })

                # Flush batch ──────────────────────────────────────────────────
                if len(buffer) >= BATCH_SIZE:
                    db.bulk_insert_mappings(WatchHistoryEntry, buffer)
                    db.commit()
                    total_imported += len(buffer)
                    buffer = []                   # ← clear (was the bug before)
                    logger.info("  … %d rows inserted so far.", total_imported)

        # Flush remaining rows
        if buffer:
            db.bulk_insert_mappings(WatchHistoryEntry, buffer)
            db.commit()
            total_imported += len(buffer)

    logger.info("=== Import complete ===")
    logger.info("  Imported : %d", total_imported)
    logger.info("  Skipped  : %d  (podcasts / plays < 30 s / missing metadata)", total_skipped)
    logger.info("  Duplicates skipped: %d", total_dupes)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    TARGET_USER_ID   = 1
    SPOTIFY_JSON_DIR = "./spotify_data"   # put your JSON files here

    import_spotify_history(user_id=TARGET_USER_ID, directory=SPOTIFY_JSON_DIR)
