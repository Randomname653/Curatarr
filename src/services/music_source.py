"""
Curatarr — where music lives when Lidarr is optional (owner decision 2026-09-15).

Three set-ups must work: Lidarr alone; Lidarr + SoulSync (Modell B, the
owner's until now); no Lidarr at all — Plex (and SoulSync for metadata).
Everything that used to ask "is Lidarr configured?" to decide about MUSIC
asks ``music_service()`` instead:

  'lidarr'  Lidarr is configured — the structure index and the delete
            write-path stay Lidarr's (unchanged behaviour).
  'plex'    no Lidarr, but the Plex music index (src/services/lyrics.py,
            rebuilt by the daily walk) has artists — deletion candidates,
            the Music page and the curator's discography evidence read it,
            deletions go through Plex ('Allow media deletion').
  None      neither — no music curation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from src.config import settings
from src.database.loop_guard import sqlite_connect


def lidarr_configured() -> bool:
    return bool(settings.LIDARR_URL and settings.LIDARR_API_KEY)


def plex_music_indexed() -> bool:
    """True once the Plex music index holds at least one artist.

    A LIMIT 1 probe, not lyrics_coverage(): that ran full-table aggregates
    over every track just to answer yes/no, on the scheduler's loop thread."""
    try:
        from src.services import lyrics
        path = Path(lyrics.LYRICS_DB_PATH)
        if not path.exists():
            return False
        con = sqlite_connect(str(path))
        try:
            return con.execute(
                "SELECT 1 FROM plex_artists WHERE tracks > 0 LIMIT 1").fetchone() is not None
        finally:
            con.close()
    except Exception:
        return False


def music_service() -> Optional[str]:
    if lidarr_configured():
        return "lidarr"
    if plex_music_indexed():
        return "plex"
    return None
