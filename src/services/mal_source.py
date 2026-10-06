"""
Curatarr — MyAnimeList data for anime: the official API v2 when a client id
is configured, Jikan (the unofficial, keyless scraper) otherwise, behind a
pause.

Jikan has failed since 2026-08-28: HTTP 504 on every endpoint ("Jikan failed
to connect to MyAnimeList", jikan-me/jikan-rest#612, no answer from its
maintainers), and by 2026-10-06 it took no connections at all. Every anime
AniList could not match waited on it for each run and stayed open, and anime
reception, which demanded MAL's scores and reviews from Jikan, stood still
for every title with a MAL id.

The official API needs no user login for public data: the client id travels
in the X-MAL-CLIENT-ID header (a free app at myanimelist.net/apiconfig, type
"other", no secret). It carries scores, rank, popularity, genres (themes and
demographics folded in; split back here), the content rating, studios and
the source material, but no written reviews. Those stay Jikan's, while it
answers.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

API = "https://api.myanimelist.net/v2"
UNAVAILABLE = object()   # rate-limited, down, or the client id refused: ask again later

# Jikan's pause: this many failures in a row and it is not asked for an hour;
# one success clears the count.
JIKAN_PAUSE_AFTER = 3
JIKAN_PAUSE_S = 3600
_jikan = {"fails": 0, "until": 0.0}
_warned: set = set()

_DETAIL_FIELDS = ("id,title,alternative_titles,start_date,start_season,synopsis,mean,rank,"
                  "popularity,num_list_users,num_scoring_users,nsfw,genres,media_type,status,"
                  "num_episodes,source,rating,studios")
# MAL's own taxonomy: the API returns one "genres" list, Jikan returned four.
_GENRES = frozenset({"Action", "Adventure", "Avant Garde", "Award Winning", "Boys Love",
                     "Comedy", "Drama", "Fantasy", "Girls Love", "Gourmet", "Horror",
                     "Mystery", "Romance", "Sci-Fi", "Slice of Life", "Sports",
                     "Supernatural", "Suspense"})
_EXPLICIT = frozenset({"Ecchi", "Erotica", "Hentai"})
_DEMOGRAPHICS = frozenset({"Josei", "Kids", "Seinen", "Shoujo", "Shounen"})
# Jikan's strings: the tone hints and the adult guard read these.
_RATING = {"g": "G - All Ages", "pg": "PG - Children", "pg_13": "PG-13 - Teens 13 or older",
           "r": "R - 17+ (violence & profanity)", "r+": "R+ - Mild Nudity", "rx": "Rx - Hentai"}
_STATUS = {"finished_airing": "Finished Airing", "currently_airing": "Currently Airing",
           "not_yet_aired": "Not yet aired"}


def client_id() -> Optional[str]:
    cid = (settings.MAL_CLIENT_ID or "").strip()
    return cid or None


# ── Jikan's pause ─────────────────────────────────────────────────────────────

def jikan_paused() -> bool:
    return time.monotonic() < _jikan["until"]


def jikan_result(ok: bool) -> None:
    """Count Jikan's answers: JIKAN_PAUSE_AFTER failures in a row pause it
    for JIKAN_PAUSE_S; a success clears the count. After a pause the next
    call is tried once, and one more failure pauses it again."""
    if ok:
        _jikan["fails"] = 0
        return
    _jikan["fails"] += 1
    if _jikan["fails"] >= JIKAN_PAUSE_AFTER and not jikan_paused():
        _jikan["until"] = time.monotonic() + JIKAN_PAUSE_S
        logger.warning("[mal] Jikan failed %d times in a row: not asked for %d minutes%s",
                       _jikan["fails"], JIKAN_PAUSE_S // 60,
                       "" if client_id() else "; a MyAnimeList client id replaces it")


# ── the official API ──────────────────────────────────────────────────────────

async def get(client: httpx.AsyncClient, path: str, params: dict) -> object:
    """One official call: the JSON on 200, None when MAL answered that there
    is nothing (400 bad query, 404), UNAVAILABLE otherwise."""
    cid = client_id()
    if not cid:
        return UNAVAILABLE
    try:
        r = await client.get(f"{API}{path}", params=params, headers={"X-MAL-CLIENT-ID": cid})
    except Exception as e:  # noqa: BLE001 — the network: ask again later
        logger.debug("[mal] %s: %s", path, type(e).__name__)
        return UNAVAILABLE
    if r.status_code == 200:
        return r.json()
    if r.status_code in (400, 404):
        return None
    if r.status_code in (401, 403) and r.status_code not in _warned:
        _warned.add(r.status_code)
        logger.warning("[mal] HTTP %d from the MyAnimeList API: %s", r.status_code,
                       "the client id was refused, check MAL_CLIENT_ID" if r.status_code == 401
                       else "refused; MyAnimeList answers 403 to too many requests")
    return UNAVAILABLE


async def anime(client: httpx.AsyncClient, mal_id: int) -> object:
    """The detail record of one MAL id, None, or UNAVAILABLE."""
    return await get(client, f"/anime/{int(mal_id)}", {"fields": _DETAIL_FIELDS, "nsfw": "true"})


async def search(client: httpx.AsyncClient, title: str,
                 close: Callable[[str, list], bool], rejected=()) -> object:
    """The MAL id of the first result whose titles ``close`` accepts for
    ``title``, None when none does, or UNAVAILABLE. The owner's rejected ids
    are skipped."""
    q = (title or "").strip()[:64]
    if len(q) < 3:                      # MAL refuses shorter searches
        return None
    data = await get(client, "/anime", {"q": q, "limit": 8, "nsfw": "true",
                                        "fields": "id,title,alternative_titles"})
    if data is UNAVAILABLE:
        return UNAVAILABLE
    for row in (data or {}).get("data") or []:
        node = row.get("node") or {}
        if node.get("id") in rejected:
            continue
        alt = node.get("alternative_titles") or {}
        names = [node.get("title") or "", alt.get("en") or "", alt.get("ja") or "",
                 *(alt.get("synonyms") or [])]
        if close(title, names):
            return node.get("id")
    return None


async def reception_stats(client: httpx.AsyncClient, mal_id: int) -> object:
    """MAL's numbers for the reception line, keyed the way Jikan's were
    (score, scored_by, members); {} when MAL has no such anime."""
    d = await get(client, f"/anime/{int(mal_id)}",
                  {"fields": "mean,num_scoring_users,num_list_users", "nsfw": "true"})
    if d is UNAVAILABLE:
        return UNAVAILABLE
    if not d:
        return {}
    return {"score": d.get("mean"), "scored_by": d.get("num_scoring_users"),
            "members": d.get("num_list_users")}


def _source_label(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    return "4-koma manga" if code == "4_koma_manga" else code.replace("_", " ").capitalize()


def to_supplement(d: dict) -> dict:
    """The official record in the shape the Jikan fetcher always returned,
    so the merge, the tone hints and the adult guard read it unchanged."""
    names = [g.get("name") for g in d.get("genres") or [] if g.get("name")]
    alt = d.get("alternative_titles") or {}
    year = (d.get("start_season") or {}).get("year")
    if not year and (d.get("start_date") or "")[:4].isdigit():
        year = int(d["start_date"][:4])
    return {
        "source": "mal",
        "mal_id": d.get("id"),
        "title": alt.get("en") or d.get("title"),
        "year": year,
        "synopsis": (d.get("synopsis") or "")[:800],
        "genres": [n for n in names if n in _GENRES],
        "themes": [n for n in names if n not in _GENRES | _EXPLICIT | _DEMOGRAPHICS],
        "demographics": [n for n in names if n in _DEMOGRAPHICS],
        "explicit_genres": [n for n in names if n in _EXPLICIT],
        "score": d.get("mean"),
        "scored_by": d.get("num_scoring_users"),
        "rank": d.get("rank"),
        "popularity": d.get("popularity"),
        "episodes": d.get("num_episodes") or None,
        "status": _STATUS.get(d.get("status"), d.get("status")),
        "rating": _RATING.get(d.get("rating"), d.get("rating")),
        "studios": [s.get("name") for s in d.get("studios") or [] if s.get("name")],
        "source_material": _source_label(d.get("source")),
    }
