"""
Curatarr — Community-reception layer (AniList + Jikan/MAL + TMDB reviews).

The judge goes nearly blind on obscure titles: Takamine-san showed a bare
5.8 TMDB rating and no Wikipedia article while MAL actually held 6.17/10
from 48,187 users and 20 written reviews — including exactly the split
("gem of the genre" 9/10 vs "gratuitous" 3/10) the deletion debate needed.

Prototype-validated (tests/reception_proto.py) before wiring:
  - AniList TITLE SEARCH (+year, entity-resolution lesson) is the working
    resolution path — the anime-lists tvdb mapping lags for new seasons;
  - AniList ``idMal`` bridges to Jikan reliably;
  - the 8B summarizer condenses split opinions faithfully (grounding-sniffed
    clean); score numbers are still written DETERMINISTICALLY so they can
    never be hallucinated.

Mirrors the significance layer exactly: the result lives on the raw:* cache
doc as ``reception`` + idempotent ``reception_checked`` marker, flows through
build_verified_data/format_verified_block into the judge and discussions,
gets a JIT top-up in ensure_verified_data (allow_summarizer-gated, so gate
holders never trigger it) and a custodian backfill walker.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import httpx

from src.config import settings
from src.services.llm_utils import (clean_llm_text, ollama_options,
                                    strip_think_tags, SUMMARIZER_KEEP_ALIVE)

logger = logging.getLogger(__name__)

ANILIST_URL = "https://graphql.anilist.co"
JIKAN_URL = "https://api.jikan.moe/v4"
_JIKAN_PAUSE_S = 1.2          # politeness: jikan allows ~60/min
_MAX_REVIEWS = 4              # per source, clipped — plenty for a verdict

_ANILIST_FIELDS = """
  id idMal seasonYear averageScore popularity favourites
  title { romaji english }
  tags { name rank }
  reviews(sort: RATING_DESC, perPage: 4) { nodes { summary score body } }
  relations { edges { relationType(version: 2)
    node { seasonYear format title { romaji english } } } }
  staff(perPage: 12, sort: RELEVANCE) {
    edges { role node { name { full } } } }
