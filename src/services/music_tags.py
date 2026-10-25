"""Plex's own moods and styles on the music, and what the plays sounded like.

Track genres were a comma string (Plex or Last.fm top tags) and nothing
said how the music feels, so the Wrap-Up could not tell "morning intensity,
late-night wind-down". Plex tags artists and albums with styles and moods,
and tracks with moods once sonic analysis ran.

**The walk** (custodian task ``music_tags``, no model) reads them the way
Plex's own filter bar does: for each music section, kind and level it lists
the tags (``/library/sections/{key}/{kind}?type=N``), then the items under
each tag. The plain artist and album listings are read too, for servers
that put ``Mood`` / ``Style`` on the items themselves. A section's rows are
replaced only by a walk that read all of it.

**The plays** (``played_moods``): a track takes its own moods, else its
album's, else its artist's; styles come from the album, else the artist.
The track's album and artist come from the music walk's index
(services/lyrics, ``track_lyrics``): by the track's ratingKey, else by the
play's artist and album names.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime
from typing import Optional

from src.database.connection import get_db_session

logger = logging.getLogger(__name__)

_PAGE = 400
_TYPES = {"artist": 8, "album": 9, "track": 10}
# Plex has no styles on tracks (checked on a real server: the style list for
# type 10 is empty; moods have ~300 tags on artists, albums and tracks).
_WALK = (("mood", "artist"), ("mood", "album"), ("mood", "track"),
         ("style", "artist"), ("style", "album"))
_FIELD = {"mood": "Mood", "style": "Style"}


def _tags_of(item: dict, kind: str) -> list:
    raw = item.get(_FIELD[kind]) or []
    if isinstance(raw, dict):
        raw = [raw]
    return [str(t.get("tag")).strip() for t in raw if isinstance(t, dict) and t.get("tag")]


async def _page_all(client, base: str, headers: dict, path: str, params: dict) -> Optional[list]:
    """Every item of one listing, or None when a page could not be read."""
    out, start = [], 0
    while True:
        try:
            r = await client.get(f"{base}{path}", params=params,
                                 headers={**headers, "X-Plex-Container-Start": str(start),
                                          "X-Plex-Container-Size": str(_PAGE)})
        except Exception as e:  # noqa: BLE001
            logger.info("[music tags] %s failed: %s", path, e)
            return None
        if r.status_code != 200:
            logger.info("[music tags] %s: HTTP %s", path, r.status_code)
            return None
        mc = r.json().get("MediaContainer", {}) or {}
        items = mc.get("Metadata") or mc.get("Directory") or []
        out.extend(items)
        if len(items) < _PAGE:
            return out
        start += _PAGE


async def walk_section(client, base: str, headers: dict, sec: str) -> Optional[list]:
    """[(level, item_key, kind, tag)] for one section, or None when any
    listing could not be read (the stored rows then stay)."""
    found: set = set()
    for level in ("artist", "album"):
        items = await _page_all(client, base, headers, f"/library/sections/{sec}/all",
                                {"type": _TYPES[level]})
        if items is None:
            return None
        for it in items:
            rk = it.get("ratingKey")
            for kind in _FIELD:
                for tag in _tags_of(it, kind):
                    found.add((level, str(rk), kind, tag))
    for kind, level in _WALK:
        tags = await _page_all(client, base, headers, f"/library/sections/{sec}/{kind}",
                               {"type": _TYPES[level]})
        if tags is None:
            return None
        for t in tags:
            name, key = (t.get("title") or "").strip(), t.get("key")
            if not name or key is None:
                continue
            items = await _page_all(client, base, headers, f"/library/sections/{sec}/all",
                                    {"type": _TYPES[level], kind: str(key).rsplit("=", 1)[-1]})
            if items is None:
                return None
            for it in items:
                if it.get("ratingKey") is not None:
                    found.add((level, str(it["ratingKey"]), kind, name))
    return sorted(found)


def store_section(sec: str, rows: list, now: Optional[datetime] = None) -> int:
    """Replace one section's tags. Blocking — call via ``run_write``."""
    from src.database.models import MusicTag
    now = now or datetime.utcnow()
    with get_db_session() as db:
        db.query(MusicTag).filter(MusicTag.section_key == sec).delete(synchronize_session=False)
        db.add_all([MusicTag(section_key=sec, level=lv, item_key=k, kind=kd, tag=t[:128],
                             seen_at=now) for lv, k, kd, t in rows])
    return len(rows)


