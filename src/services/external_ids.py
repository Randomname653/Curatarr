"""Harvest the external ids the *arr services already hold.

Sonarr and Radarr know each title's IMDb, TVDB and TMDB id. Curatarr asks
them for the library on every sync and keeps none of it: the enrichment path
resolves ids itself, which works for films and Western series and largely
fails for anime, where the profile comes from AniList and never touches TMDB.

The cost of that was invisible until two sources that key on the IMDb id —
OMDb and Wikidata — reported a quarter of the library as unreachable.
Measured on the live install: 2,319 video titles had no IMDb id, 1,833 of
them were sitting in Sonarr WITH one.

Matching is deliberately timid. A wrong id is not a missing fact, it is a
confident lie about a different work — the same failure the corrupt-id
detector and the Fix-match pin exist to undo. So a title is only claimed
when exactly ONE library entry normalises to its name, within the right
media family. Anything ambiguous is left alone for a human or a pin.
"""
import json
import logging

logger = logging.getLogger(__name__)

TV = ("show", "anime")


def _unprefixed(cache_key: str) -> str:
    """Strip the cache-version prefix a stored key carries."""
    from src.cache.metadata_cache import _CACHE_VERSION
    prefix = f"{_CACHE_VERSION}:"
    return cache_key[len(prefix):] if cache_key.startswith(prefix) else cache_key


def _norm(title: str) -> str:
    """Alphanumerics only — the difference between 'Re:ZERO' and 'Re Zero'
    is punctuation nobody agrees on."""
    return "".join(c for c in (title or "").lower() if c.isalnum())


def _index(entries: list) -> dict:
    """{normalised title: ids} for unambiguous names only.

    Every alternate title an *arr knows is indexed too — anime is routinely
    filed under a romanisation the enrichment side never saw. A name that
    two entries share is dropped rather than guessed.
    """
    seen: dict = {}
    for item in entries:
        names = [item.get("title"), item.get("sortTitle")]
        names += [a.get("title") for a in (item.get("alternateTitles") or [])]
        ids = {
            "imdb_id": item.get("imdbId") or None,
            "tvdb_id": item.get("tvdbId") or None,
            "tmdb_id": item.get("tmdbId") or None,
        }
        if not any(ids.values()):
            continue
        for name in filter(None, names):
            key = _norm(name)
            if not key:
                continue
            if key in seen and seen[key] != ids:
                seen[key] = None          # ambiguous — never claim this name
            elif key not in seen:
                seen[key] = ids
    return {k: v for k, v in seen.items() if v}


async def build_arr_id_index() -> dict:
    """{"tv": {...}, "movie": {...}} from whatever *arr is configured."""
    from src.config import settings
    from src.services.arr_client import RadarrClient, SonarrClient

    index = {"tv": {}, "movie": {}}
    # The clients open their HTTP session in __aenter__; constructing one
    # without the context manager leaves it with no session at all.
    if settings.effective_sonarr_url and settings.SONARR_API_KEY:
        try:
            client = SonarrClient(settings.effective_sonarr_url,
                                  settings.SONARR_API_KEY)
            async with client:
                index["tv"] = _index(await client.get_series() or [])
        except Exception as e:
            logger.debug("[external-ids] sonarr unavailable: %s", e)
    if settings.effective_radarr_url and settings.RADARR_API_KEY:
        try:
            client = RadarrClient(settings.effective_radarr_url,
                                  settings.RADARR_API_KEY)
            async with client:
                index["movie"] = _index(await client.get_movies() or [])
        except Exception as e:
            logger.debug("[external-ids] radarr unavailable: %s", e)
    return index


async def harvest(cache, *, limit: int = 0, task=None, should_stop=None) -> dict:
    """Fill missing ids on raw cache entries from the *arr libraries."""
    from src.services.media_enricher import _RAW_CACHE_DAYS

    index = await build_arr_id_index()
    if not index["tv"] and not index["movie"]:
        return {"source": "external_ids", "visited": 0, "added": 0}

    rows = cache.conn.execute(
        """
        SELECT cache_key, response FROM api_cache
        WHERE (cache_key LIKE 'v2:raw:%' OR cache_key LIKE 'raw:%')
          AND expires_at > datetime('now')
          AND response NOT LIKE '%"imdb_id": "tt%'
        """
    ).fetchall()

    visited = added = 0
    for key, resp in rows:
        if should_stop and should_stop():
            break
        try:
            raw = json.loads(resp)
        except Exception:
            continue
        mtype = {"tv": "show"}.get(raw.get("media_type"), raw.get("media_type"))
        if raw.get("imdb_id") or not raw.get("title"):
            continue
        family = "tv" if mtype in TV else "movie" if mtype == "movie" else None
        if not family:
            continue
        visited += 1
        if limit and visited > limit:
            break
        ids = index[family].get(_norm(raw["title"]))
        if not ids:
            continue
        changed = False
        for field, value in ids.items():
            if value and not raw.get(field):
                raw[field] = value
                changed = True
        if changed:
            # Recorded so a later look can tell a harvested id from one the
            # enrichment path resolved itself.
            raw["ids_from_arr"] = True
            # set_cache prefixes the cache version itself; the key read back
            # out of the table already carries it, and passing it through
            # unchanged writes a second copy under "v2:v2:raw:…" that nothing
            # ever reads.
            cache.set_cache(_unprefixed(key), raw, days=_RAW_CACHE_DAYS)
            added += 1
        if task is not None and visited % 50 == 0:
            from src.services.task_monitor import task_monitor
            task_monitor.update(task, processed=visited, total=len(rows),
                                message=f"{added} matched so far")
    return {"source": "external_ids", "visited": visited, "added": added}