"""

# The creative core the curator's talent-pedigree arguments need; the
# director already has his own verified-data line. Proto: 24/24 library
# anime across 1960-2026 carried these roles (22/24 with 3+).
_STAFF_KEEP = re.compile(
    r"original creator|original story|series composition|script|screenplay"
    r"|music|character design|chief director", re.I)


def _extract_staff(al: dict) -> list[str]:
    out, seen = [], set()
    for ed in ((al.get("staff") or {}).get("edges")) or []:
        role = (ed.get("role") or "").strip()
        name = ((ed.get("node") or {}).get("name") or {}).get("full")
        if not name or not _STAFF_KEEP.search(role):
            continue
        role = re.sub(r"\s*\(.*?\)\s*$", "", role)   # "Script (eps 3, 7)" -> "Script"
        key = (role.lower(), name)
        if key in seen:
            continue
        seen.add(key)
        out.append(f"{role}: {name}")
    return out[:6]

# The anime franchise graph — manga/novel sources are out of scope for
# library curation, and long tails (character cameos etc.) only add noise.
_RELATION_FORMATS = {"TV", "TV_SHORT", "MOVIE", "OVA", "ONA", "SPECIAL"}
_MAX_RELATIONS = 8


def _extract_relations(al: dict) -> list[dict]:
    rels = []
    for ed in ((al.get("relations") or {}).get("edges")) or []:
        node = ed.get("node") or {}
        if (node.get("format") or "") not in _RELATION_FORMATS:
            continue
        t = (node.get("title") or {}).get("english") or \
            (node.get("title") or {}).get("romaji")
        if not t:
            continue
        rels.append({"type": (ed.get("relationType") or "RELATED").replace("_", " ").title(),
                     "title": t, "year": node.get("seasonYear")})
    return rels[:_MAX_RELATIONS]

_CONDENSE_SYS = (
    "You write a RECEPTION summary for a media curator's evidence file. "
    "Use ONLY the material given — never your own knowledge of the title. "
    "If the material is thin, say so plainly instead of padding."
)


def _clip(s: Optional[str], n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s[:n] + ("…" if len(s) > n else "")


# ── fetchers ─────────────────────────────────────────────────────────────────

async def _anilist_media(client: httpx.AsyncClient, *, anilist_id=None,
                         search: str = None, year: int = None) -> dict:
    if anilist_id:
        q = "query($id:Int){ Media(id:$id, type:ANIME){ %s } }" % _ANILIST_FIELDS
        r = await client.post(ANILIST_URL,
                              json={"query": q, "variables": {"id": int(anilist_id)}})
        if r.status_code == 200:
            return (r.json().get("data") or {}).get("Media") or {}
        return {}
    q = ("query($search:String){ Page(perPage:5){ media(search:$search, type:ANIME)"
         "{ %s } } }" % _ANILIST_FIELDS)
    r = await client.post(ANILIST_URL, json={"query": q, "variables": {"search": search}})
    if r.status_code != 200:
        return {}
    media = ((r.json().get("data") or {}).get("Page") or {}).get("media") or []
    if not media:
        return {}
    if not year:
        return media[0]
    # year-anchored best match — never trust a bare title (entity-resolution rule)
    scored = sorted(media, key=lambda m: abs((m.get("seasonYear") or 0) - year))
    return scored[0] if abs((scored[0].get("seasonYear") or 0) - year) <= 1 else {}


async def _jikan(client: httpx.AsyncClient, path: str) -> dict:
    try:
        r = await client.get(f"{JIKAN_URL}{path}")
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


async def _tmdb_reviews(client: httpx.AsyncClient, tmdb_id, media_type: str) -> list[dict]:
    if not settings.TMDB_API_KEY or not tmdb_id:
        return []
    kind = "movie" if media_type == "movie" else "tv"
    try:
        r = await client.get(
            f"https://api.themoviedb.org/3/{kind}/{tmdb_id}/reviews",
            params={"api_key": settings.TMDB_API_KEY, "language": "en-US"},
        )
        return (r.json().get("results") or []) if r.status_code == 200 else []
    except Exception:
        return []


# ── condensation ─────────────────────────────────────────────────────────────

async def _condense(prompt: str) -> Optional[str]:
    for model in (settings.SUMMARIZER_MODEL, settings.BASE_SUMMARIZER_MODEL):
        if not model:
            continue
        try:
            async with httpx.AsyncClient(timeout=150) as client:
                r = await client.post(f"{settings.effective_ollama}/api/chat", json={
                    "model": model,
                    "messages": [{"role": "system", "content": _CONDENSE_SYS},
                                 {"role": "user", "content": prompt}],
                    "stream": False,
                    "keep_alive": SUMMARIZER_KEEP_ALIVE,
                    **ollama_options(temperature=0.2, num_predict=400),
                })
            if r.status_code != 200:
                continue
            out = clean_llm_text(strip_think_tags(
                r.json().get("message", {}).get("content", "") or "")).strip()
            out = re.sub(r"(?:^|\n)\s*(?:\d+[.)]|[-*•])\s*", " ", out)   # flatten lists
            out = re.sub(r"\s{2,}", " ", out.replace("**", "")).strip()
            if len(out) >= 40:
                return out
        except Exception as e:
            logger.debug("[reception] condense via %s failed: %s", model, e)
    return None


def _anime_stats_line(al: dict, mal: dict) -> str:
    bits = []
    if mal.get("score"):
        bits.append(f"MyAnimeList {mal['score']}/10 from {mal.get('scored_by') or '?'} ratings"
                    + (f" ({mal.get('members')} members)" if mal.get("members") else ""))
    if al.get("averageScore"):
        bits.append(f"AniList {al['averageScore']}/100")
    return "Community scores: " + "; ".join(bits) + "." if bits else ""


def _anime_tags_line(al: dict) -> str:
    tags = sorted(al.get("tags") or [], key=lambda t: -(t.get("rank") or 0))[:6]
    if not tags:
        return ""
    return "Community tags: " + ", ".join(
        f"{t['name']} ({t['rank']}%)" for t in tags if t.get("name")) + "."


async def build_reception(title: str, media_type: str, *, year: int = None,
                          anilist_id=None, tmdb_id=None) -> tuple[Optional[str], list, list]:
    """Fetch community reviews and condense them into a short RECEPTION
    paragraph. Score numbers are written deterministically; only the verdict
    prose comes from the summarizer. Returns (reception_text, typed_relations)
    — relations and staff ride along free on the same AniList call (anime
    only)."""
    async with httpx.AsyncClient(timeout=25) as client:
        if media_type == "anime":
            al = await _anilist_media(client, anilist_id=anilist_id,
                                      search=title, year=year)
            if not al:
                return None, [], []
            relations = _extract_relations(al)
            staff = _extract_staff(al)
            mal_stats, mal_reviews = {}, []
            if al.get("idMal"):
                core = await _jikan(client, f"/anime/{al['idMal']}")
                mal_stats = core.get("data") or {}
                await asyncio.sleep(_JIKAN_PAUSE_S)
                revs = await _jikan(client, f"/anime/{al['idMal']}/reviews")
                mal_reviews = (revs.get("data") or [])[:_MAX_REVIEWS]
            stats = _anime_stats_line(al, mal_stats)
            tags = _anime_tags_line(al)
            al_reviews = (al.get("reviews") or {}).get("nodes") or []
            if not mal_reviews and not al_reviews:
                # quantitative signal only — deterministic, no LLM, still honest
                if not stats:
                    return None, relations, staff
                return " ".join(x for x in (
                    stats, tags, "Audience too small for written reviews.") if x), relations, staff
            lines = [f"TITLE: {title}", stats, tags]
            for rv in al_reviews:
                lines.append(f"\nAniList review ({rv.get('score')}/100): "
                             f"{_clip(rv.get('summary'), 200)} {_clip(rv.get('body'), 700)}")
            for rv in mal_reviews:
                t = ",".join(rv.get("tags") or [])
                lines.append(f"\nMAL review ({rv.get('score')}/10, {t}): "
                             f"{_clip(rv.get('review'), 900)}")
            lines.append("\nTASK: Write RECEPTION — 3 to 5 sentences: the community "
                         "verdict, what viewers praise, what they slam, and whether "
                         "opinion is split. Plain prose, no bullet points, no scores "
                         "invented beyond those above.")
            verdict = await _condense("\n".join(x for x in lines if x))
            if not verdict:
                return (" ".join(x for x in (stats, tags) if x) or None), relations, staff
            return " ".join(x for x in (stats, verdict) if x), relations, staff

        # movies / shows: TMDB reviews (ratings already live on the raw doc)
        reviews = await _tmdb_reviews(client, tmdb_id, media_type)
        if not reviews:
            return None, [], []
        lines = [f"TITLE: {title}"]
        for rv in reviews[:_MAX_REVIEWS]:
            rating = (rv.get("author_details") or {}).get("rating")
            lines.append(f"\nTMDB review ({rating}/10): {_clip(rv.get('content'), 800)}")
        lines.append("\nTASK: Write RECEPTION — 3 to 5 sentences: the community "
                     "verdict, what viewers praise, what they slam, and whether "
                     "opinion is split. Plain prose, no bullet points, no scores "
                     "invented beyond those above.")
        return await _condense("\n".join(lines)), []


def _offline_tags(title: str, media_type: str, *, anilist_id=None,
                  mal_id=None, year=None) -> list[str]:
    """AniDB community tags from the weekly offline snapshot (local read,
    never a download in this path). Anime only."""
    if media_type != "anime":
        return []
    try:
        from src.services import anime_offline
        off = anime_offline.lookup(anilist_id=anilist_id, mal_id=mal_id,
                                   title=title, year=year)
        return (off or {}).get("tags", [])[:18]
    except Exception:
        return []


# ── raw-doc top-up (mirrors topup_significance) ──────────────────────────────

async def topup_reception(
    title: str,
    media_type: str,
    *,
    tmdb_id=None, tvdb_id=None, anilist_id=None, anidb_id=None,
    plex_rating_key=None, year: Optional[int] = None,
    cache=None,
) -> bool:
    """Fetch community reception once and store it on the title's raw:* cache
    entries, idempotent via ``reception_checked``. Returns True when a
    reception string was actually added."""
    if media_type not in ("anime", "movie", "show"):
        return False
    from src.cache.metadata_cache import MetadataCache
    from src.services.media_enricher import _RAW_CACHE_DAYS
    owns = cache is None
    if owns:
        cache = MetadataCache()
    try:
        id_keys = [v for v in (anilist_id, anidb_id, tmdb_id, tvdb_id, plex_rating_key) if v]
        t40 = (title or "")[:40]
        if t40:
            id_keys.append(t40)
        targets = []
        for k in id_keys:
            hit = cache.get_cache(f"raw:{media_type}:{k}")
            if not hit or not isinstance(hit.get("response"), dict):
                continue
            raw = hit["response"]
            if raw.get("reception_checked"):
                return False  # already done for this title
            targets.append((f"raw:{media_type}:{k}", raw))
        if not targets:
            return False
        # prefer ids the raw doc itself carries (sonarr prefetch resolves them)
        doc = targets[0][1]
        rec, rels, staff = await build_reception(
            title, media_type, year=year or doc.get("year"),
            anilist_id=anilist_id or doc.get("anilist_id"),
            tmdb_id=tmdb_id or doc.get("tmdb_id"),
        )
        anidb_tags = _offline_tags(title, media_type,
                                   anilist_id=anilist_id or doc.get("anilist_id"),
                                   mal_id=doc.get("mal_id"),
                                   year=year or doc.get("year"))
        added = False
        for key, raw in targets:
            # Marker set even on None so titles with no community data are
            # never re-queried (same contract as significance_checked).
            raw["reception_checked"] = True
            raw["relations_checked"] = True
            if rec:
                raw["reception"] = rec
                added = True
            if rels:
                raw["relations"] = rels
            if staff:
                raw["staff"] = staff
            if anidb_tags:
                raw["anidb_tags"] = anidb_tags
            cache.set_cache(key, raw, days=_RAW_CACHE_DAYS)
        return added
    except Exception as e:
        logger.debug("[reception] top-up failed for %r: %s", title, e)
        return False
    finally:
        if owns:
            cache.close()


async def topup_franchise(
    title: str,
    media_type: str,
    *,
    tmdb_id=None, tvdb_id=None, anilist_id=None, anidb_id=None,
    plex_rating_key=None, year: Optional[int] = None,
    cache=None,
) -> bool:
    """Light catch-up for docs that were reception-checked BEFORE relations
    existed: one AniList call (no LLM) + a local snapshot read. Idempotent
    via ``relations_checked``. The franchise graph only EXISTS for anime in
    our sources, but the marker is stamped on movies/shows too so the
    backfill query converges instead of re-touching them every tick."""
    if media_type not in ("anime", "movie", "show"):
        return False
    from src.cache.metadata_cache import MetadataCache
    from src.services.media_enricher import _RAW_CACHE_DAYS
    owns = cache is None
    if owns:
        cache = MetadataCache()
    try:
        id_keys = [v for v in (anilist_id, anidb_id, tmdb_id, tvdb_id, plex_rating_key) if v]
        t40 = (title or "")[:40]
        if t40:
            id_keys.append(t40)
        targets = []
        for k in id_keys:
            hit = cache.get_cache(f"raw:{media_type}:{k}")
            if not hit or not isinstance(hit.get("response"), dict):
                continue
            raw = hit["response"]
            if raw.get("relations_checked"):
                return False
            targets.append((f"raw:{media_type}:{k}", raw))
        if not targets:
            return False
        doc = targets[0][1]
        rels, anidb_tags, staff = [], [], []
        if media_type == "anime":
            al_id = anilist_id or doc.get("anilist_id")
            async with httpx.AsyncClient(timeout=25) as client:
                al = await _anilist_media(client, anilist_id=al_id,
                                          search=title, year=year or doc.get("year"))
            rels = _extract_relations(al) if al else []
            staff = _extract_staff(al) if al else []
            anidb_tags = _offline_tags(title, media_type, anilist_id=al_id,
                                       mal_id=doc.get("mal_id"),
                                       year=year or doc.get("year"))
        added = False
        for key, raw in targets:
            raw["relations_checked"] = True
            if rels:
                raw["relations"] = rels
                added = True
            if staff:
                raw["staff"] = staff
                added = True
            if anidb_tags:
                raw["anidb_tags"] = anidb_tags
                added = True
            cache.set_cache(key, raw, days=_RAW_CACHE_DAYS)
        return added
    except Exception as e:
        logger.debug("[reception] franchise top-up failed for %r: %s", title, e)
        return False
    finally:
        if owns:
            cache.close()


# ── custodian walker (mirrors run_significance_backfill) ─────────────────────

async def run_reception_backfill(limit: int = 40) -> dict:
    """Custodian walker: attach community reception to LIVE raw:* entries that
    were never reception-checked. Anime first (largest evidence gain), then
    shows/movies. Yields to any active curator, stops when a game grabs the
    GPU, throttled to Jikan's public rate limit by the per-title pauses."""
    import json as _json
    from datetime import datetime
    from src.cache.metadata_cache import MetadataCache
    from src.services.llm_priority import wait_for_curator

    def _gaming() -> bool:
        try:
            from src.services.app_state import get_state
            return get_state("game_active") == "1"
        except Exception:
            return False

    # weekly snapshot for the AniDB-tag side (downloads at most once a week)
    from src.services.anime_offline import ensure_offline_db
    await ensure_offline_db(allow_download=True)

    cache = MetadataCache()
    try:
        cur = cache.conn.cursor()
        cur.execute(
            """
            SELECT cache_key, response FROM api_cache
            WHERE (cache_key LIKE 'v2:raw:%' OR cache_key LIKE 'raw:%')
              AND expires_at > ?
              AND NOT (response LIKE '%"reception_checked"%'
                       AND response LIKE '%"relations_checked"%')
            """,
            (datetime.now().isoformat(),),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        cache.close()

    # anime first — that's where the blind spot (and the data) is biggest
    order = {"anime": 0, "show": 1, "movie": 2}
    items = []
    for row in rows:
        key = row["cache_key"]
        key = key.split(":", 1)[1] if key.startswith("v2:") else key
        try:
            _, cat, id_key = key.split(":", 2)
        except ValueError:
            continue
        if cat not in order:
            continue
        try:
            resp = _json.loads(row["response"]) if isinstance(row["response"], str) else row["response"]
        except Exception:
            continue
        items.append((order[cat], cat, id_key, resp or {}))
    items.sort(key=lambda x: x[0])

    total = len(items)
    checked = added = 0
    for _, cat, id_key, resp in items[:limit]:
        if _gaming():
            logger.info("[reception-backfill] game active — stopping early "
                        "(%d/%d this pass)", checked, min(limit, total))
            break
        title = resp.get("title") or id_key
        await wait_for_curator()
        # full pass when reception was never fetched; light relations/tags
        # catch-up (no LLM) when only the franchise fields are missing
        fn = topup_reception if not resp.get("reception_checked") else topup_franchise
        try:
            if await asyncio.wait_for(
                fn(title, cat, plex_rating_key=id_key,
                   year=resp.get("year"),
                   anilist_id=resp.get("anilist_id"),
                   tmdb_id=resp.get("tmdb_id")),
                timeout=90.0,
            ):
                added += 1
        except Exception as e:
            logger.debug("[reception-backfill] %r failed: %s", title, e)
        checked += 1
    return {"checked": checked, "added": added,
            "remaining": max(0, total - checked)}
