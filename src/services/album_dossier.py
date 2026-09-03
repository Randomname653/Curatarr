"""
Curatarr — Album dossier: album-level evidence for music debates.

Music proposals and chat anchors are ARTIST-level; albums only ever
appeared as bare names inside debates (the Bomber discussion argued
"definitive version" with zero album facts). Probe-validated design
(2026-07-08, Billx 'Punishment'): everything is deterministic — no LLM.

  Type        Lidarr albumType + secondaryTypes (Compilation/Live/Remix —
              the "definitive version" signal), release year, monitored
  Stock       trackFileCount/trackCount + sizeOnDisk
  Community   Last.fm album.getInfo listeners/playcount + tags, plus the
              album's ListenBrainz rank within the artist's own catalogue
              by global listens (one token-authed call, 30 d-cached per
              artist — needs LISTENBRAINZ_TOKEN)
  Owner       the album's TRACKS matched against the listening history:
              "4/12 tracks played, 17 plays — top: Sax (13)" (the probe
              case: 2,284 artist plays but barely THIS album)
  Styles      per-work Discogs styles from the offline snapshot, if any

Two LAN calls + one Last.fm call + one ListenBrainz call per dossier;
used on demand when a chat or discussion names an album of the active
artist.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—−"})


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower().translate(_DASHES)).strip()


async def _lidarr_artist_albums(artist_name: str) -> tuple[Optional[dict], list]:
    """(artist_obj, albums) from Lidarr, or (None, [])."""
    base = (settings.LIDARR_URL or "").rstrip("/")
    key = settings.LIDARR_API_KEY
    if not base or not key:
        return None, []
    try:
        # 60 s, not 20: the full /artist payload is 15-30 MB (see the
        # ARCHITECTURE "ARR collect needs >=60s" invariant) — a cold Lidarr
        # takes >20 s and the dossier silently vanished.
        async with httpx.AsyncClient(timeout=60) as c:
            artists = (await c.get(f"{base}/api/v1/artist",
                                   params={"apikey": key})).json()
            want = _norm(artist_name)
            artist = next((a for a in artists
                           if _norm(a.get("artistName")) == want), None)
            if not artist:
                return None, []
            albums = (await c.get(f"{base}/api/v1/album",
                                  params={"apikey": key,
                                          "artistId": artist["id"]})).json()
            return artist, albums or []
    except Exception as e:
        logger.debug("[album] lidarr fetch failed for %r: %s", artist_name, e)
        return None, []


def find_album(albums: list, album_title: str) -> Optional[dict]:
    want = _norm(album_title)
    return next((a for a in albums if _norm(a.get("title")) == want), None)


async def build_album_dossier(artist_name: str, album_title: str) -> Optional[str]:
    """One evidence block for a single album, or None when the album isn't
    in Lidarr under this artist. Deterministic — every number has a source."""
    artist, albums = await _lidarr_artist_albums(artist_name)
    if not artist:
        return None
    alb = find_album(albums, album_title)
    if not alb:
        return None

    lines = []
    year = (alb.get("releaseDate") or "")[:4]
    a_type = alb.get("albumType") or "Album"
    secondary = [s for s in (alb.get("secondaryTypes") or []) if s]
    type_str = a_type + (f" ({', '.join(secondary)})" if secondary else "")
    lines.append(f"[ALBUM DOSSIER] '{alb.get('title')}' by "
                 f"{artist.get('artistName')} ({year or '?'}) — {type_str}"
                 + ("" if alb.get("monitored") else ", not monitored"))

    st = alb.get("statistics") or {}
    files, total_t = st.get("trackFileCount") or 0, st.get("trackCount") or 0
    size = (st.get("sizeOnDisk") or 0) / 1e9
    lines.append(f"On disk: {files}/{total_t} tracks, {size:.2f} GB")

    # album tracklist -> owner plays (LAN + one history query)
    tnames: list[str] = []
    try:
        base = settings.LIDARR_URL.rstrip("/")
        async with httpx.AsyncClient(timeout=15) as c:
            tracks = (await c.get(f"{base}/api/v1/track",
                                  params={"apikey": settings.LIDARR_API_KEY,
                                          "albumId": alb["id"]})).json()
        tnames = [t.get("title") for t in tracks if t.get("title")]
    except Exception as e:
        logger.debug("[album] tracklist failed: %s", e)
    if tnames:
        try:
            from sqlalchemy import func
            from src.database.connection import get_db_session
            from src.database.models import WatchHistoryEntry as W
            with get_db_session() as db:
                rows = (db.query(W.title, func.count(W.id))
                        .filter(W.media_type == "music",
                                W.series_title == artist.get("artistName"),
                                W.title.in_(tnames))
                        .group_by(W.title).all())
            played = {t: n for t, n in rows}
            if played:
                top = sorted(played.items(), key=lambda x: -x[1])[:3]
                top_s = ", ".join(f"{t} ({n})" for t, n in top)
                lines.append(f"Owner plays: {len(played)}/{len(tnames)} tracks "
                             f"played, {sum(played.values())} plays — top: {top_s}")
            else:
                lines.append(f"Owner plays: NONE of the {len(tnames)} tracks "
                             "appear in the listening history")
        except Exception as e:
            logger.debug("[album] history match failed: %s", e)

    # Last.fm community numbers + tags
    if settings.LASTFM_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                lf = (await c.get("https://ws.audioscrobbler.com/2.0/", params={
                    "method": "album.getinfo",
                    "api_key": settings.LASTFM_API_KEY,
                    "artist": artist.get("artistName"),
                    "album": alb.get("title"), "format": "json"})).json()
            a = lf.get("album") or {}
            bits = []
            if a.get("listeners"):
                bits.append(f"{int(a['listeners']):,} listeners")
            if a.get("playcount"):
                bits.append(f"{int(a['playcount']):,} plays")
            tags = [t.get("name") for t in (a.get("tags") or {}).get("tag", [])][:6]
            if bits or tags:
                line = "Community (Last.fm): " + ", ".join(bits)
                if tags:
                    line += f"; tags: {', '.join(tags)}"
                lines.append(line)
        except Exception as e:
            logger.debug("[album] lastfm failed: %s", e)

    # ListenBrainz: rank THIS album inside the artist's own catalogue by
    # global listens. Resolved independently of the judge's raw:music:*
    # doc (the dossier doesn't route through it); fetch_artist_popularity's
    # own 30 d cache keeps repeat dossiers to one network call per artist.
    mbid = artist.get("foreignArtistId")
    if not mbid:
        try:
            from src.cache.metadata_cache import MetadataCache
            _c = MetadataCache()
            _hit = _c.get_cache(
                f"mb:artist:{(artist.get('artistName') or '')[:60].lower()}")
            _c.close()
            if _hit and isinstance(_hit.get("response"), dict):
                mbid = _hit["response"].get("mbid")
        except Exception as e:
            logger.debug("[album] mb:artist fallback failed: %s", e)
    if mbid:
        try:
            from src.services.listenbrainz import (fetch_artist_popularity,
                                                   norm_album_title)
            lb = await fetch_artist_popularity(mbid)
            if lb is None:
                pass  # transient — silent, like every other absent-data line
            elif not lb:
                # definitive: LB tracks nothing for this artist — distinct
                # from "merely obscure" (below the digest cap) on purpose
                lines.append("Community (ListenBrainz): artist has no "
                             "release groups tracked")
            else:
                want = norm_album_title(alb.get("title"))
                pool = lb.get("albums") or []
                idx = next((i for i, a in enumerate(pool)
                            if a.get("norm_name") == want), None)
                if idx is not None:
                    a = pool[idx]
                    lines.append(
                        f"Community (ListenBrainz): #{idx + 1} of "
                        f"{lb['n_release_groups']} releases by global listens "
                        f"— {a['listens']:,} listens, {a['listeners']:,} listeners")
                else:
                    lines.append(
                        f"Community (ListenBrainz): outside the top {len(pool)} "
                        f"of {lb['n_release_groups']} releases by global listens")
        except Exception as e:
            logger.debug("[album] listenbrainz rank failed: %s", e)

    # per-work Discogs styles from the offline snapshot
    try:
        import json as _json
        import sqlite3
        from src.services.discogs_offline import STYLES_DB_PATH
        if STYLES_DB_PATH.exists():
            con = sqlite3.connect(str(STYLES_DB_PATH))
            row = con.execute(
                "SELECT styles, year FROM master_styles WHERE artist_norm=? "
                "AND title LIKE ? LIMIT 1",
                (_norm(artist.get("artistName")), alb.get("title"))).fetchone()
            con.close()
            if row and row[0]:
                styles = _json.loads(row[0])
                if styles:
                    lines.append(f"Styles (Discogs): {', '.join(styles[:8])}")
    except Exception as e:
        logger.debug("[album] discogs styles failed: %s", e)

    # SoulSync (LAN neighbour): aggregated per-album metadata from its
    # enrichment workers — genres/style/tags/label fill in over time as the
    # workers progress; silently absent when unconfigured or unreachable.
    try:
        from src.services.soulsync_client import album_info
        ss = await album_info(artist.get("artistName"), alb.get("title"))
        if ss:
            bits = []
            # join only real lists — a raw JSON string ('["rock", ...]')
            # would otherwise render character-wise
            if isinstance(ss.get("genres"), list) and ss["genres"]:
                bits.append("genres: " + ", ".join(map(str, ss["genres"][:6])))
            if ss.get("style"):
                bits.append(f"style: {ss['style']}")
            if isinstance(ss.get("lastfm_tags"), list) and ss["lastfm_tags"]:
                bits.append("tags: " + ", ".join(map(str, ss["lastfm_tags"][:6])))
            if ss.get("label"):
                bits.append(f"label: {ss['label']}")
            if ss.get("record_type"):
                bits.append(str(ss["record_type"]))
            if bits:
                lines.append("SoulSync: " + "; ".join(bits))
    except Exception as e:
        logger.debug("[album] soulsync failed: %s", e)

    return "\n".join(lines)


def detect_album_in_message(albums: list, message: str) -> Optional[dict]:
    """The longest album title of the active artist contained verbatim
    (normalized, word-bounded) in the user's message, or None. Mirrors the
    library-title scan; >=4 chars to dodge one-word collisions."""
    msg = f" {_norm(message)} "
    best = None
    for a in albums:
        n = _norm(a.get("title"))
        if len(n) >= 4 and f" {n} " in msg:
            if best is None or len(n) > len(_norm(best.get("title"))):
                best = a
    return best
