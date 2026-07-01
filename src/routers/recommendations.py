"""
Curatarr 1.0 - Recommendations & Deletions Router

All endpoints are category-aware and use the LLM for pitches.
"""

import logging
import time
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.database import get_db
from src.database.connection import get_db_session
from src.database.models import (
    ConversationMessage,
    CuratorResolutionLog,
    DeletionProposal,
    User,
)
from src.routers.auth import get_current_user, require_admin
from src.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

CATEGORIES = ["movie", "show", "anime", "music"]

# ── ARR LIBRARY CACHE ─────────────────────────────────────────────────────────
# Per-service cache so Radarr, Sonarr, and Lidarr degrade independently.
# On a successful fetch the response is stored for ARR_CACHE_TTL seconds.
# On a fetch error the stale cache is returned so a momentarily-unreachable
# service doesn't wipe out the candidate pool.
_ARR_CACHE: dict[str, dict] = {}   # {"radarr"|"sonarr"|"lidarr": {"items": list, "at": float}}
_ARR_CACHE_TTL = 300               # 5 minutes — ARR libraries don't change minute-to-minute

# ── TMDB / DEEZER POSTER CACHE ────────────────────────────────────────────────
# Poster paths and synopses are stable for months; cache them for 24 h so
# repeated deletion-proposal refreshes don't re-hit the external API.
_TMDB_CACHE: dict[str, tuple] = {}  # {f"{title}:{category}": (poster_url, synopsis)}
_TMDB_CACHE_TTL = 86_400            # 24 hours


@router.get("/")
async def get_recommendations(
    category: Optional[str] = Query(None),
    limit: int = Query(8),
    refresh: bool = Query(False),
    source: str = Query("cache"),  # cache / library / external
    lane: Optional[str] = Query(None),  # filter cache to "library" / "discovery"
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from src.database.models import CachedRecommendation
    from src.services.app_state import get_state
    CAT_LABEL = {"movie":"🎬 Movies","show":"📺 TV Shows","anime":"⛩️ Anime","music":"🎵 Music"}

    # Library-based: from ARR items not yet watched
    if source == "library":
        from src.services.recommendations_engine import generate_recommendations, score_arr_items
        unwatched = await _fetch_arr_unwatched(user.id, category)
        if not unwatched:
            return {"recommendations": [], "category": category, "source": "library",
                    "message": "No unwatched items in your ARR libraries, or ARR not configured."}
        cats = [category] if category and category in CATEGORIES else CATEGORIES
        all_recs = []
        for cat in cats:
            cat_items = [i for i in unwatched if i.get("category") == cat]
            if not cat_items: continue
            # Pre-filter to top 50 most relevant items to avoid overwhelming the LLM context
            cat_items = await score_arr_items(user.id, cat, cat_items, top_n=50)
            recs = await generate_recommendations(user_id=user.id, category=cat,
                                                   limit=limit, arr_library=cat_items)
            for rec in recs:
                match = next((i for i in cat_items if i["title"] == rec.get("title")), {})
                rec["arr_url"] = match.get("arr_url", "")
                rec["size_gb"] = round(match.get("size_mb", 0) / 1024, 1)
                rec["poster_url"] = await _fetch_poster(rec.get("title", ""), cat)
                rec["category_label"] = CAT_LABEL.get(cat, cat)
            all_recs.extend(recs)
        return {"recommendations": all_recs, "category": category, "source": "library", "from_cache": False}

    # Serve from cache unless refresh/external requested. BOTH lanes
    # ("library" + "discovery") live here; each rec carries its lane so the UI
    # can split them into the two sections. Optional ?lane= narrows to one.
    if not refresh and source != "external":
        q = db.query(CachedRecommendation).filter(CachedRecommendation.user_id == user.id)
        if category and category in CATEGORIES:
            q = q.filter(CachedRecommendation.category == category)
        if lane in ("library", "discovery"):
            q = q.filter(CachedRecommendation.lane == lane)
        # Generous cap: up to two lanes × ~10 each per category, so neither
        # lane gets truncated. Ordered by lane then confidence (best-first).
        cap = limit * (4 if category else 16)
        cached = q.order_by(CachedRecommendation.lane.asc(),
                            CachedRecommendation.confidence.desc()).limit(cap).all()
        if cached:
            recs = [{"title": r.title, "reason": r.reason, "confidence": r.confidence,
                     "genres": r.genres, "category": r.category,
                     "category_label": CAT_LABEL.get(r.category, r.category),
                     "lane": r.lane or "discovery",
                     "poster_url": r.poster_url, "synopsis": r.synopsis,
                     "cached_at": r.cached_at.isoformat() if r.cached_at else None}
                    for r in cached]
            return {"recommendations": recs, "category": category,
                    "from_cache": True, "source": "cache",
                    "cached_at": get_state("recs_cached_at")}

    # Generate on the fly
    import asyncio
    from src.services.recommendations_engine import generate_recommendations
    cats = [category] if category and category in CATEGORIES else CATEGORIES
    all_recs = []
    for cat in cats:
        recs = await generate_recommendations(user_id=user.id, category=cat, limit=limit)
        posters = await asyncio.gather(*[_fetch_tmdb(r.get("title", ""), cat) for r in recs])
        for rec, (poster, synopsis) in zip(recs, posters):
            rec["poster_url"] = poster
            rec["synopsis"] = synopsis
            rec["category_label"] = CAT_LABEL.get(cat, cat)
        all_recs.extend(recs)
    return {"recommendations": all_recs, "category": category, "from_cache": False, "source": "external"}


@router.post("/refresh-cache")
async def refresh_recommendation_cache(
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
):
    """Manually trigger recommendation cache refresh."""
    background_tasks.add_task(_run_cache_refresh, user.id)
    return {"status": "started", "message": "Generating recommendations in background…"}


async def _run_cache_refresh(user_id: int):
    from src.services.scheduler import _cache_recommendations
    await _cache_recommendations(user_id)


# ── POSTER / SYNOPSIS FETCHING ───────────────────────────────────────────────

async def _fetch_poster(title: str, category: str) -> Optional[str]:
    """Fetch poster URL from TMDB (w300). Returns URL or None."""
    poster, _ = await _fetch_tmdb(title, category)
    return poster


async def _fetch_tmdb(
    title: str,
    category: str,
    tmdb_id: int | None = None,
    year: int | None = None,
    tvdb_id: int | None = None,
    mbid: str | None = None,
) -> tuple:
    """
    Return (poster_url, synopsis).
    Uses Deezer for music, TMDB for everything else.
    Results are cached in-memory for 24 h — poster paths are stable.

    Pass 51: when ``tmdb_id`` is supplied (Radarr items always carry it),
    query TMDB by ID directly — deterministic, no title-match ambiguity.
    The old path took ``results[0]`` of a bare title search, which
    silently returned the WRONG entry for franchise titles: a search for
    "Five Nights at Freddy's" returns both the 2023 original and the
    more-popular 2025 sequel, popularity-sorted, so the original got
    the sequel's synopsis and poster. ``year`` disambiguates the
    title-search fallback for items without an ID (e.g. Sonarr).

    Pass 52: ID-determinism extended to the other two ARR sources —
      - ``mbid`` (Lidarr ``foreignArtistId``) → deterministic Deezer
        artist via the MusicBrainz url-relationship bridge.
      - ``tvdb_id`` (Sonarr always provides one even when tmdbId is null)
        → resolved through TMDB's ``/find`` endpoint before the fuzzy
        title search is tried.
    """
    if not title:
        return None, None

    # ID-keyed cache entry when we have any stable ID — prevents two
    # franchise / same-name entries from colliding on a shared title key.
    if tmdb_id:
        cache_key = f"id:{tmdb_id}:{category}"
    elif mbid:
        cache_key = f"mbid:{mbid}:{category}"
    elif tvdb_id:
        cache_key = f"tvdb:{tvdb_id}:{category}"
    else:
        cache_key = f"{title}:{category}"
    hit = _TMDB_CACHE.get(cache_key)
    if hit and (time.monotonic() - hit[2]) < _TMDB_CACHE_TTL:
        return hit[0], hit[1]

    if category == "music":
        poster = await _fetch_deezer_artist(title, mbid=mbid)
        _TMDB_CACHE[cache_key] = (poster, None, time.monotonic())
        return poster, None

    tmdb_key = settings.TMDB_API_KEY
    if not tmdb_key:
        return None, None
    media_type = "movie" if category == "movie" else "tv"

    def _from_entry(data: dict) -> tuple:
        """Pull (poster, synopsis) out of a full TMDB movie/tv object."""
        poster = (
            f"https://image.tmdb.org/t/p/w92{data['poster_path']}"
            if data.get("poster_path") else None
        )
        synopsis = (data.get("overview") or "").strip() or None
        return poster, synopsis

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            if tmdb_id:
                # Deterministic path — query the exact entry by ID.
                r = await client.get(
                    f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}",
                    params={"api_key": tmdb_key},
                )
                if r.status_code != 200:
                    return None, None
                poster, synopsis = _from_entry(r.json())
                _TMDB_CACHE[cache_key] = (poster, synopsis, time.monotonic())
                return poster, synopsis

            # Pass 52: no tmdb_id but a tvdb_id (Sonarr). Resolve it via
            # TMDB's /find before falling back to the fuzzy title search.
            # /find returns the TMDB entry directly — still deterministic.
            if tvdb_id and media_type == "tv":
                rf = await client.get(
                    f"https://api.themoviedb.org/3/find/{tvdb_id}",
                    params={"api_key": tmdb_key, "external_source": "tvdb_id"},
                )
                if rf.status_code == 200:
                    tv_results = rf.json().get("tv_results", [])
                    if tv_results:
                        poster, synopsis = _from_entry(tv_results[0])
                        _TMDB_CACHE[cache_key] = (poster, synopsis, time.monotonic())
                        return poster, synopsis
                # /find whiffed — fall through to the title search.

            # Fallback path — title search. Year-aware when available so
            # franchise entries don't collapse onto results[0].
            params = {"api_key": tmdb_key, "query": title, "page": 1}
            if year:
                params["year" if media_type == "movie" else "first_air_date_year"] = year
            r = await client.get(
                f"https://api.themoviedb.org/3/search/{media_type}",
                params=params,
            )
        if r.status_code != 200:
            return None, None
        results = r.json().get("results", [])
        if not results:
            return None, None
        poster, synopsis = _from_entry(results[0])
        _TMDB_CACHE[cache_key] = (poster, synopsis, time.monotonic())
        return poster, synopsis
    except Exception:
        return None, None


