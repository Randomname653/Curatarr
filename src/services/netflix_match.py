"""Netflix titles onto the library (2026-10-25).

A Netflix play counts like a Plex play, so it has to name the same title
the library does. Every reader of watch_history matches video by title,
series title or TMDB id, so the matcher writes those:

* a film in the library: ``plex_item_id`` becomes the key its Plex plays
  already use (else its enrichment key ``radarr:N``), ``title`` the
  library's title, ``tmdb_id`` the film's;
* a series in the library: ``series_title`` becomes the library's title
  (Sonarr's), ``media_type`` its category (show / anime), ``tmdb_id`` the
  series'. ``plex_item_id`` stays the episode's own Netflix id - the
  episode readers count distinct ids;
* not in the library: the TMDB id when TMDB knows the title, so it joins
  the library once it arrives (``relink``).

Exact titles only (case, punctuation and "&" aside): a Netflix play that
protects a library title from deletion must be that title. The TMDB search
runs in English and German, because a German profile names "Money Heist"
"Haus des Geldes"."""
import logging
import re
import unicodedata
from typing import Callable, Optional

from src.services.media_server.base import key_namespace

logger = logging.getLogger(__name__)

_LANGS = ("en-US", "de-DE")


def norm(t: str) -> str:
    t = unicodedata.normalize("NFKC", (t or "")).casefold().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t)).strip()


def catalog(db) -> dict:
    """The library's films and series by normalized title and by id. A
    title two library entries share (a remake) is no title match."""
    from src.database.models import ArrEnrichmentStatus as AES, WatchHistoryEntry as W
    cat = {"movie": {}, "show": {}, "movie_tmdb": {}, "show_tmdb": {}, "show_tvdb": {}}
    plex_keys = {}
    for t, k in db.query(W.title, W.plex_item_id).filter(
            W.media_type == "movie", W.source.is_(None)).distinct():
        if t and k and key_namespace(str(k)) == "server":
            plex_keys.setdefault(norm(t), k)
    for s, arr_id, c, t, tmdb, tvdb in db.query(AES.service, AES.arr_id, AES.category, AES.title,
                                                AES.tmdb_id, AES.tvdb_id).filter(
            AES.service.in_(("radarr", "sonarr"))):
        if not t:
            continue
        if s == "radarr":
            e = {"title": t, "tmdb_id": tmdb, "category": "movie",
                 "key": plex_keys.get(norm(t)) or f"radarr:{arr_id}"}
            kind = "movie"
        else:
            e = {"title": t, "tmdb_id": tmdb, "tvdb_id": tvdb, "arr_id": arr_id,
                 "category": c if c in ("show", "anime") else "show"}
            kind = "show"
            if tvdb:
                cat["show_tvdb"][tvdb] = e
        if tmdb:
            cat[f"{kind}_tmdb"][tmdb] = e
        n = norm(t)
        cat[kind][n] = None if n in cat[kind] else e
    return cat


def local(cat: dict, kind: str, name: str, tmdb_id=None, tvdb_id=None) -> Optional[dict]:
    """The library entry for one Netflix title, without the network."""
    if tmdb_id and cat[f"{kind}_tmdb"].get(tmdb_id):
        return cat[f"{kind}_tmdb"][tmdb_id]
    if kind == "show" and tvdb_id and cat["show_tvdb"].get(tvdb_id):
        return cat["show_tvdb"][tvdb_id]
    return cat[kind].get(norm(name))


async def tmdb_find(kind: str, name: str, get: Optional[Callable] = None) -> Optional[dict]:
    """{"tmdb_id", "tvdb_id"} of the TMDB entry whose title (English or
    German) or original title is exactly ``name``; None otherwise."""
    import httpx
    from src.services.http_client import tls_context
    from src.services.media_enricher import _tmdb_get
    get = get or _tmdb_get
    ep = "movie" if kind == "movie" else "tv"
    tk, ok = ("title", "original_title") if ep == "movie" else ("name", "original_name")
    want = norm(name)
    async with httpx.AsyncClient(verify=tls_context(), timeout=10) as client:
        for lang in _LANGS:
            r = await get(client, f"/search/{ep}", {"query": name, "language": lang})
            hit = next((x for x in (r.get("results") or [])[:5]
                        if want in (norm(x.get(tk)), norm(x.get(ok)))), None)
            if hit:
                out = {"tmdb_id": hit.get("id"), "tvdb_id": None}
                if ep == "tv" and hit.get("id"):
                    ext = await get(client, f"/tv/{hit['id']}/external_ids", {})
                    out["tvdb_id"] = ext.get("tvdb_id")
                return out
    return None


def apply(row: dict, entry: Optional[dict], found: Optional[dict]) -> dict:
    """One parsed play with the library entry (or the TMDB ids) written in."""
    if entry and row["media_type"] == "movie":
        return {**row, "plex_item_id": entry["key"], "title": entry["title"], "tmdb_id": entry.get("tmdb_id")}
    if entry:
        return {**row, "series_title": entry["title"], "media_type": entry["category"],
                "tmdb_id": entry.get("tmdb_id")}
    if found:
        return {**row, "tmdb_id": found.get("tmdb_id")}
    return row


def fix_kind(row: dict, cat: dict) -> dict:
    """A title parsed as an episode ("A: B: C") whose whole Netflix title is
    a library film is that film."""
    if row["media_type"] != "movie" and cat["movie"].get(norm(row.get("raw_title") or "")):
        return {**row, "media_type": "movie", "series_title": None, "season": None, "episode": None,
                "title": row["raw_title"]}
    return row


