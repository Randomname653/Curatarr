"""The music index on Jellyfin (docs/JELLYFIN_DESIGN.md, phase 6).

``services/lyrics.py`` keeps one SQLite of everything the music walk learns:
tracks with sizes and lyrics, artists with MusicBrainz ids and footprints.
It is the music index the deletion candidates (without Lidarr), the Music
page, the Wrap-Up's moods and the curator's lyrics profiles read. Its walk
(``lyrics.sync_lyrics``) takes callables for listing and fetching; this
module hands it Jellyfin's, with items in the shape the Plex listing has
(ratingKey, parent/grandparent keys and titles, Media/Part sizes, a lyric
stream), so the index and everything reading it stay as they are.

Lyrics come from ``GET /Audio/{id}/Lyrics`` (10.9+), for tracks whose
MediaStreams list a ``Lyric`` stream (an .lrc / .txt sidecar or embedded
tags Jellyfin read).
"""
from __future__ import annotations

import logging
from typing import Optional

from src.services.media_server.base import normalize_jellyfin_id

logger = logging.getLogger(__name__)

LYRIC_PREFIX = "jf-lyrics:"


def _unix(iso) -> Optional[int]:
    from src.services.media_server.jellyfin_tech import _unix as unix
    return unix(iso)


def plex_shaped_track(raw: dict) -> Optional[dict]:
    """A Jellyfin Audio item in the Plex track listing's shape."""
    key = normalize_jellyfin_id(raw.get("Id"))
    if not key or raw.get("Type") != "Audio":
        return None
    arts = raw.get("AlbumArtists") or raw.get("ArtistItems") or []
    artist_key = normalize_jellyfin_id(arts[0].get("Id")) if arts else None
    parts, has_lyric = [], bool(raw.get("HasLyrics"))
    for src in raw.get("MediaSources") or []:
        parts.append({"size": int(src.get("Size") or 0), "file": src.get("Path")})
        has_lyric = has_lyric or any(st.get("Type") == "Lyric" for st in src.get("MediaStreams") or [])
    if parts and has_lyric:
        parts[0]["Stream"] = [{"streamType": 4, "key": LYRIC_PREFIX + key, "codec": "txt"}]
    return {
        "ratingKey": key, "type": "track", "title": raw.get("Name"),
        "grandparentRatingKey": artist_key or "", "parentRatingKey": normalize_jellyfin_id(raw.get("AlbumId")) or "",
        "grandparentTitle": raw.get("AlbumArtist") or (arts[0].get("Name") if arts else None),
        "parentTitle": raw.get("Album"), "duration": int(raw.get("RunTimeTicks") or 0) // 10_000 or None,
        "addedAt": _unix(raw.get("DateCreated")), "parentYear": raw.get("ProductionYear"),
        "Media": [{"Part": parts}] if parts else [],
    }


def plex_shaped_artist(raw: dict) -> Optional[dict]:
    key = normalize_jellyfin_id(raw.get("Id"))
    if not key:
        return None
    mbid = (raw.get("ProviderIds") or {}).get("MusicBrainzArtist")
    return {"ratingKey": key, "type": "artist", "title": raw.get("Name"),
            "addedAt": _unix(raw.get("DateCreated")),
            "Guid": [{"id": f"mbid://{mbid}"}] if mbid else []}


def _music_library_keys() -> list:
    from src.database.connection import get_db_session
    from src.database.models import LibraryConfig
    with get_db_session() as db:
        return [c.plex_section_key for c in
                db.query(LibraryConfig).filter(LibraryConfig.media_category == "music").all()]


async def run_music_index(task=None, *, adapter=None, db_path=None) -> bool:
    """The custodian's lyrics / music-index task on a Jellyfin install: the
    mapped music libraries, else every music library Jellyfin has. True =
    the walk finished; False = the lyrics budget ran out mid-walk."""
    from src.database.runner import run_read
    from src.services import lyrics
    from src.services.media_server import get_adapter
    from src.services.media_server.jellyfin_adapter import JellyfinError
    adapter = adapter or get_adapter()
    libs = {lib.key: lib for lib in await adapter.libraries() if lib.kind == "music"}
    mapped = await run_read(_music_library_keys)
    chosen = [libs[k] for k in mapped if k in libs] or list(libs.values())
    if not chosen:
        logger.info("[lyrics] Jellyfin has no music library - nothing to collect")
        return True

    async def walk(sec_key: str, kind: str, shape):
        try:
            return [x async for r in adapter.raw_items(libs[sec_key], kinds=(kind,)) if (x := shape(r))]
        except JellyfinError as e:
            logger.warning("[lyrics] Jellyfin %s listing of %s broke: %s", kind, libs[sec_key].title, e)
            return None

    async def list_tracks(sec_key: str):
        return await walk(sec_key, "track", plex_shaped_track)

    async def list_artists(sec_key: str):
        return await walk(sec_key, "artist", plex_shaped_artist)

    async def fetch_text(stream_key: str):
        try:
            return await adapter.lyrics(stream_key.removeprefix(LYRIC_PREFIX))
        except JellyfinError as e:
            logger.info("[lyrics] %s not fetched: %s", stream_key, e)
            return None

    res = await lyrics.sync_lyrics([(lib.key, lib.title) for lib in chosen], list_tracks, fetch_text,
                                   list_artists=list_artists, db_path=db_path, task=task)
    logger.info("[lyrics] Jellyfin music index: %s", res)
    return bool(res.get("done"))


def tag_rows(level: str, raws: list) -> list:
    """``music_tags`` rows from one level of a Jellyfin walk. Plex has moods
    and styles; Jellyfin has Genres (from the file tags and metadata
    providers) and Tags (free tags, where MOOD file tags land). Genres read
    as styles on artists and albums, Tags as moods on every level."""
    out = set()
    for raw in raws:
        key = normalize_jellyfin_id(raw.get("Id"))
        if not key:
            continue
        if level in ("artist", "album"):
            for g in raw.get("Genres") or []:
                if str(g).strip():
                    out.add((level, key, "style", str(g).strip()))
        for t in raw.get("Tags") or []:
            if str(t).strip():
                out.add((level, key, "mood", str(t).strip()))
    return sorted(out)


async def run_music_tags(task=None, *, adapter=None) -> bool:
    """The custodian's ``music_tags`` task on a Jellyfin install. A library's
    rows are replaced only by a walk that read all of it."""
    from src.database.runner import run_write
    from src.services.media_server import get_adapter
    from src.services.media_server.jellyfin_adapter import JellyfinError
    from src.services.music_tags import store_section
    adapter = adapter or get_adapter()
    ok = True
    for lib in [lib for lib in await adapter.libraries() if lib.kind == "music"]:
        rows = []
        try:
            for level, kind in (("artist", "artist"), ("album", "album"), ("track", "track")):
                rows += tag_rows(level, [r async for r in adapter.raw_items(lib, kinds=(kind,))])
        except JellyfinError as e:
            ok = False
            logger.warning("[music tags] Jellyfin library %s not fully read - kept as it was: %s", lib.title, e)
            continue
        n = await run_write(store_section, lib.key, rows)
        logger.info("[music tags] Jellyfin library %s: %d tags", lib.title, n)
    return ok