async def _fetch_deezer_artist(artist_name: str, mbid: str | None = None) -> Optional[str]:
    """Fetch artist image from Deezer (free, no key). Returns picture_medium URL or None.

    Pass 52: when ``mbid`` is supplied (Lidarr items carry the MusicBrainz
    artist MBID as ``foreignArtistId``), resolve the EXACT Deezer artist
    via MusicBrainz url-relationships and hit ``/artist/{id}`` directly —
    deterministic, no popularity-sorted ``/search/artist`` guess. This is
    the music-domain equivalent of the Pass 51 TMDB-by-ID fix. Falls back
    to the name search when there's no MBID or no linked Deezer profile.
    """
    # Deterministic path: MBID → Deezer ID → exact artist.
    if mbid:
        try:
            from src.services.music_metadata import fetch_deezer_id_via_mbid
            deezer_id = await fetch_deezer_id_via_mbid(mbid)
            if deezer_id:
                async with httpx.AsyncClient(timeout=8) as client:
                    r = await client.get(f"https://api.deezer.com/artist/{deezer_id}")
                if r.status_code == 200:
                    d = r.json()
                    pic = d.get("picture_medium") or d.get("picture")
                    if pic and "default_artist" not in pic:
                        return pic
            # No Deezer link / lookup whiffed — fall through to name search.
        except Exception:
            pass

    # Fallback path: name search (popularity-sorted — best-effort).
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                "https://api.deezer.com/search/artist",
                params={"q": artist_name, "limit": 1},
            )
        if r.status_code != 200:
            return None
        data = r.json().get("data", [])
        if not data:
            return None
        pic = data[0].get("picture_medium") or data[0].get("picture")
        # Deezer returns a generic silhouette for unknown artists — skip it
        if pic and "default_artist" not in pic:
            return pic
    except Exception:
        pass
    return None


async def _fetch_poster_batch(titles_cats: list) -> dict:
    """Fetch posters for multiple titles concurrently. Returns {title: url}."""
    import asyncio
    sem = asyncio.Semaphore(5)

    async def fetch_one(title, cat):
        async with sem:
            return title, await _fetch_poster(title, cat)

    results = await asyncio.gather(*[fetch_one(t, c) for t, c in titles_cats])
    return {t: u for t, u in results if u}


