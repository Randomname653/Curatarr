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

from typing import Optional

from src.config import settings


def lidarr_configured() -> bool:
    return bool(settings.LIDARR_URL and settings.LIDARR_API_KEY)


def plex_music_indexed() -> bool:
    """True once the Plex music index holds at least one artist."""
    try:
        from src.services.lyrics import lyrics_coverage
        return (lyrics_coverage().get("artists_indexed") or 0) > 0
    except Exception:
        return False


def music_service() -> Optional[str]:
    if lidarr_configured():
        return "lidarr"
    if plex_music_indexed():
        return "plex"
    return None