def number_episode(row: dict, eps: list) -> dict:
    """Season and episode number from the library's episode list (Sonarr's)
    when Netflix left one out: the episode whose title is the play's title,
    or its tail ("Stranger Things 2: Chapter One: MADMAX" is "Chapter One:
    MADMAX"), and agrees with whatever number Netflix did give. One such
    episode or nothing."""
    if row.get("season") is not None and row.get("episode") is not None:
        return row
    by_title: dict = {}
    for e in eps or []:
        if e.get("title"):
            by_title.setdefault(norm(e["title"]), []).append(e)
    parts = (row.get("title") or "").split(": ")
    for i in range(len(parts)):
        hits = by_title.get(norm(": ".join(parts[i:]))) or []
        if row.get("season") is not None:
            hits = [h for h in hits if h.get("seasonNumber") == row["season"]]
        if row.get("episode") is not None:
            hits = [h for h in hits if h.get("episodeNumber") == row["episode"]]
        hits = list({(h.get("seasonNumber"), h.get("episodeNumber")): h for h in hits}.values())
        if len(hits) == 1:
            return {**row, "season": hits[0].get("seasonNumber"), "episode": hits[0].get("episodeNumber"),
                    "title": hits[0].get("title") or row["title"]}
    return row


async def sonarr_episodes(arr_id: int) -> list:
    """Sonarr's episode list of one series ([] when Sonarr is not set up or
    does not answer)."""
    from src.config import settings
    from src.services.arr_client import SonarrClient
    if not (settings.SONARR_URL and settings.SONARR_API_KEY):
        return []
    try:
        async with SonarrClient(settings.SONARR_URL, settings.SONARR_API_KEY) as c:
            return await c.get_episodes(int(arr_id)) or []
    except Exception as e:  # noqa: BLE001 - the plays keep their titles as the episode key
        logger.info("[netflix] Sonarr episodes of series %s: %s", arr_id, e)
        return []


async def tmdb_season_episodes(tmdb_id: int, season: int, get: Optional[Callable] = None) -> list:
    """One season's episodes from TMDB, under their English and German
    names, in the shape of Sonarr's list. A German profile names "Lies"
    "Lügen", which Sonarr's English list does not know."""
    import httpx
    from src.services.http_client import tls_context
    from src.services.media_enricher import _tmdb_get
    get = get or _tmdb_get
    out = []
    async with httpx.AsyncClient(verify=tls_context(), timeout=10) as client:
        for lang in _LANGS:
            try:
                r = await get(client, f"/tv/{int(tmdb_id)}/season/{int(season)}", {"language": lang})
            except Exception as e:  # noqa: BLE001 - the plays keep their titles as the episode key
                logger.info("[netflix] TMDB season %s/%s: %s", tmdb_id, season, e)
                continue
            out += [{"seasonNumber": e.get("season_number"), "episodeNumber": e.get("episode_number"),
                     "title": e.get("name")} for e in r.get("episodes") or []]
    return out


def name_of(row: dict) -> tuple:
    return (row["media_type"] if row["media_type"] == "movie" else "show",
            row["series_title"] if row["media_type"] != "movie" else row["title"])


async def resolve(rows: list, cat: dict, find=None, task_report=None) -> dict:
    """{(kind, name): (entry, found)} for every distinct title in ``rows``:
    the library first, TMDB for the rest. A title parsed as an episode
    whose whole Netflix title is a library film is that film."""
    find = find or tmdb_find
    out: dict = {}
    names = sorted({name_of(r) for r in rows})
    for i, (kind, name) in enumerate(names):
        entry = local(cat, kind, name)
        found = None
        if entry is None:
            try:
                found = await find(kind, name)
            except Exception as e:  # noqa: BLE001 - one failed search leaves the title unmatched
                logger.info("[netflix] TMDB search for %r failed: %s", name, e)
            if found:
                entry = local(cat, kind, name, found.get("tmdb_id"), found.get("tvdb_id"))
        out[(kind, name)] = (entry, found)
        if task_report and i % 25 == 0:
            task_report(i, len(names))
    return out


def relink(db) -> int:
    """Netflix plays whose title arrived in the library since the import
    (by TMDB id, else by title). No network; the daily custodian pass."""
    from src.database.models import WatchHistoryEntry as W
    cat = catalog(db)
    n = 0
    rows = db.query(W).filter(W.source == "netflix").all()
    for w in rows:
        if w.media_type == "movie":
            if not str(w.plex_item_id or "").startswith("netflix:"):
                continue
            e = local(cat, "movie", w.title, w.tmdb_id)
            if e:
                w.plex_item_id, w.title, w.tmdb_id = e["key"], e["title"], e.get("tmdb_id")
                n += 1
        else:
            e = local(cat, "show", w.series_title or "", w.tmdb_id)
            if e and (w.series_title != e["title"] or w.media_type != e["category"]):
                w.series_title, w.media_type, w.tmdb_id = e["title"], e["category"], e.get("tmdb_id")
                n += 1
    return n


def run_relink() -> int:
    """The custodian's daily pass (blocking; via run_write)."""
    from src.database.connection import get_db_session
    with get_db_session() as db:
        n = relink(db)
        db.commit()
    if n:
        logger.info("[netflix] %d plays linked to titles that arrived in the library", n)
    return n