@router.get("/by-category")
async def get_recommendations_by_category(
    limit: int = Query(5),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from src.database.models import CachedRecommendation
    cats = CATEGORIES
    result = {}
    for cat in cats:
        cached = db.query(CachedRecommendation).filter(
            CachedRecommendation.user_id == user.id,
            CachedRecommendation.category == cat,
        ).order_by(CachedRecommendation.confidence.desc()).limit(limit).all()

        if cached:
            result[cat] = [
                {"title": r.title, "reason": r.reason, "confidence": r.confidence,
                 "genres": r.genres, "poster_url": r.poster_url,
                 "category": cat}
                for r in cached
            ]
    return {"by_category": result}


_CATEGORY_TO_SERVICE = {"movie": "radarr", "music": "lidarr", "show": "sonarr", "anime": "sonarr"}


def _proposal_dict(p: DeletionProposal) -> dict:
    return {
        "id": p.id, "title": p.title, "pitch": p.reason,
        "confidence": p.confidence, "service": p.service,
        "arr_url": p.arr_url, "size_gb": round(p.storage_mb / 1024, 2),
        "status": p.status, "user_comment": p.user_comment,
        "category": p.category,
        "poster_url": p.poster_url, "synopsis": p.synopsis, "genres": p.genres,
        # Pass 17: file-level activity timestamp powers the "🆕 Just-arrived"
        # filter in the UI. ISO string (or None) so the frontend can show
        # an "added Xd ago" badge per row.
        "latest_activity_at": p.latest_activity_at.isoformat() if p.latest_activity_at else None,
    }


@router.get("/protections")
async def list_judge_protections(
    user: User = Depends(require_admin),   # curation = admin only
    db: Session = Depends(get_db),
):
    """Admin debug view: every title the 3-pillar judge auto-protected, with the
    pillar Begründung — so the admin can see WHY a title was saved and lift it."""
    from src.database.models import ProtectedMedia
    rows = (
        db.query(ProtectedMedia)
        .filter(ProtectedMedia.user_id == user.id, ProtectedMedia.source == "judge")
        .order_by(ProtectedMedia.created_at.desc())
        .all()
    )
    return {
        "protections": [
            {
                "id": p.id,
                "title": p.title or p.identifier,
                "category": p.category,
                "verdict": p.verdict,
                "reason": p.reason,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in rows
        ]
    }


@router.delete("/protections/{protection_id}")
async def delete_judge_protection(
    protection_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Lift a judge protection. The title re-enters the candidate pool on the
    next scan, where the judge may re-protect it or now cut it."""
    from src.database.models import ProtectedMedia
    row = (
        db.query(ProtectedMedia)
        .filter(ProtectedMedia.id == protection_id, ProtectedMedia.user_id == user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Protection not found")
    title = row.title or row.identifier
    db.delete(row)
    db.commit()
    return {"status": "deleted", "title": title}


@router.get("/deletions")
async def get_deletion_proposals(
    category: Optional[str] = Query(None),
    refresh: bool = Query(False),
    # Pass 17: "🆕 Just-arrived" filter — show only proposals whose
    # latest file-level activity is within ``recent_days`` (default 7).
    # When on, results are sorted by latest_activity_at desc so the
    # newest activity bubbles to the top.
    recent_only: bool = Query(False),
    recent_days: int = Query(7, ge=1, le=90),
    user: User = Depends(require_admin),   # deletions = admin curation only
    db: Session = Depends(get_db),
):
    from src.services.recommendations_engine import generate_deletion_proposals
    from src.database.models import ProtectedMedia

    if not refresh:
        protected = {
            p.identifier
            for p in db.query(ProtectedMedia).filter(ProtectedMedia.user_id == user.id).all()
        }
        q = db.query(DeletionProposal).filter(
            DeletionProposal.user_id == user.id,
            DeletionProposal.status.in_(["pending", "limbo"]),  # show limbo for retry
        )
        if category and category in _CATEGORY_TO_SERVICE:
            # Filter by stored category first, fall back to service for old rows
            q = q.filter(
                (DeletionProposal.category == category) |
                (
                    (DeletionProposal.category.is_(None)) &
                    (DeletionProposal.service == _CATEGORY_TO_SERVICE[category])
                )
            )
        # Pass 17: "🆕 Just-arrived" filter + sort by activity descending
        if recent_only:
            from datetime import timedelta
            cutoff = datetime.utcnow() - timedelta(days=recent_days)
            q = q.filter(DeletionProposal.latest_activity_at != None,
                         DeletionProposal.latest_activity_at >= cutoff)
            cached = q.order_by(DeletionProposal.latest_activity_at.desc()).limit(30).all()
        else:
            cached = q.order_by(DeletionProposal.confidence.desc()).limit(30).all()
        cached = [p for p in cached if p.title not in protected]

        # Mark cache as stale (but still serve) when the underlying data has
        # changed since the oldest pending proposal was written. UI can offer
        # a "Refresh" button; we DON'T auto-regenerate because that's a heavy
        # ARR scan + LLM run.
        from src.services.app_state import get_datetime as _get_dt
        invalidate_at = _get_dt("recs_invalidate_at")
        cache_stale = bool(
            cached and invalidate_at
            and min(p.created_at for p in cached if p.created_at) < invalidate_at
        )

        # Always return on non-refresh — never fall through to a live ARR fetch.
        # An empty result just shows the "Analyse library" prompt; it never
        # triggers a network call that can fail and wipe the loading state.
        return {
            "proposals": [_proposal_dict(p) for p in cached],
            "total_gb": round(sum(p.storage_mb for p in cached) / 1024, 1),
            "enrichment_coverage": _arr_enrichment_coverage(category),
            "stale": cache_stale,
            **({"message": "No proposals yet. Click 'Analyse library' to generate."}
               if not cached else {}),
        }

    arr_items = await _fetch_arr_candidates(category)
    if not arr_items:
        return {"proposals": [], "total_gb": 0,
                "message": "No ARR services configured or no candidates found."}

    # Pass 25b: visibility into the candidate pool. "Only 1 proposal returned"
    # could mean (a) tiny library after filtering, (b) very tight taste fit,
    # (c) recent_only filter culled everything. The logs below make each
    # stage countable.
    _per_svc_in = {}
    for it in arr_items:
        s = it.get("service") or "?"
        _per_svc_in[s] = _per_svc_in.get(s, 0) + 1
    logger.info(
        "[deletions] candidates pre-scoring: %d total %s (category=%s)",
        len(arr_items), _per_svc_in, category or "all",
    )

    # Pass 17: enrich each arr item with its latest file-level activity
    # timestamp (when the most recent episode/movie/track file was actually
    # imported, not when the parent series/artist was added to the arr).
    # Lookup is one history call per arr — runs in parallel.
    import asyncio
    services_needed = {i.get("service") for i in arr_items if i.get("service")}
    activity_maps = await asyncio.gather(*[
        _fetch_arr_recent_imports(svc) for svc in services_needed
    ])
    activity_by_svc = dict(zip(services_needed, activity_maps))
    activity_hits = 0
    for item in arr_items:
        svc = item.get("service")
        arr_id = item.get("arr_id")
        if svc and arr_id:
            item["latest_activity_at"] = activity_by_svc.get(svc, {}).get(arr_id)
            if item["latest_activity_at"]:
                activity_hits += 1
    logger.info(
        "[deletions] activity-stamps: %d/%d candidates have latest_activity_at set "
        "(others NULL — won't survive recent_only filter)",
        activity_hits, len(arr_items),
    )

    # Pass 26: when "🆕 Just-arrived only" is ON, narrow the candidate pool
    # to recent-import items BEFORE scoring/pitch-generation. Without this,
    # we'd score all 9000+ items globally, generate pitches for the top-10
    # mismatch fits, and then post-filter — leaving the user with maybe 1
    # of 10 pitches because the global-worst-fits rarely overlap with
    # recent imports. With pre-filtering the LLM only writes pitches for
    # items the user might actually act on.
    if recent_only:
        from datetime import timedelta as _td
        _cutoff = datetime.utcnow() - _td(days=recent_days)
        def _is_recent(it):
            la = it.get("latest_activity_at")
            if not la:
                return False
            try:
                dt = datetime.fromisoformat(str(la).replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None)
                return dt >= _cutoff
            except Exception:
                return False
        _pre = len(arr_items)
        arr_items = [it for it in arr_items if _is_recent(it)]
        logger.info(
            "[deletions] recent_only pre-filter: narrowed candidate pool %d → %d "
            "(last %dd) — scoring runs on this subset only",
            _pre, len(arr_items), recent_days,
        )
        if not arr_items:
            return {
                "proposals": [], "total_gb": 0,
                "message": (
                    f"No items imported in the last {recent_days} days. "
                    f"Untoggle '🆕 Just-arrived only' to evaluate the full library, "
                    f"or widen the window."
                ),
            }

    # ── Generate proposals ────────────────────────────────────────────────────
    # When category=None ("All"), run a separate pass per domain so each batch
    # is scored against the correct taste vector and the cache is populated for
    # every individual category tab, not just a random top-10 across all types.

    if not category:
        # Run sequentially — Ollama is single-threaded and can't handle concurrent
        # 27B curator calls without returning 500s.
        cats_present = list({i["category"] for i in arr_items if i.get("category")})
        proposals = []
        for cat in cats_present:
            cat_items = [i for i in arr_items if i.get("category") == cat]
            if cat_items:
                proposals.extend(
                    await generate_deletion_proposals(user.id, cat_items, cat)
                )
    else:
        proposals = await generate_deletion_proposals(user.id, arr_items, category)

    # Enrich each proposal with poster, synopsis, genres from TMDB + ARR metadata
    item_map = {i["title"]: i for i in arr_items}

    async def _enrich(p: dict) -> dict:
        orig = item_map.get(p["title"], {})
        cat = orig.get("category", category or "movie")
        genres = orig.get("genres", "")
        # Pass 51/52: hand _fetch_tmdb every stable ID the ARR item carries
        # so it can resolve the EXACT entry instead of guessing from a
        # title search — tmdb_id (Radarr / Sonarr), tvdb_id (Sonarr
        # fallback via /find), musicbrainz_id (Lidarr → Deezer bridge).
        poster, synopsis = await _fetch_tmdb(
            p["title"], cat,
            tmdb_id=orig.get("tmdb_id"),
            year=orig.get("year"),
            tvdb_id=orig.get("tvdb_id"),
            mbid=orig.get("musicbrainz_id"),
        )
        # Pass 51: ARR (Radarr/Sonarr) already returns an ``overview`` per
        # item — it's the correct synopsis for THIS exact entry. Prefer the
        # TMDB-by-ID synopsis, but fall back to the ARR text before giving
        # up. A blank synopsis card when ARR had the description all along
        # is the worst outcome.
        if not synopsis:
            synopsis = (orig.get("overview") or "").strip() or None
        return {**p, "category": cat, "genres": genres, "poster_url": poster, "synopsis": synopsis}

    enriched = await asyncio.gather(*[_enrich(p) for p in proposals])

    if not enriched:
        # Generation produced nothing — keep whatever is in the DB rather than
        # wiping it, so the user still sees the last known proposals.
        return {"proposals": [], "total_gb": 0,
                "message": "Analysis returned no candidates. Previous proposals retained."}

    with get_db_session() as dbs:
        # Only now — after successful generation — remove the stale proposals.
        # This prevents the "wiped cache with no replacement" failure mode.
        from sqlalchemy import or_, and_

        old_q = dbs.query(DeletionProposal).filter(
            DeletionProposal.user_id == user.id,
            DeletionProposal.status == "pending",
        )
        if category and category in _CATEGORY_TO_SERVICE:
            svc = _CATEGORY_TO_SERVICE[category]
            # Match rows where category column is set correctly OR where category
            # is NULL (legacy rows written before the column existed) but the
            # service matches.  This cleans up scheduler-generated NULL-category
            # rows without accidentally deleting "show" rows when refreshing "anime"
            # (both share sonarr — the category column disambiguates).
            old_q = old_q.filter(
                or_(
                    DeletionProposal.category == category,
                    and_(
                        DeletionProposal.category.is_(None),
                        DeletionProposal.service == svc,
                    ),
                )
            )
        # Pass 90b: SOFT delete via status='superseded' instead of hard
        # ``DELETE``. The hard-delete freed ROWIDs that SQLite (without
        # AUTOINCREMENT) reused for the new rows below — and stale
        # frontend caches that still held an old proposal_id then
        # silently pointed at a DIFFERENT title in the new batch
        # (the cross-render bug Pass 90a documented). Soft-delete
        # preserves the IDs (no reuse can happen for these rows even
        # without AUTOINCREMENT) AND gives us an audit trail of
        # superseded proposals. All status filters elsewhere in the
        # codebase look for ``pending`` / ``limbo`` / ``rejected`` /
        # ``deleted`` so ``superseded`` rows are silently ignored by
        # the UI and the deletion flows — exactly what we want.
        old_q.update(
            {"status": "superseded", "resolved_at": datetime.utcnow()},
            synchronize_session=False,
        )

        saved = []
        for p in enriched:
            # Pass 17: parse latest_activity_at iso string back to datetime
            # (or None if the helper couldn't fill it).
            la = p.get("latest_activity_at")
            la_dt = None
            if la:
                try:
                    la_dt = datetime.fromisoformat(str(la).replace("Z", "+00:00"))
                    if la_dt.tzinfo is not None:
                        la_dt = la_dt.replace(tzinfo=None)
                except Exception:
                    la_dt = None
            row = DeletionProposal(
                user_id=user.id, media_id=str(p.get("arr_id", "")),
                title=p["title"], service=p.get("service", ""),
                arr_url=p.get("arr_url", ""), reason=p["pitch"],
                confidence=p["confidence"], storage_mb=p.get("size_mb", 0),
                status="pending",
                category=p.get("category"),
                poster_url=p.get("poster_url"),
                synopsis=p.get("synopsis"),
                genres=p.get("genres"),
                tvdb_id=p.get("tvdb_id"),
                tmdb_id=p.get("tmdb_id"),
                latest_activity_at=la_dt,
            )
            dbs.add(row)
            saved.append((p, row))
        dbs.flush()
        proposals_with_ids = [
            {**p, "id": row.id, "size_gb": round((p.get("size_mb") or 0) / 1024, 1)}
            for p, row in saved
        ]
        dbs.commit()

    # Pass 24: apply the same recent_only filter+sort the read path uses,
    # so an Analyse run with the "🆕 Just-arrived" toggle ON returns the
    # SAME shape of data the user will see when they leave the view and
    # come back. Without this, refresh=true ignored recent_only entirely,
    # frontend rendered all proposals (the "pretty list"), and the next
    # reload (refresh=false applies the filter) silently culled most rows
    # → impression that the list "got lost" between turns.
    pre_filter_count = len(proposals_with_ids)
    if recent_only:
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(days=recent_days)

        def _within_recent(p):
            la = p.get("latest_activity_at")
            if not la:
                return False
            try:
                dt = datetime.fromisoformat(str(la).replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None)
                return dt >= cutoff
            except Exception:
                return False

        proposals_with_ids = sorted(
            [p for p in proposals_with_ids if _within_recent(p)],
            key=lambda p: p.get("latest_activity_at") or "",
            reverse=True,
        )
    # Pass 25b: final-count visibility. If pre==post, recent_only was off
    # (or every proposal had a recent timestamp). If post << pre and toggle
    # was on, the filter is what's culling — not a bug, just visibility.
    logger.info(
        "[deletions] regeneration done — wrote %d proposals (recent_only=%s, returning %d after filter)",
        pre_filter_count, recent_only, len(proposals_with_ids),
    )

    return {
        "proposals":           proposals_with_ids,
        "total_gb":            round(sum(p.get("size_mb", 0) for p in proposals_with_ids) / 1024, 1),
        "enrichment_coverage": _arr_enrichment_coverage(category),
    }


@router.post("/deletions/{proposal_id}/comment")
async def update_comment(
    proposal_id: int, comment: str,
    user: User = Depends(require_admin),   # deletions = admin curation only
    db: Session = Depends(get_db),
):
    p = db.query(DeletionProposal).filter(
        DeletionProposal.id == proposal_id,
        DeletionProposal.user_id == user.id
    ).first()
    if not p:
        raise HTTPException(404, "Not found")
        
    p.user_comment = comment
    db.commit()

    is_kept = False
    if comment:
        from src.services.episodic_memory import analyze_deletion_comment
        is_kept = await analyze_deletion_comment(user.id, p.title, comment)

    return {"ok": True, "is_kept": is_kept}

def _latest_curator_stance_for_proposal(
    db: Session,
    user_id: int,
    proposal_id: int,
    fallback_pitch: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(stance_text, polarity)`` for a deletion-proposal resolution.

    Pass 81e: the proposal's static ``reason`` field is the pitch at
    proposal-time. After a Level-2 reevaluation (or even a plain Discuss
    thread where the curator updated its position), the LATEST assistant
    message in the ``deletion_proposal:{id}`` thread is the curator's
    actual final stance — not the stale pitch. We capture that here so
    ``CuratorResolutionLog.curator_stance`` reflects what the curator
    really said at decision time.

    ``polarity`` is parsed from the Level-2 verdict tokens we ask the LLM
    to emit ("CONFIRMED" / "REVERSED"):

      "REVERSED"  — curator flipped to keep. If the user still DELETES,
                    that's an override (user overrode the curator's
                    reversal). If the user KEEPS, that's consensus.
      "CONFIRMED" — curator held the original delete line. Delete →
                    consensus. Keep → override.
      None        — no Level-2 verdict markers found, or no chat history
                    at all. Caller falls back to the existing default
                    (deletion = consensus, keep = override).

    Stance text is capped at 500 chars to match the CuratorResolutionLog
    column convention.
    """
    msg = (
        db.query(ConversationMessage)
        .filter(
            ConversationMessage.user_id == user_id,
            ConversationMessage.thread_id == f"deletion_proposal:{proposal_id}",
            ConversationMessage.role == "assistant",
        )
        .order_by(ConversationMessage.id.desc())
        .first()
    )

    if msg and (msg.content or "").strip():
        content = msg.content.strip()
        # Polarity precedence: REVERSED beats CONFIRMED. The Level-2 framing
        # asks for one token only; if both appear (e.g. "I won't REVERSE,
        # CONFIRMING the original"), the user-meaningful signal is whether
        # the verdict ultimately flipped — "REVERSED" present means it did.
        upper = content.upper()
        if "REVERSED" in upper:
            polarity = "REVERSED"
        elif "CONFIRMED" in upper or "CONFIRM DELETION" in upper:
            polarity = "CONFIRMED"
        else:
            polarity = None
        return content[:500], polarity

    # No chat history — fall back to the original pitch. Polarity stays None
    # because no verdict-token reasoning happened.
    return (fallback_pitch.strip()[:500] if fallback_pitch else None), None


@router.post("/deletions/{proposal_id}/approve")
async def approve_deletion(
    proposal_id: int,
    user: User = Depends(require_admin),   # deletions = admin curation only
    db: Session = Depends(get_db),
):
    p = db.query(DeletionProposal).filter(
        DeletionProposal.id == proposal_id,
        DeletionProposal.user_id == user.id,
        DeletionProposal.status.in_(["pending", "limbo"]),
    ).first()
    if not p:
        raise HTTPException(404, "Not found")

    # Probe before committing to a destructive, irreversible action.
    reachable = await _probe_arr(p.service)
    if not reachable:
        # Keep the proposal alive in "limbo" — the user can retry at any time.
        p.status = "limbo"
        db.commit()
        service_name = p.service.capitalize()
        return {
            "ok": False,
            "limbo": True,
            "status": "limbo",
            "error": f"{service_name} is currently unreachable. The item has NOT been deleted and can be retried.",
        }

    success = await _execute_arr_delete(p)
    p.status = "deleted" if success else "error"
    p.resolved_at = datetime.utcnow()

    # Pass 66 / 81e: log the resolution.
    #
    # Originally (Pass 66) this was hardcoded to ``resolution_type="consensus"``
    # with ``curator_stance = p.reason`` — fine when the proposal pitch
    # WAS the curator's final word. But with Level-2 reevaluation (Pass 81)
    # the curator's last word can be either a CONFIRM (still wants delete
    # — true consensus) or a REVERSAL (now wants to keep — user deleting
    # anyway is an OVERRIDE). The static ``p.reason`` field can't represent
    # that; the chat history can.
    #
    # 81e: pull the latest assistant message from the deletion_proposal
    # thread as the canonical stance, parse the verdict polarity from it,
    # and classify resolution_type accordingly. No chat history → falls
    # back to the original ``p.reason`` + ``"consensus"`` (unchanged
    # behaviour for the click-without-discussion path).
    if success:
        try:
            stance, polarity = _latest_curator_stance_for_proposal(
                db, user.id, p.id, fallback_pitch=p.reason,
            )
            if polarity == "REVERSED":
                resolution_type = "override"
                override_reason = "Deleted despite Level-2 curator reversal"
            else:
                resolution_type = "consensus"
                override_reason = None
            db.add(CuratorResolutionLog(
                user_id=user.id,
                title=p.title,
                category=p.category,
                outcome="deleted",
                resolution_type=resolution_type,
                curator_stance=stance,
                override_reason=override_reason,
            ))
            logger.info(
                "📒 [RESOLUTION LOG] user=%d '%s' deleted (%s)%s",
                user.id, p.title, resolution_type,
                f" — {override_reason}" if override_reason else "",
            )
        except Exception as e:
            logger.debug("[deletion] resolution-log write failed: %s", e)

    db.commit()
    return {"ok": success, "limbo": False, "status": p.status}


@router.post("/deletions/{proposal_id}/reject")
async def reject_deletion(
    proposal_id: int,
    user: User = Depends(require_admin),   # deletions = admin curation only
    db: Session = Depends(get_db),
):
    """User clicks Keep on the deletion-proposal card.

    Pass 85: writes a ``CuratorResolutionLog`` entry, symmetric to the
    ``/approve`` handler from Pass 81e. Without this, card-button Keeps
    were invisible to the year-in-review / stats — only in-chat keeps
    captured via ``handle_protection_intent`` ever landed in the log.
    The polarity table mirrors the /approve side:

      Latest curator stance → resolution_type
        REVERSED   (curator now wants keep)   → consensus
        CONFIRMED  (curator still wants delete) → override (user overrode)
        no chat                                → override (overrode pitch)

    Idempotent: if the proposal is already "rejected" we return OK without
    writing a duplicate log row. That covers the case where the chat
    ``handle_protection_intent`` path rejected the proposal first and
    already wrote its own (better-classified) log entry — re-clicking the
    card button after the fact shouldn't create a phantom second row.
    """
    p = db.query(DeletionProposal).filter(
        DeletionProposal.id == proposal_id,
        DeletionProposal.user_id == user.id,
    ).first()
    if not p:
        raise HTTPException(404, "Not found")

    if p.status == "rejected":
        # Already settled — most likely by the chat protection-intent path
        # which has its own log-writing. Don't write a duplicate.
        return {"ok": True, "already_rejected": True}

    p.status = "rejected"
    p.resolved_at = datetime.utcnow()

    try:
        stance, polarity = _latest_curator_stance_for_proposal(
            db, user.id, p.id, fallback_pitch=p.reason,
        )
        if polarity == "REVERSED":
            resolution_type = "consensus"
            override_reason = None
        else:
            # Either CONFIRMED (curator held the delete line) or no chat
            # history at all — either way the user clicking Keep is an
            # override of the standing delete pitch.
            resolution_type = "override"
            override_reason = "Card-button keep"
        db.add(CuratorResolutionLog(
            user_id=user.id,
            title=p.title,
            category=p.category,
            outcome="kept",
            resolution_type=resolution_type,
            curator_stance=stance,
            override_reason=override_reason,
        ))
        logger.info(
            "📒 [RESOLUTION LOG] user=%d '%s' kept (%s)%s",
            user.id, p.title, resolution_type,
            f" — {override_reason}" if override_reason else "",
        )
    except Exception as e:
        logger.debug("[deletion] resolution-log write failed: %s", e)

    db.commit()
    return {"ok": True}


# ── ARR HELPERS ───────────────────────────────────────────────────────────────

def _arr_enrichment_coverage(category: Optional[str] = None) -> dict:
    """
    Return enrichment coverage stats for the ARR library.

    Reads ArrEnrichmentStatus (written by the enrichment pipeline) to count
    how many downloaded ARR items have been LLM-enriched.  Returned with every
    deletion-proposals response so the frontend can show a coverage warning when
    enrichment hasn't run yet (or is substantially incomplete).

    Returns:
        {
          "enriched": N,        # items with LLM profile
          "total":    M,        # all ArrEnrichmentStatus rows (ever attempted)
          "pct":      K,        # enriched / total * 100
          "low":      bool,     # True when pct < 50 — show warning
          "never_run": bool,    # True when total == 0 (enrichment never ran)
        }
    """
    try:
        from src.database.connection import get_db_session
        from src.database.models import ArrEnrichmentStatus

        _CAT_TO_SVC = {
            "movie": ["radarr"],
            "show": ["sonarr"],
            "anime": ["sonarr"],
            "music": ["lidarr"],
        }

        with get_db_session() as db:
            q = db.query(ArrEnrichmentStatus)
            if category and category in _CAT_TO_SVC:
                svcs = _CAT_TO_SVC[category]
                q = q.filter(ArrEnrichmentStatus.service.in_(svcs))
                if category in ("show", "anime"):
                    q = q.filter(ArrEnrichmentStatus.category == category)
            total    = q.count()
            enriched = q.filter(ArrEnrichmentStatus.enriched == True).count()

        pct = round(100 * enriched / max(total, 1))
        return {
            "enriched":   enriched,
            "total":      total,
            "pct":        pct,
            "low":        pct < 50,
            "never_run":  total == 0,
        }
    except Exception:
        return {"enriched": 0, "total": 0, "pct": 0, "low": True, "never_run": True}


async def _fetch_arr_recent_imports(svc: str, days: int = 60) -> dict[int, str]:
    """Return ``{arr_item_id: latest_imported_iso}`` from arr ``/history``.

    Pass 20 (after Sonarr/Radarr/Lidarr OpenAPI sweep):
      ``eventType`` is ``array<integer>`` per the spec, not a string —
      that's why the Pass 17 string form ("downloadFolderImported")
      returned 400. Pass 19 worked around it by dropping the filter and
      filtering client-side. Now we use the documented integer codes.

      All three arrs use **eventType=3** for "import succeeded":

        Sonarr v3   EpisodeHistoryEventType.DownloadFolderImported = 3
        Radarr v3   MovieHistoryEventType.DownloadFolderImported   = 3
        Lidarr v1   EntityHistoryEventType.TrackFileImported       = 3

      Convenient — one server-side filter, all three services. The
      client-side check stays as a defensive belt-and-braces against
      version drift (some installs return eventType as enum-int in the
      response body, others as the enum-string name).

    Returns ``{}`` on any failure — proposals still get generated, just
    without the activity timestamp (filter shows them under "All").
    """
    from datetime import timedelta
    # event_type_int is what we send; event_type_name is the per-service
    # string we ALSO accept in the client-side defensive check.
    if svc == "sonarr":
        url_base, api_key = settings.SONARR_URL, settings.SONARR_API_KEY
        path = "/api/v3/history"
        event_type_int, event_type_name = 3, "downloadFolderImported"
        id_field = "seriesId"
    elif svc == "radarr":
        url_base, api_key = settings.RADARR_URL, settings.RADARR_API_KEY
        path = "/api/v3/history"
        event_type_int, event_type_name = 3, "downloadFolderImported"
        id_field = "movieId"
    elif svc == "lidarr":
        url_base, api_key = settings.LIDARR_URL, settings.LIDARR_API_KEY
        path = "/api/v1/history"
        event_type_int, event_type_name = 3, "trackFileImported"
        id_field = "artistId"
    else:
        return {}

    if not url_base or not api_key:
        return {}

    # Server-side eventType filter: the OpenAPI spec defines it as
    # ``array of integers``, so we pass the integer code that all three
    # arrs use for "import succeeded" (= 3). pageSize 250 covers ~60d
    # of typical-library imports without hitting per-call response caps.
    params = {"pageSize": 250, "page": 1, "eventType": event_type_int}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{url_base.rstrip('/')}{path}",
                params=params,
                headers={"X-Api-Key": api_key},
            )
        if r.status_code != 200:
            logger.info("[deletions] %s history → HTTP %d (skipping activity timestamps)",
                        svc, r.status_code)
            return {}
        records = (r.json() or {}).get("records") or []
    except Exception as e:
        logger.info("[deletions] %s history fetch failed: %s", svc, e)
        return {}

    cutoff = datetime.utcnow() - timedelta(days=days)
    result: dict[int, str] = {}
    matched_events = 0
    for rec in records:
        # Defensive client-side filter. After server-side filtering
        # everything SHOULD be the right eventType, but we accept either
        # the int form (==3) or the per-service string form to survive
        # API version drift.
        evt = rec.get("eventType")
        if evt != event_type_int and evt != event_type_name:
            continue
        matched_events += 1
        item_id  = rec.get(id_field)
        date_str = rec.get("date")
        if not item_id or not date_str:
            continue
        try:
            # Strip timezone — arr returns ISO with "Z" or offset, we store
            # naive UTC to stay consistent with the rest of the schema.
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
        except Exception:
            continue
        if dt < cutoff:
            continue
        # Be defensive about ordering — keep the latest per item_id.
        prev_iso = result.get(item_id)
        cur_iso  = dt.isoformat()
        if not prev_iso or cur_iso > prev_iso:
            result[item_id] = cur_iso
    logger.info(
        "[deletions] %s recent-imports map: %d items active in last %dd "
        "(scanned %d records, %d matched eventType=%s/%d)",
        svc, len(result), days, len(records), matched_events,
        event_type_name, event_type_int,
    )
    return result


async def _fetch_arr_candidates(category: str = None) -> list:
    """
    Fetch all items from ARR services.

    Each service (Radarr / Sonarr / Lidarr) is cached independently for
    ARR_CACHE_TTL seconds.  On a connection error the stale cache is used so a
    momentarily-unreachable service never wipes the candidate pool.
    """
    now = time.monotonic()
    candidates = []

    # ── RADARR ────────────────────────────────────────────────────────────────
    if (not category or category == "movie") and settings.RADARR_URL and settings.RADARR_API_KEY:
        hit = _ARR_CACHE.get("radarr", {})
        age = now - hit.get("at", 0)
        if hit and age < _ARR_CACHE_TTL:
            logger.debug("Radarr: serving %d items from cache (%.0fs old)", len(hit["items"]), age)
            candidates.extend(hit["items"])
        else:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(f"{settings.RADARR_URL.rstrip('/')}/api/v3/movie",
                        headers={"X-Api-Key": settings.RADARR_API_KEY})
                if r.status_code == 200:
                    items = []
                    for m in r.json():
                        if not m.get("hasFile"): continue
                        items.append({
                            "title": m.get("title", ""), "year": m.get("year"),
                            "genres": ", ".join(m.get("genres", [])[:4]),
                            "size_mb": m.get("sizeOnDisk", 0) / (1024 * 1024),
                            "service": "radarr", "arr_id": m.get("id"),
                            # Pass 64: plex_rating_key in the SAME shape the
                            # enrichment pipeline (_collect_arr_items) uses
                            # when it writes the ChromaDB embedding —
                            # "{service}:{arr_id}". generate_deletion_proposals
                            # builds its ChromaDB lookup doc_id as
                            # ``item.get("plex_rating_key") or tmdb_id or title``;
                            # without this key it fell through to tmdb_id and
                            # MISSED every embedding (stored under radarr:{id}),
                            # leaving distance_penalty pinned at the 0.5 default
                            # — the taste-vector mismatch score, the dominant
                            # deletion signal, was a flat constant.
                            "plex_rating_key": f"radarr:{m.get('id')}",
                            "arr_url": f"{settings.effective_radarr_url}/movie/{m.get('titleSlug', '')}",
                            "category": "movie",
                            "tmdb_id": m.get("tmdbId"),
                            "imdb_id": m.get("imdbId"),
                            "overview": m.get("overview", ""),
                            "ratings": m.get("ratings", {}),
                            "monitored": m.get("monitored", True),
                        })
                    _ARR_CACHE["radarr"] = {"items": items, "at": now}
                    logger.debug("Radarr: cached %d items", len(items))
                    candidates.extend(items)
                else:
                    # Pass 58: a non-200 used to be a silent no-op — the
                    # block above just got skipped and the user saw "no
                    # candidates" with no clue whether it was a bad key,
                    # wrong URL, or genuinely-empty library. Log it.
                    logger.warning(
                        "Radarr: /api/v3/movie returned HTTP %d — no movie "
                        "candidates this cycle (check URL / API key)",
                        r.status_code,
                    )
            except Exception as e:
                logger.warning("Radarr fetch failed: %s", e)
                if hit:
                    logger.warning("Radarr: falling back to stale cache (%.0fs old)", age)
                    candidates.extend(hit["items"])

    # ── SONARR ────────────────────────────────────────────────────────────────
    if (not category or category in ("show", "anime")) and settings.SONARR_URL and settings.SONARR_API_KEY:
        hit = _ARR_CACHE.get("sonarr", {})
        age = now - hit.get("at", 0)
        if hit and age < _ARR_CACHE_TTL:
            logger.debug("Sonarr: serving %d items from cache (%.0fs old)", len(hit["items"]), age)
            candidates.extend(hit["items"])
        else:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(f"{settings.SONARR_URL.rstrip('/')}/api/v3/series",
                        headers={"X-Api-Key": settings.SONARR_API_KEY})
                if r.status_code == 200:
                    from src.services.arr_client import classify_sonarr_category
                    items = []
                    size_zero = 0
                    for s in r.json():
                        # Pass 58: ``or {}`` — Sonarr returns "statistics":
                        # null for series with no episode files. The old
                        # ``.get("statistics", {})`` only substitutes when
                        # the KEY is missing, so a present-null value gave
                        # None and ``.get("sizeOnDisk")`` crashed the whole
                        # fetch (same class of bug as the Lidarr one this
                        # pass fixes — Sonarr just never tripped it because
                        # the user's series all have files).
                        stats = s.get("statistics") or {}
                        size = stats.get("sizeOnDisk", 0) or 0
                        if size <= 0:
                            size_zero += 1
                        genres = s.get("genres", [])
                        # Pass 59: shared classifier — this branch used to
                        # roll its own ``"Anime" in genres or seriesType``
                        # check while enrichment's _collect_arr_items used
                        # ``seriesType`` only. The drift cached a series
                        # under enriched:show:* on one path and looked it
                        # up under enriched:anime:* on the other, so a
                        # deletion discussion missed its own profile.
                        cat = classify_sonarr_category(s)
                        items.append({
                            "title": s.get("title", ""), "year": s.get("year"),
                            "genres": ", ".join(genres[:4]),
                            "size_mb": size / (1024 * 1024),
                            "service": "sonarr", "arr_id": s.get("id"),
                            # Pass 64: see the radarr branch — ChromaDB lookup
                            # doc_id match for the deletion-scoring vector
                            # comparison. Embeddings are stored under
                            # "sonarr:{id}" by the enrichment pipeline.
                            "plex_rating_key": f"sonarr:{s.get('id')}",
                            "arr_url": f"{settings.effective_sonarr_url}/series/{s.get('titleSlug', '')}",
                            "category": cat,
                            "tvdb_id": s.get("tvdbId"),
                            # Pass 51b: Sonarr v3 series objects DO carry a
                            # tmdbId — enrichment.py:439 already reads it for
                            # the exact same /api/v3/series payload. This
                            # path hard-coded None, so every Sonarr deletion
                            # proposal fell through to the fuzzy title
                            # search in _fetch_tmdb. Read the real value;
                            # it's None only for poorly-matched series, which
                            # then still hit the year-aware title fallback.
                            "tmdb_id": s.get("tmdbId"),
                            "imdb_id": s.get("imdbId"),
                            "overview": s.get("overview", ""),
                            "ratings": s.get("ratings", {}),
                            "sonarr_series_type": s.get("seriesType", "standard"),
                            "monitored": s.get("monitored", True),
                        })
                    _ARR_CACHE["sonarr"] = {"items": items, "at": now}
                    if size_zero:
                        # Pass 58: visibility — if this is ALL of them, the
                        # statistics field is missing wholesale (version
                        # drift) rather than just a few file-less series.
                        logger.info(
                            "Sonarr: cached %d items (%d with size 0 — kept, "
                            "won't score on size)", len(items), size_zero,
                        )
                    else:
                        logger.debug("Sonarr: cached %d items", len(items))
                    candidates.extend(items)
                else:
                    logger.warning(
                        "Sonarr: /api/v3/series returned HTTP %d — no show/anime "
                        "candidates this cycle (check URL / API key)",
                        r.status_code,
                    )
            except Exception as e:
                logger.warning("Sonarr fetch failed: %s", e)
                if hit:
                    logger.warning("Sonarr: falling back to stale cache (%.0fs old)", age)
                    candidates.extend(hit["items"])

    # ── LIDARR ────────────────────────────────────────────────────────────────
    if (not category or category == "music") and settings.LIDARR_URL and settings.LIDARR_API_KEY:
        hit = _ARR_CACHE.get("lidarr", {})
        age = now - hit.get("at", 0)
        if hit and age < _ARR_CACHE_TTL:
            logger.debug("Lidarr: serving %d items from cache (%.0fs old)", len(hit["items"]), age)
            candidates.extend(hit["items"])
        else:
            try:
                # Lidarr's /api/v1/artist computes per-artist statistics across
                # the whole library; on a large collection (measured: 5,211
                # artists, 62 MB, ~124 s) it blows far past the 15 s the other
                # ARRs need. The old flat 15 s timed out EVERY cycle, so music
                # deletion proposals never appeared. Generous read timeout, but
                # a short connect timeout so a genuinely-down Lidarr still fails
                # fast (→ stale-cache fallback below) instead of hanging.
                _lidarr_timeout = httpx.Timeout(240.0, connect=10.0)
                async with httpx.AsyncClient(timeout=_lidarr_timeout) as client:
                    r = await client.get(f"{settings.LIDARR_URL.rstrip('/')}/api/v1/artist",
                        headers={"X-Api-Key": settings.LIDARR_API_KEY})
                if r.status_code == 200:
                    items = []
                    size_zero = 0
                    for a in r.json():
                        # Pass 58: THE music-deletion-proposals bug. Lidarr
                        # returns "statistics": null for artists with no
                        # track files. ``a.get("statistics", {})`` only
                        # substitutes {} when the KEY is missing — a
                        # present-null value passed None straight through,
                        # and the next ``.get("sizeOnDisk")`` crashed the
                        # entire Lidarr fetch with 'NoneType' has no
                        # attribute 'get'. The outer except swallowed it,
                        # so music proposals came back empty while
                        # enrichment (which never touches statistics)
                        # worked fine — the exact asymmetry the user saw.
                        #
                        # ``or {}`` is null-safe. And we no longer ``continue``
                        # on size 0: a file-less artist isn't a storage win,
                        # but dropping it outright meant any Lidarr install
                        # where the list endpoint omits statistics wholesale
                        # returned zero candidates. Keep it (size_mb=0 just
                        # won't score on the size component) and log the
                        # count so a "everything is size 0" situation is
                        # visible instead of silent.
                        stats = a.get("statistics") or {}
                        size = stats.get("sizeOnDisk", 0) or 0
                        if size <= 0:
                            size_zero += 1
                        items.append({
                            "title": a.get("artistName", ""), "year": None,
                            # null-safe: Lidarr can send "genres": null (present
                            # but null), where .get("genres", []) returns None
                            # and None[:4] would crash the whole fetch — the same
                            # trap as the statistics field above.
                            "genres": ", ".join((a.get("genres") or [])[:4]),
                            "size_mb": size / (1024 * 1024),
                            "service": "lidarr", "arr_id": a.get("id"),
                            # Pass 64: see the radarr branch — ChromaDB lookup
                            # doc_id match for the deletion-scoring vector
                            # comparison. Embeddings are stored under
                            # "lidarr:{id}" by the enrichment pipeline.
                            "plex_rating_key": f"lidarr:{a.get('id')}",
                            "arr_url": f"{settings.effective_lidarr_url}/artist/{a.get('foreignArtistId', '')}",
                            "category": "music",
                            "musicbrainz_id": a.get("foreignArtistId"),
                            "monitored": a.get("monitored", True),
                        })
                    _ARR_CACHE["lidarr"] = {"items": items, "at": now}
                    if size_zero:
                        logger.info(
                            "Lidarr: cached %d items (%d with size 0 — kept, "
                            "won't score on size)", len(items), size_zero,
                        )
                    else:
                        logger.debug("Lidarr: cached %d items", len(items))
                    candidates.extend(items)
                else:
                    logger.warning(
                        "Lidarr: /api/v1/artist returned HTTP %d — no music "
                        "candidates this cycle (check URL / API key)",
                        r.status_code,
                    )
            except Exception as e:
                # Include the exception class — httpx.ReadTimeout str()s to ""
                # so the old message logged just "Lidarr fetch failed: " with
                # nothing after it, hiding the real (timeout) cause.
                logger.warning("Lidarr fetch failed: %s: %s",
                               type(e).__name__, e or "(no message — likely timeout)")
                if hit:
                    logger.warning("Lidarr: falling back to stale cache (%.0fs old)", age)
                    candidates.extend(hit["items"])

    # Apply category filter against cached items (needed when Sonarr cache was
    # populated for category=None and now we want only "show" or only "anime")
    if category:
        candidates = [c for c in candidates if c.get("category") == category]

    return candidates


async def _fetch_arr_unwatched(user_id: int, category: str = None) -> list:
    """
    Fetch ARR items that are downloaded but not yet watched — the candidate pool
    for the LIBRARY recommendation lane.

    Watched-matching goes through library_memory (stable id + normalised title,
    scoped to THIS category) instead of a raw exact-title set spanning every
    category. The old version matched across all media types, so a listened
    track named "Brazil" would wrongly hide the film "Brazil" from the movie lane.
    """
    all_items = await _fetch_arr_candidates(category)
    if not all_items:
        return []

    from src.services.library_memory import seen_index, is_seen
    seen = seen_index(user_id, category)
    unwatched = [item for item in all_items if not is_seen(item, seen)]
    logger.info("ARR unwatched: %d/%d items (cat=%s)",
                len(unwatched), len(all_items), category or "all")
    return unwatched


async def _probe_arr(service: str) -> bool:
    """
    Quick reachability check before a destructive delete.
    Hits the /system/status endpoint (read-only, fast) with a tight timeout.
    Returns True only when the service responds with HTTP 200.
    """
    _STATUS = {
        "radarr": (settings.RADARR_URL,  settings.RADARR_API_KEY,  "v3"),
        "sonarr": (settings.SONARR_URL,  settings.SONARR_API_KEY,  "v3"),
        "lidarr": (settings.LIDARR_URL,  settings.LIDARR_API_KEY,  "v1"),
    }
    if service not in _STATUS:
        return False
    base_url, api_key, ver = _STATUS[service]
    if not base_url or not api_key:
        return False
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(
                f"{base_url.rstrip('/')}/api/{ver}/system/status",
                headers={"X-Api-Key": api_key},
            )
        return r.status_code == 200
    except Exception:
        return False


async def _execute_arr_delete(p: DeletionProposal) -> bool:
    def _check(r, label: str) -> bool:
        if r.status_code in (200, 204):
            return True
        logger.error("[%s] delete HTTP %s — %s", label, r.status_code, r.text[:300])
        return False

    # Pass 58: every delete also adds an import-list exclusion. Without it,
    # an item managed by an *arr import list comes straight back on the
    # next list sync — Curatarr deletes it, the list re-adds it, forever.
    # The user explicitly OK'd this: "otherwise the two just keep fighting each other".
    # NOTE: the parameter name differs by service — Radarr calls it
    # ``addImportExclusion``, Sonarr and Lidarr ``addImportListExclusion``
    # (verified against each project's OpenAPI spec, not guessed).
    try:
        if p.service == "radarr" and settings.RADARR_URL and settings.RADARR_API_KEY:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.delete(
                    f"{settings.effective_radarr_url}/api/v3/movie/{p.media_id}",
                    headers={"X-Api-Key": settings.RADARR_API_KEY},
                    params={"deleteFiles": "true", "addImportExclusion": "true"})
            return _check(r, "radarr")
        if p.service == "sonarr" and settings.SONARR_URL and settings.SONARR_API_KEY:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.delete(
                    f"{settings.effective_sonarr_url}/api/v3/series/{p.media_id}",
                    headers={"X-Api-Key": settings.SONARR_API_KEY},
                    params={"deleteFiles": "true", "addImportListExclusion": "true"})
            return _check(r, "sonarr")
        if p.service == "lidarr" and settings.LIDARR_URL and settings.LIDARR_API_KEY:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.delete(
                    f"{settings.effective_lidarr_url}/api/v1/artist/{p.media_id}",
                    headers={"X-Api-Key": settings.LIDARR_API_KEY},
                    params={"deleteFiles": "true", "addImportListExclusion": "true"})
            return _check(r, "lidarr")
    except Exception as e:
        logger.error("[arr] delete failed: %s", e)
    return False