async def run_music_tags(task=None) -> bool:
    """Custodian task: walk every music section's moods and styles."""
    import httpx
    from src.config import settings
    from src.database.runner import run_read, run_write
    from src.services.http_client import tls_context
    from src.services.lyrics import _discover_music_sections, _music_sections
    base, token = settings.effective_plex_url, settings.effective_plex_token
    if not base or not token:
        return True
    headers = {"Accept": "application/json", "X-Plex-Token": token,
               "X-Plex-Client-Identifier": settings.PLEX_CLIENT_ID}
    ok = True
    async with httpx.AsyncClient(verify=tls_context(), timeout=30) as client:
        sections = await run_read(_music_sections) or await _discover_music_sections(
            client, base, headers)
        for sec, title in sections:
            rows = await walk_section(client, base, headers, str(sec))
            if rows is None:
                ok = False
                logger.warning("[music tags] section %s (%s) not fully read — kept as it was",
                               sec, title)
                continue
            n = await run_write(store_section, str(sec), rows)
            kinds = Counter((lv, kd) for lv, _, kd, _ in rows)
            logger.info("[music tags] section %s (%s): %d tags %s", sec, title, n,
                        dict(sorted((f"{lv}_{kd}", c) for (lv, kd), c in kinds.items())))
    return ok


# ── reading ─────────────────────────────────────────────────────────────────

def _norm(s: Optional[str]) -> str:
    return " ".join((s or "").lower().split())


def _parents(keys: list, names: Optional[dict] = None) -> dict:
    """{play key: (album key, artist key)} from the music walk's index: by
    the track's ratingKey, else by the play's artist and album names — most
    track events in Plex's history no longer carry a ratingKey, so many
    stored plays point at no track the index knows."""
    from src.services import lyrics
    if not keys:
        return {}
    out: dict = {}
    try:
        con = lyrics._connect()
    except Exception as e:  # noqa: BLE001 - no index, no parents
        logger.info("[music tags] no track index: %s", e)
        return {}
    try:
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            q = ("SELECT plex_rating_key, album_key, artist_key FROM track_lyrics "
                 f"WHERE plex_rating_key IN ({','.join('?' * len(chunk))})")  # nosec B608 - placeholders only
            for rk, al, ar in con.execute(q, chunk):
                out[str(rk)] = (str(al) if al else None, str(ar) if ar else None)
        missing = [k for k in keys if k not in out and (names or {}).get(k)]
        if missing:
            albums, artists = {}, {}
            for al, ar, artist, album in con.execute(
                    "SELECT album_key, artist_key, artist, album FROM track_lyrics "
                    "WHERE album_key IS NOT NULL GROUP BY album_key"):
                albums.setdefault((_norm(artist), _norm(album)), (str(al), str(ar) if ar else None))
                if ar:
                    artists.setdefault(_norm(artist), str(ar))
            for ak, name in con.execute("SELECT artist_key, name FROM plex_artists"):
                artists.setdefault(_norm(name), str(ak))
            for k in missing:
                artist, album = names[k]
                hit = albums.get((_norm(artist), _norm(album)))
                if hit:
                    out[k] = hit
                elif artists.get(_norm(artist)):
                    out[k] = (None, artists[_norm(artist)])
    finally:
        con.close()
    return out


def tags_for_tracks(db, keys: list, names: Optional[dict] = None) -> dict:
    """{play key: {"mood": [...], "style": [...]}} by the track > album >
    artist rule. ``names``: {play key: (artist, album)} for plays whose key
    the index does not know. Plays with neither are left out."""
    from src.database.models import MusicTag
    keys = [str(k) for k in keys if k]
    parents = _parents(keys, names)
    want = set(keys) | {p for ps in parents.values() for p in ps if p}
    tags: dict = defaultdict(lambda: defaultdict(list))
    want_l = list(want)
    for i in range(0, len(want_l), 500):
        for lv, k, kd, t in (db.query(MusicTag.level, MusicTag.item_key, MusicTag.kind, MusicTag.tag)
                             .filter(MusicTag.item_key.in_(want_l[i:i + 500]))):
            tags[(lv, k)][kd].append(t)
    out = {}
    for k in keys:
        album, artist = parents.get(k, (None, None))
        mood = (tags.get(("track", k), {}).get("mood") or tags.get(("album", album), {}).get("mood")
                or tags.get(("artist", artist), {}).get("mood") or [])
        style = (tags.get(("album", album), {}).get("style")
                 or tags.get(("artist", artist), {}).get("style") or [])
        if mood or style:
            out[k] = {"mood": sorted(set(mood)), "style": sorted(set(style))}
    return out
