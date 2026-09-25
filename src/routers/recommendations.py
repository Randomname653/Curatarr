"""
Curatarr - Recommendations & Deletions Router

All endpoints are category-aware and use the LLM for pitches.
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
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
    limit: int = Query(8, ge=1, le=50),
    refresh: bool = Query(False),
    source: str = Query("cache"),  # cache / library / external
    lane: Optional[str] = Query(None),  # filter cache to "library" / "discovery"
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cache reads are free. Generating (``refresh``, the library or external
    lane) is minutes of curator time on the shared GPU plus TMDB/OMDb calls
    per candidate — the same per-user budget and one-at-a-time guard as
    ``/refresh-cache``, or this GET was the way around them."""
    generating = refresh or source in ("library", "external")
    if not generating:
        return await _get_recommendations_impl(category, limit, refresh, source, lane, user, db)
    from src.services import rate_limit as _rl
    _rl.enforce("recs-refresh", user.id, _rl.RECS_REFRESH_PER_10MIN, 600,
                "Recommendations were generated recently — try again in a few minutes")
    _rl.RECS_REFRESH_IN_FLIGHT.enter_or_409(user.id)
    try:
        return await _get_recommendations_impl(category, limit, refresh, source, lane, user, db)
    finally:
        _rl.RECS_REFRESH_IN_FLIGHT.leave(user.id)


async def _get_recommendations_impl(category, limit, refresh, source, lane, user, db):
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
                     "year": r.year,   # library lane only; helps [+ Add] disambiguate
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
    """Manually trigger recommendation cache refresh.

    One refresh per user at a time (a second click while one runs → 409),
    and two starts per ten minutes: each refresh is minutes of curator time
    on the shared GPU, and a stacked queue of them is the cheapest way for
    one member token to own the LLM for an hour."""
    from src.services import rate_limit as _rl
    _rl.enforce("recs-refresh", user.id, _rl.RECS_REFRESH_PER_10MIN, 600,
                "Recommendations were refreshed recently — try again in a few minutes")
    _rl.RECS_REFRESH_IN_FLIGHT.enter_or_409(user.id)
    background_tasks.add_task(_run_cache_refresh, user.id)
    return {"status": "started", "message": "Generating recommendations in background…"}


async def _run_cache_refresh(user_id: int):
    from src.services.scheduler import _cache_recommendations
    from src.services import rate_limit as _rl
    try:
        await _cache_recommendations(user_id)
    finally:
        _rl.RECS_REFRESH_IN_FLIGHT.leave(user_id)


# ── POSTER / SYNOPSIS FETCHING ───────────────────────────────────────────────

async def _fetch_poster(title: str, category: str) -> Optional[str]:
    """Fetch poster URL from TMDB (w500). Returns URL or None."""
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

    # Pass 62: the arr's tmdbId is a SECONDARY cross-reference and Sonarr's
    # comes from TheTVDB, where it is sometimes mistyped — "Museum of Life"
    # (BBC documentary) carried 4054 instead of 40545 and this function
    # cheerfully returned the poster and plot of a 1999 Japanese melodrama.
    # TMDB's own tvdb index is the primary record; consult it first.
    if tvdb_id and media_type == "tv":
        try:
            from src.services.media_enricher import authoritative_tv_tmdb_id
            tmdb_id = await authoritative_tv_tmdb_id(tvdb_id, tmdb_id)
        except Exception as e:
            logger.debug("[recs] tvdb id authority check failed for %r: %s",
                         title, e)

    def _from_entry(data: dict) -> tuple:
        """Pull (poster, synopsis) out of a full TMDB movie/tv object."""
        # w500, not w92: the UI moved to large poster cards, and a 92px
        # source upscaled to card size is mush. One size for every consumer
        # (recs, deletions, add-new, recent-history) — this is the only
        # place in src/ that renders a TMDB image path into a URL.
        poster = (
            f"https://image.tmdb.org/t/p/w500{data['poster_path']}"
            if data.get("poster_path") else None
        )
        synopsis = (data.get("overview") or "").strip() or None
        return poster, synopsis

    def _wrong_work(data: dict) -> bool:
        """The entry we fetched by ID cannot be this title.

        ``year`` was passed in all along but only ever used to disambiguate
        the title SEARCH — the by-ID path took whatever came back, which is
        how a 2010 documentary ended up wearing a 1999 melodrama's plot on
        its deletion card. The enricher has carried this same delta-check
        since 0e0453f; the card path never got it. Returning nothing here is
        the right answer: _enrich_proposal then falls back to the ARR
        overview, which is correct for this exact entry by construction.
        """
        if not year:
            return False
        d = data.get("release_date") or data.get("first_air_date") or ""
        try:
            got = int(str(d)[:4])
        except (TypeError, ValueError):
            return False
        if not got or abs(got - int(year)) <= 5:
            return False
        logger.warning("[recs] TMDB id %s is %r (%s) but the arr says %r (%s) "
                       "— refusing the mismatched entry",
                       tmdb_id, data.get("name") or data.get("title"), got,
                       title, year)
        return True

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
                _entry = r.json()
                if _wrong_work(_entry):
                    return None, None
                poster, synopsis = _from_entry(_entry)
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


@router.get("/by-category")
async def get_recommendations_by_category(
    limit: int = Query(5, ge=1, le=50),
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
        # arr item id — the "Fix match" pin needs (service, arr_id)
        "media_id": p.media_id,
        "arr_url": p.arr_url, "size_gb": round(p.storage_mb / 1024, 2),
        "status": p.status, "user_comment": p.user_comment,
        "category": p.category, "stagnant": bool(p.stagnant),
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
    """Admin view: EVERY protection — judge auto-saves AND chat-intent grants —
    with reason and source, so the admin can see WHY a title was saved and can
    lift it. (The old source=='judge' filter made chat protections invisible:
    a chat-granted protection had NO place in the UI to undo.)"""
    from src.database.models import ProtectedMedia
    rows = (
        db.query(ProtectedMedia)
        .filter(ProtectedMedia.user_id == user.id)
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
                "source": p.source or "chat",
                "reason": p.reason,
                "arr_url": p.arr_url,
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


# ── Learned curation principles (the self-learning layer) ────────────────────

@router.get("/principles")
async def list_principles(
    user: User = Depends(require_admin),   # curation = admin only
    db: Session = Depends(get_db),
):
    """Admin view of what the curator has TAUGHT ITSELF from past debates —
    shadow (captured, not yet affecting judgments), active (injected into the
    judge), rejected. The one place the owner curates the self-learning; a
    'contradiction' novelty is the flagged human touch-point."""
    from src.database.models import CuratorPrinciple
    rows = (
        db.query(CuratorPrinciple)
        .filter(CuratorPrinciple.user_id == user.id)
        .order_by(CuratorPrinciple.created_at.desc())
        .all()
    )
    return {
        "principles": [
            {
                "id": p.id,
                "text": p.text,
                "basis": p.basis,
                "category": p.category,
                "status": p.status,
                "novelty": p.novelty,
                "related": p.related,
                "times_reinforced": p.times_reinforced or 0,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in rows
        ]
    }


@router.post("/principles/condense")
async def condense_principles_endpoint(
    dry_run: bool = False,
    user: User = Depends(require_admin),
):
    """Rule-set hygiene: let the curator consolidate near-duplicate ACTIVE
    principles (one call per category group). Sources become status='merged'
    (audit trail, never hard-deleted); the consolidated rule goes active with
    basis='condensed'. Conservative by design — 0 merges on a clean set is the
    expected outcome."""
    from src.services.curator_principles import condense_principles
    return await condense_principles(user.id, dry_run=dry_run)


@router.post("/principles/{principle_id}/{action}")
async def update_principle(
    principle_id: int,
    action: str,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Promote a shadow principle to 'active' (inject it into the judge from now
    on), send it back to 'shadow', or 'reject' it (never inject). The owner's one
    lever over the autonomous learning."""
    from src.database.models import CuratorPrinciple
    if action not in ("activate", "reject", "shadow"):
        raise HTTPException(status_code=400, detail="action must be activate/reject/shadow")
    row = (
        db.query(CuratorPrinciple)
        .filter(CuratorPrinciple.id == principle_id, CuratorPrinciple.user_id == user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Principle not found")
    if action == "activate":
        row.status, row.activated_at = "active", datetime.utcnow()
    elif action == "reject":
        row.status = "rejected"
    else:
        row.status, row.activated_at = "shadow", None
    db.commit()
    return {"status": "ok", "id": row.id, "new_status": row.status}


@router.delete("/principles/{principle_id}")
async def delete_principle(
    principle_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete a learned principle outright (vs 'reject', which keeps the row)."""
    from src.database.models import CuratorPrinciple
    row = (
        db.query(CuratorPrinciple)
        .filter(CuratorPrinciple.id == principle_id, CuratorPrinciple.user_id == user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Principle not found")
    db.delete(row)
    db.commit()
    return {"status": "deleted", "id": principle_id}


# ── Downscale candidates (KEEP_WITH_FLAG — kept, but bloated) ────────────────

def _namespace_for(media_type: str) -> Optional[str]:
    """TMDB namespace for a category, or None when the category is unknown.

    The movie/tv rule itself lives in size_norms — one definition, because a
    second copy drifting from it is exactly how a film and a series with the
    same TMDB number came to be reported as duplicates of each other.

    The None case belongs to this caller: a ProtectedMedia row with no
    category must NOT be guessed into the tv bucket, where it could collect
    an unrelated series' resolution and bitrate. No namespace, no id match —
    the title fallback still applies.
    """
    from src.services.size_norms import _tmdb_namespace
    return _tmdb_namespace(media_type) if media_type else None


@router.get("/downscale")
async def list_downscale_candidates(
    user: User = Depends(require_admin),   # curation = admin only
    db: Session = Depends(get_db),
):
    """The 'reclaim space' work list: every judge-kept title flagged as a bitrate
    outlier (KEEP_WITH_FLAG — incl. the Household Quality-Floor). Enriched
    best-effort with the tech profile so the admin sees what a transcode would
    touch. The actual downscaling happens outside Curatarr; 'done' moves the
    protection to HARD_KEEP so the row leaves this list but keeps its shield."""
    from src.database.models import ProtectedMedia, MediaTechProfile
    rows = (
        db.query(ProtectedMedia)
        # Verdict-gated, NOT source-gated: judge-granted flags and
        # discussion-reached keeps whose file is a bitrate outlier both
        # belong on this list (a discussion "I am flagging X for a
        # downscale" used to be pure prose - the row said verdict=NULL
        # and never surfaced here). Manual whitelist rows carry no
        # verdict and stay out.
        .filter(ProtectedMedia.user_id == user.id,
                ProtectedMedia.verdict == "KEEP_WITH_FLAG")
        .order_by(ProtectedMedia.created_at.desc())
        .all()
    )
    # Pre-fetch profiles to avoid N+1 queries. TMDB ids are only unique per
    # namespace ("movie" vs "tv"), so the map is keyed by both — see
    # _namespace_for above.
    tmdb_ids, titles = set(), set()
    for p in rows:
        tmdb = int(p.identifier) if (p.identifier or "").isdigit() else None
        if tmdb:
            tmdb_ids.add(tmdb)
        if p.title or p.identifier:
            titles.add(p.title or p.identifier)

    prof_by_id, prof_by_title = {}, {}
    if tmdb_ids or titles:
        from sqlalchemy import or_
        conds = []
        if tmdb_ids:
            conds.append(MediaTechProfile.tmdb_id.in_(list(tmdb_ids)))
        if titles:
            conds.append(MediaTechProfile.title.in_(list(titles)))

        for prof in db.query(MediaTechProfile).filter(or_(*conds)).all():
            ns = _namespace_for(prof.media_type)
            if prof.tmdb_id and ns:
                # Last write wins if two profiles share a key. That was
                # equally arbitrary with the old .first(), and namespacing
                # already removed the collisions that actually occurred.
                prof_by_id[(ns, prof.tmdb_id)] = prof
            if prof.title:
                prof_by_title[prof.title.lower()] = prof

    out, total_gb = [], 0.0
    for p in rows:
        tech = None
        try:
            tmdb = int(p.identifier) if (p.identifier or "").isdigit() else None
            ns = _namespace_for(p.category)

            prof = None
            if tmdb and ns:
                prof = prof_by_id.get((ns, tmdb))
            if not prof and (p.title or p.identifier):
                prof = prof_by_title.get((p.title or p.identifier).lower())

            if prof and prof.size_mb:
                total_gb += (prof.size_mb or 0) / 1024.0
                tech = {
                    "resolution": prof.resolution, "codec": prof.codec,
                    "size_gb": round((prof.size_mb or 0) / 1024.0, 1),
                    "mb_per_min": round(prof.mb_per_min, 0) if prof.mb_per_min else None,
                }
        except Exception:
            tech = None
        # the Bitrate line of the pillar reasoning is the one this list is about
        bitrate_note = next(
            (ln.strip() for ln in (p.reason or "").splitlines()
             if ln.strip().lower().startswith("bitrate")), "")
        out.append({
            "id": p.id, "title": p.title or p.identifier, "category": p.category,
            "bitrate_note": bitrate_note, "reason": p.reason,
            "tech": tech, "arr_url": p.arr_url,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        })
    return {"candidates": out, "total_gb": round(total_gb, 1)}


@router.post("/downscale/{protection_id}/done")
async def downscale_done(
    protection_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Mark a downscale as done: the file is no longer bloated, so the protection
    graduates to HARD_KEEP (leaves the downscale list, keeps the shield)."""
    from src.database.models import ProtectedMedia
    row = (
        db.query(ProtectedMedia)
        .filter(ProtectedMedia.id == protection_id,
                ProtectedMedia.user_id == user.id,
                ProtectedMedia.verdict == "KEEP_WITH_FLAG")
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Downscale candidate not found")
    row.verdict = "HARD_KEEP"
    db.commit()
    return {"status": "ok", "id": row.id, "title": row.title or row.identifier}


@router.get("/upgrade-candidates")
async def get_upgrade_candidates(_user: User = Depends(require_admin)):
    """Loved-but-lean titles: strong watch/feedback signal on a file below
    1080p or well under its class median. Read-only — acting on it stays a
    manual click in the arr (no auto-regrab)."""
    from src.services.upgrade_curation import upgrade_candidates
    rows = upgrade_candidates()
    # Series editions (2026-09-15): the TV cut on disk while AniDB knows an
    # uncensored version — the same list, a different kind of upgrade.
    try:
        from src.services.editions import upgrade_rows
        rows = rows + upgrade_rows()
    except Exception as e:
        logger.debug("[upgrades] editions unavailable: %s", e)
    return {"candidates": rows}


@router.get("/redundancy")
async def get_redundancy(_user: User = Depends(require_admin)):
    """Redundant-storage audit (intra-item versions + cross-item same-id
    copies). Entries carry provenance; intra entries are enriched LIVE from
    Plex with the actual per-version files (resolution, size, filename) so
    the panel can say WHICH file is the duplicate, and a Plex web deep-link
    base lets the UI jump to the item."""
    import asyncio as _aio
    import os as _os

    from src.services.size_norms import duplicate_report
    rep = duplicate_report()
    try:
        from src.services.plex_playlists import (_base, _headers, _owner_token,
                                                 get_machine_identifier)
        mid = await get_machine_identifier()
        if mid:
            rep["plex_web_base"] = (f"https://app.plex.tv/desktop/#!/server/{mid}"
                                    "/details?key=")
        tok = _owner_token()
        if tok:
            async def _attach_files(entry: dict):
                key = entry.get("plex_rating_key")
                if not key:
                    return
                try:
                    async with httpx.AsyncClient(timeout=10) as c:
                        r = await c.get(f"{_base()}/library/metadata/{key}",
                                        headers=_headers(tok))
                    meta = ((r.json().get("MediaContainer") or {}).get("Metadata")
                            or [{}])[0]
                    files = []
                    for m in meta.get("Media") or []:
                        part = (m.get("Part") or [{}])[0]
                        files.append({
                            "resolution": m.get("videoResolution"),
                            "size_gb": round((part.get("size") or 0) / 1024 ** 3, 1),
                            "file": _os.path.basename(part.get("file") or ""),
                        })
                    if files:
                        entry["files"] = files
                except Exception:
                    pass   # series keys carry no Media[] — panel degrades

            await _aio.gather(*[_attach_files(e)
                                for e in rep.get("intra_item") or []])
    except Exception as e:
        logger.debug("[redundancy] plex detail enrich failed: %s", e)

    # Second jump-off: the arr detail page. The cached candidate pool
    # already carries each item's ready-made arr_url — matched per
    # category so a same-named title in another library can't steal it.
    try:
        from src.services.library_memory import normalize_title
        url_map: dict = {}
        for cat in ("movie", "show", "anime"):
            for c in await _fetch_arr_candidates(cat):
                u, t = c.get("arr_url"), c.get("title") or ""
                if u and t:
                    url_map.setdefault((cat, normalize_title(t)), u)

        def _attach_arr(entry: dict):
            key = (entry.get("media_type"), normalize_title(entry.get("title") or ""))
            u = url_map.get(key)
            if u:
                entry["arr_url"] = u

        for e in rep.get("intra_item") or []:
            _attach_arr(e)
        for grp in rep.get("cross_item") or []:
            for c in grp.get("copies") or []:
                _attach_arr(c)
    except Exception as e:
        logger.debug("[redundancy] arr-url enrich failed: %s", e)
    return rep


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

    # ONE deletion run at a time. Live failure 2026-08-18: the owner's manual
    # Analyze and the custodian's ARR scan ran CONCURRENTLY — two full judge
    # runs interleaving call-by-call on the LLM gate (double wall clock), and
    # the later batch superseding the earlier one's freshly written proposals.
    from src.services.app_state import acquire_state_lock, release_state_lock
    if not acquire_state_lock("deletion_run"):
        return {"proposals": [], "total_gb": 0,
                "message": ("A deletion analysis is already running (scheduled "
                            "scan or another session). Its results will appear "
                            "here when it finishes — run Analyze again after "
                            "that if you still want a fresh pass.")}

    # Activity card for the manual Analyze run. The scheduler's ARR-sync path
    # already cards this work ("ARR Sync"), but the button-triggered run —
    # minutes of scoring + LLM pitches — was invisible the moment the user
    # left the Curation view. monitor_task lights up the Pass 99-fu5
    # phase/per-pitch progress messages inside generate_deletion_proposals.
    from src.services.task_monitor import task_monitor
    mtask = task_monitor.create(
        name=f"Deletion analysis: {category or 'all categories'}",
        category="curation", task_id=f"del-analysis-{user.id}")
    task_monitor.start(mtask)
    # try/finally: the enrich + DB-write block below can raise too (TMDB,
    # a constraint, a locked DB) — before, only a generation error released
    # the lock, and anything later left "deletion_run" held until restart,
    # blocking every Analyze and the scheduled scan.
    try:
        if not category:
            # Run sequentially — Ollama is single-threaded and can't handle
            # concurrent 27B curator calls without returning 500s.
            cats_present = list({i["category"] for i in arr_items if i.get("category")})
            proposals = []
            for cat in cats_present:
                cat_items = [i for i in arr_items if i.get("category") == cat]
                if cat_items:
                    proposals.extend(
                        await generate_deletion_proposals(
                            user.id, cat_items, cat, monitor_task=mtask)
                    )
        else:
            proposals = await generate_deletion_proposals(
                user.id, arr_items, category, monitor_task=mtask)

        # Enrich each proposal with poster, synopsis, genres from TMDB + ARR
        # metadata. arr-id-first keying — see build_proposal_item_map (the
        # Devil-Wears-Prada cross-service collision AND the same-service
        # same-title Good-Boy collision).
        item_map = build_proposal_item_map(arr_items)
        enriched = await asyncio.gather(*[
            _enrich_proposal(p, item_map, category) for p in proposals])

        if not enriched:
            # Generation produced nothing — keep whatever is in the DB rather than
            # wiping it, so the user still sees the last known proposals.
            task_monitor.done(mtask, "No candidates — previous proposals retained")
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
                    stagnant=p.get("stagnant", False),
                )
                dbs.add(row)
                saved.append((p, row))
            dbs.flush()
            proposals_with_ids = [
                {**p, "id": row.id, "size_gb": round((p.get("size_mb") or 0) / 1024, 1)}
                for p, row in saved
            ]
            dbs.commit()
    except Exception as e:
        task_monitor.error(mtask, str(e))
        raise
    finally:
        release_state_lock("deletion_run")
    task_monitor.done(mtask, f"{len(proposals_with_ids)} proposal(s) saved")

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
    # A closed proposal (already deleted, or mid-delete) must not learn a
    # "Keeping: …" note from a stale card: the protection path would log a
    # kept resolution for files that are gone.
    if comment and p.status in _OPEN_STATUSES:
        from src.services.episodic_memory import analyze_deletion_comment
        # pass the proposal's REAL category — the old default ("show") routed
        # every movie/music/anime comment into the show taste vector, which
        # filled its feedback caps (100/50) while the other vectors stayed at 0
        is_kept = await analyze_deletion_comment(
            user.id, p.title, comment, media_category=p.category or "show")

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


# Statuses a proposal can still be acted on from (approve / keep). Every
# status write on the delete path is a CONDITIONAL update against this set:
# the Lidarr freshness guard alone can hold an item for ~120 s, and a Keep
# click, a chat "keep X" or a second approve landing in that window used to
# be silently overwritten by the in-flight delete.
_OPEN_STATUSES = ("pending", "limbo")


def _transition_if_open(db: Session, p, new_status: str, **extra) -> bool:
    """Atomically move ``p`` from an open status to ``new_status``.

    A conditional UPDATE (not an ORM attribute write) so two sessions can
    never both win — rowcount says who did. Commits at once so the claim is
    visible to every other session before anything slow or destructive runs,
    then refreshes ``p`` so the caller sees the row's REAL status either way.
    Pending ORM edits (the Lidarr guard's storage_mb true-up, a comment) are
    flushed FIRST — flushed after, a stale in-memory status would overwrite
    the claim this just made.
    """
    db.flush()
    n = (db.query(DeletionProposal)
         .filter(DeletionProposal.id == p.id,
                 DeletionProposal.status.in_(_OPEN_STATUSES))
         .update({"status": new_status, **extra}, synchronize_session=False))
    db.commit()
    db.refresh(p)
    return n == 1


def _protection_for(db: Session, user_id: int, p):
    """The ProtectedMedia row that covers this proposal, or None.

    Identifiers are the exact title (discussion / manual keeps) or the TMDB
    id string (judge protections — see _persist_judge_protection). Title
    matches in any category: a false block costs a retry, a missed one costs
    the files. TMDB ids are per-type (movie 1399 != tv 1399), so those only
    count within the proposal's category."""
    from sqlalchemy import func, or_
    from src.database.models import ProtectedMedia
    conds = []
    if p.title:
        conds.append(func.lower(ProtectedMedia.identifier) == p.title.lower())
    if p.tmdb_id:
        conds.append((ProtectedMedia.identifier == str(p.tmdb_id))
                     & or_(ProtectedMedia.category == p.category,
                           ProtectedMedia.category.is_(None)))
    if not conds:
        return None
    return (db.query(ProtectedMedia)
            .filter(ProtectedMedia.user_id == user_id, or_(*conds))
            .first())


def _blocked_reason(db: Session, user_id: int, p) -> Optional[str]:
    """Fresh re-check right before a destructive step: None when the delete
    may proceed, else why not. A protected title's proposal is closed as
    rejected (same non-destructive outcome as the chat protection path)."""
    # commit before refresh: a bare refresh would discard pending edits
    # (the freshness guard's storage_mb true-up) along with the stale status
    db.commit()
    db.refresh(p)
    if p.status not in _OPEN_STATUSES:
        return f"proposal is {p.status}"
    prot = _protection_for(db, user_id, p)
    if prot is not None:
        _transition_if_open(db, p, "rejected", resolved_at=datetime.utcnow(),
                            user_comment="Auto-rejected by ProtectedMedia entry")
        return "title is protected"
    return None


@router.post("/deletions/{proposal_id}/approve")
async def approve_deletion(
    proposal_id: int,
    user: User = Depends(require_admin),   # deletions = admin curation only
    db: Session = Depends(get_db),
):
    p = db.query(DeletionProposal).filter(
        DeletionProposal.id == proposal_id,
        DeletionProposal.user_id == user.id,
    ).first()
    if not p:
        raise HTTPException(404, "Not found")
    if p.status not in _OPEN_STATUSES:
        # Double click / approve during a bulk run: the first request owns
        # the delete; a second DELETE would 404 at the arr and overwrite
        # "deleted" with "error".
        raise HTTPException(409, f"Proposal is already {p.status}")

    # Probe before committing to a destructive, irreversible action.
    reachable = await _probe_arr(p.service)
    if not reachable:
        # Keep the proposal alive in "limbo" — the user can retry at any time.
        _transition_if_open(db, p, "limbo")
        service_name = p.service.capitalize()
        return {
            "ok": False,
            "limbo": p.status == "limbo",
            "status": p.status,
            "error": f"{service_name} is currently unreachable. The item has NOT been deleted and can be retried.",
        }

    success = await _delete_one_and_log(db, user.id, p)
    db.commit()
    out = {"ok": success, "limbo": p.status == "limbo", "status": p.status}
    if not success and p.status == "rejected":
        out["error"] = f'"{p.title}" was kept / is protected — it was NOT deleted.'
    elif not success and p.status not in ("limbo", "error"):
        out["error"] = f'"{p.title}" is {p.status} — it was NOT deleted again.'
    return out


async def _delete_one_and_log(db: Session, user_id: int, p) -> bool:
    """Execute the arr delete for ONE proposal: status/resolved_at plus the
    CuratorResolutionLog row. Shared by the single-approve endpoint and the
    bulk runner so both paths stay behaviourally identical.

    Race safety: status + ProtectedMedia are re-read from the DB right
    before the (slow) Lidarr guard AND again right before the DELETE, and
    the row is then CLAIMED ("deleting") with a conditional UPDATE that is
    committed immediately — so a Keep / chat keep / second approve that
    lands mid-run wins instead of being overwritten, and two concurrent
    approvals can never both send a DELETE. The claim commit is the only
    commit in here; the final status + log row are left for the caller.
    Returns False with ``p.status`` telling why (limbo / rejected / error /
    whatever another path set) when nothing was deleted.

    Pass 66 / 81e resolution logging: originally hardcoded to
    ``resolution_type="consensus"`` with ``curator_stance = p.reason`` —
    fine when the proposal pitch WAS the curator's final word. But with
    Level-2 reevaluation (Pass 81) the curator's last word can be either a
    CONFIRM (still wants delete — true consensus) or a REVERSAL (now wants
    to keep — user deleting anyway is an OVERRIDE). The static ``p.reason``
    field can't represent that; the chat history can. 81e: pull the latest
    assistant message from the deletion_proposal thread as the canonical
    stance, parse the verdict polarity, classify accordingly. No chat
    history → falls back to ``p.reason`` + ``"consensus"`` (unchanged
    behaviour for the click-without-discussion path)."""
    # Catalog-mode freshness contract (owner decision 2026-08-15): SoulSync
    # OWNS the music files and may move/rename them; Lidarr is only the
    # passive index. Deleting against a stale index "succeeds" while the
    # file stays on disk — so refresh the ONE artist, wait, then require
    # actual track files. Drift parks the proposal in LIMBO (retryable
    # after a refresh), never in error.
    blocked = _blocked_reason(db, user_id, p)
    if blocked:
        logger.info("[deletion] %r not deleted: %s", p.title, blocked)
        return False
    if p.service == "lidarr":
        drift = await _lidarr_freshness_guard(p)
        if drift:
            logger.warning("[deletion] lidarr delete BLOCKED for %r: %s",
                           p.title, drift)
            _transition_if_open(db, p, "limbo")
            return False
        # The guard can take ~120 s — long enough for a Keep to land.
        blocked = _blocked_reason(db, user_id, p)
        if blocked:
            logger.info("[deletion] %r not deleted (changed during the "
                        "freshness check): %s", p.title, blocked)
            return False

    if not _transition_if_open(db, p, "deleting"):
        logger.info("[deletion] %r already claimed (%s) — not deleting twice",
                    p.title, p.status)
        return False
    try:
        result = await _execute_arr_delete(p)
    except Exception as e:
        # Never leave the claim hanging in "deleting" (invisible, unactionable).
        logger.error("[deletion] delete raised for %r: %s", p.title, e)
        result = False
    success = result is True
    if result is None:
        # Outcome unknown (arr accepted the connection then went silent and
        # the follow-up check could not confirm either way): limbo, which
        # the list shows with a Retry button — "error" is a dead end.
        p.status = "limbo"
    else:
        p.status = "deleted" if success else "error"
        p.resolved_at = datetime.utcnow()

    if success:
        try:
            stance, polarity = _latest_curator_stance_for_proposal(
                db, user_id, p.id, fallback_pitch=p.reason,
            )
            if polarity == "REVERSED":
                resolution_type = "override"
                override_reason = "Deleted despite Level-2 curator reversal"
            else:
                resolution_type = "consensus"
                override_reason = None
            db.add(CuratorResolutionLog(
                user_id=user_id,
                title=p.title,
                category=p.category,
                outcome="deleted",
                resolution_type=resolution_type,
                curator_stance=stance,
                override_reason=override_reason,
            ))
            logger.info(
                "📒 [RESOLUTION LOG] user=%d '%s' deleted (%s)%s",
                user_id, p.title, resolution_type,
                f" — {override_reason}" if override_reason else "",
            )
        except Exception as e:
            logger.debug("[deletion] resolution-log write failed: %s", e)
    return success


async def _lidarr_freshness_guard(p) -> Optional[str]:
    """Refresh ONE artist, wait for the command, then require real track
    files (per-file API — the aggregated sizeOnDisk is exactly the field
    that lies when stale). Returns None when the delete is safe, else the
    drift reason. Also trues up storage_mb from the actual file bytes so
    the "GB freed" stats are exact. Conservative on ANY doubt: a blocked
    delete costs a retry, a stale delete silently strands files."""
    if not (settings.LIDARR_URL and settings.LIDARR_API_KEY):
        return "lidarr not configured"
    base = str(settings.effective_lidarr_url).rstrip("/")
    headers = {"X-Api-Key": settings.LIDARR_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(f"{base}/api/v1/command", headers=headers,
                                  json={"name": "RefreshArtist",
                                        "artistId": int(p.media_id)})
            if r.status_code not in (200, 201):
                return f"RefreshArtist rejected (HTTP {r.status_code})"
            cmd_id = r.json().get("id")
            state = "queued"
            # Live-measured: a small artist's refresh (MB roundtrip + disk
            # scan) ran >40 s — a short window would block every delete.
            for _ in range(60):                      # ≤ ~120 s
                await asyncio.sleep(2)
                cr = await client.get(f"{base}/api/v1/command/{cmd_id}",
                                      headers=headers)
                state = (cr.json() or {}).get("status", "?")
                if state in ("completed", "failed"):
                    break
            if state != "completed":
                return f"refresh did not complete (status={state})"
            tf = await client.get(f"{base}/api/v1/trackfile", headers=headers,
                                  params={"artistId": int(p.media_id)})
            files = tf.json() if tf.status_code == 200 else []
            if not files:
                return ("no track files after refresh — SoulSync likely "
                        "moved/renamed the folder; fix the artist Path in "
                        "Lidarr, then retry")
            true_mb = sum(f.get("size") or 0 for f in files) / 1048576
            if true_mb > 0:
                p.storage_mb = true_mb
    except Exception as e:
        return f"freshness check failed: {e}"
    return None


# In-memory run guard for bulk delete — deliberately NOT a DB lock (same
# rationale as the OMDb-backfill guard in enrichment.py: a --reload mid-run
# would strand a DB lock forever; a module flag self-heals on reload).
_bulk_delete_running = False


class BulkApproveRequest(BaseModel):
    ids: list[int]
    comment: Optional[str] = None   # plain reason — server prefixes "Deleted: "


@router.post("/deletions/bulk-approve")
async def bulk_approve_deletions(
    req: BulkApproveRequest,
    user: User = Depends(require_admin),   # deletions = admin curation only
):
    """Delete several proposals in one background job. Runs as a
    task_monitor task (live in the Activity SSE stream); the arr of each
    distinct service is probed ONCE up front instead of per item."""
    global _bulk_delete_running
    if _bulk_delete_running:
        raise HTTPException(409, "A bulk delete is already running")
    ids = list({int(i) for i in (req.ids or [])})   # dedupe, keep ints
    if not ids:
        raise HTTPException(400, "No proposal ids given")
    with get_db_session() as db:
        n = db.query(DeletionProposal).filter(
            DeletionProposal.id.in_(ids),
            DeletionProposal.user_id == user.id,
            DeletionProposal.status.in_(["pending", "limbo"]),
        ).count()
    if not n:
        raise HTTPException(404, "No matching open proposals")

    from src.services.bg_tasks import track_task
    from src.services.task_monitor import task_monitor
    _bulk_delete_running = True
    task = task_monitor.create(name=f"Bulk delete ({n} items)",
                               category="curation", total=n)
    track_task(
        _run_bulk_delete_bg(task, user.id, ids, (req.comment or "").strip()),
        name="bulk-delete",
    )
    return {"status": "started", "task_id": task.id, "count": n}


async def _run_bulk_delete_bg(task, user_id: int, ids: list[int], comment: str) -> None:
    """Background bulk-delete runner (wiring mirrors _run_omdb_backfill_bg).

    Per item this replays exactly what the two single-item requests do —
    optional comment via the "Deleted: " prefix fast path (NO summarizer
    call), then _delete_one_and_log — committing after each item so a
    mid-run crash loses nothing. Honors task cancellation between items."""
    global _bulk_delete_running
    from src.services.task_monitor import task_monitor, TaskStatus
    from src.services.episodic_memory import analyze_deletion_comment
    task_monitor.start(task)
    ok = failed = limbo = skipped = 0
    drift_hits = 0          # lidarr freshness-guard blocks this run
    freed_mb = 0.0
    try:
        with get_db_session() as db:
            props = db.query(DeletionProposal).filter(
                DeletionProposal.id.in_(ids),
                DeletionProposal.user_id == user_id,
                DeletionProposal.status.in_(["pending", "limbo"]),
            ).order_by(DeletionProposal.id.asc()).all()
            task_monitor.update(task, total=len(props))

            # ONE reachability probe per distinct arr, not per item.
            reachable = {svc: await _probe_arr(svc)
                         for svc in {p.service for p in props}}

            for i, p in enumerate(props, 1):
                if task.status == TaskStatus.SKIPPED:   # user hit cancel
                    task_monitor.update(task, message="Cancelled — remaining items untouched",
                                        level="warn")
                    break
                title = p.title
                # Fresh re-read per item: the run can take minutes (Lidarr
                # guard ~120 s each), and a Keep / chat keep / single approve
                # that landed since the list was loaded must win.
                blocked = _blocked_reason(db, user_id, p)
                if blocked:
                    skipped += 1
                    task_monitor.update(task, processed=i,
                                        message=f"{title}: skipped — {blocked}",
                                        level="warn")
                    continue
                if not reachable.get(p.service):
                    _transition_if_open(db, p, "limbo")
                    limbo += 1
                    task_monitor.update(task, processed=i,
                                        message=f"{title}: {p.service} unreachable → limbo",
                                        level="warn")
                    continue
                # Mass-drift breaker: several file-less lidarr artists in
                # one run = a SoulSync reorganize just moved folders under
                # the index. Stop touching music, tell the owner to run a
                # full Lidarr refresh first.
                if p.service == "lidarr" and drift_hits >= 3:
                    _transition_if_open(db, p, "limbo")
                    limbo += 1
                    task_monitor.update(task, processed=i,
                                        message=f"{title}: skipped — mass drift "
                                                "(SoulSync reorganize?); full "
                                                "Lidarr refresh needed",
                                        level="warn")
                    continue
                try:
                    if comment:
                        p.user_comment = f"Deleted: {comment}"
                        db.commit()
                        try:
                            # Prefix fast path in analyze_deletion_comment —
                            # taste/memory learning without any LLM call.
                            await analyze_deletion_comment(
                                user_id, title, f"Deleted: {comment}",
                                media_category=p.category or "show")
                        except Exception as e:
                            logger.debug("[bulk-delete] comment learn failed for %r: %s",
                                         title, e)
                    success = await _delete_one_and_log(db, user_id, p)
                    db.commit()
                    if success:
                        ok += 1
                        freed_mb += float(p.storage_mb or 0)
                        task_monitor.update(task, processed=i, message=f"Deleted {title}")
                    elif p.status == "limbo":
                        # freshness guard parked it (stale index / drift), or
                        # the delete's outcome could not be confirmed
                        limbo += 1
                        if p.service == "lidarr":
                            drift_hits += 1
                        task_monitor.update(task, processed=i,
                                            message=f"{title}: parked in limbo "
                                                    "(refresh & retry)",
                                            level="warn")
                    elif p.status != "error":
                        # kept / protected / claimed by another approve while
                        # this run was busy — not a failure, nothing deleted
                        skipped += 1
                        task_monitor.update(task, processed=i,
                                            message=f"{title}: skipped — now {p.status}",
                                            level="warn")
                    else:
                        failed += 1
                        task_monitor.update(task, processed=i,
                                            message=f"{title}: arr delete failed",
                                            level="error")
                except Exception as e:
                    failed += 1
                    logger.error("[bulk-delete] %r failed: %s", title, e)
                    task_monitor.update(task, processed=i,
                                        message=f"{title}: {e}", level="error")
        task_monitor.done(
            task,
            f"{ok} deleted, {failed} failed, {limbo} limbo"
            + (f", {skipped} skipped" if skipped else "")
            + f" — {freed_mb / 1024:.1f} GB freed",
        )
    except Exception as e:
        logger.error("[bulk-delete] run failed: %s", e)
        task_monitor.done(task, f"Bulk delete failed: {e}")
    finally:
        _bulk_delete_running = False


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

    # Only an OPEN proposal can be kept. Keep on a stale card used to flip
    # "deleted" → "rejected": a false "kept" resolution in the log and the
    # freed GB gone from the stats, while the files were already deleted.
    # Conditional, so it also loses cleanly to an in-flight delete's claim.
    if not _transition_if_open(db, p, "rejected", resolved_at=datetime.utcnow()):
        if p.status == "rejected":
            return {"ok": True, "already_rejected": True}
        raise HTTPException(409, f"Proposal is already {p.status}")

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


def build_proposal_item_map(arr_items: list) -> dict:
    """Item lookup for _enrich_proposal with TWO key shapes per item:
    ("id", service, str(arr_id)) — unique, wins — plus the legacy
    (title, category) fallback for anything without an arr_id."""
    item_map: dict = {}
    for i in arr_items:
        item_map[(i["title"], i.get("category"))] = i
        if i.get("arr_id") is not None:
            item_map[("id", i.get("service"), str(i.get("arr_id")))] = i
    return item_map


async def _enrich_proposal(p: dict, item_map: dict, fallback_category: str = None) -> dict:
    """Attach poster / synopsis / genres to one proposal dict from TMDB + the
    ARR item.

    Lookup is arr-id-FIRST (build_proposal_item_map): the (title, category)
    key alone collided for same-title entries WITHIN one service — the owner
    holds BOTH 2025/26 "Good Boy" films in Radarr, the dict's last writer won,
    and the dog-horror's card shipped with the OTHER film's synopsis+genres
    (the pitch itself was right — the display lied). Same failure class as
    the Devil-Wears-Prada movie-vs-band collision, one level deeper.

    Shared by BOTH write paths: the manual Analyse endpoint AND the scheduler's
    nightly scan. The scheduler used to insert ``p.get("poster_url")`` while
    the engine dict never carried the field — every auto-generated proposal
    shipped without an image and never got one."""
    orig = (item_map.get(("id", p.get("service"), str(p.get("arr_id") or "")))
            or item_map.get((p["title"], p.get("category")), {}))
    cat = orig.get("category", p.get("category") or fallback_category or "movie")
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


async def _plex_music_candidates() -> list:
    """Music deletion candidates from the Plex music index (src/services/
    lyrics.py, rebuilt by the daily walk) when Lidarr is not configured
    (owner decision 2026-09-15: Lidarr is optional). The same shape the
    Lidarr branch produces, so the engine, the cards and the delete path need
    no special case — service 'plex', media_id = the Plex artist key, which
    is also the enrichment key of the artist."""
    try:
        from src.services.lyrics import plex_artists
        arts = plex_artists()
    except Exception as e:
        logger.warning("Plex music index unavailable: %s", e)
        return []
    if not arts:
        return []
    mid = None
    try:
        from src.services.plex_playlists import get_machine_identifier
        mid = await get_machine_identifier()
    except Exception:
        pass
    base = str(settings.effective_plex_url).rstrip("/")
    items = []
    for a in arts:
        key = str(a["artist_key"])
        link = (f"{base}/web/index.html#!/server/{mid}/details?key=%2Flibrary%2Fmetadata%2F{key}"
                if mid else f"{base}/web/index.html")
        items.append({
            "title": a.get("name") or "", "year": None, "genres": "",
            "size_mb": (a.get("size_bytes") or 0) / (1024 * 1024),
            "service": "plex", "arr_id": key, "plex_rating_key": key, "arr_url": link,
            "category": "music", "musicbrainz_id": a.get("mbid"), "monitored": True,
            "album_count": a.get("albums") or 0, "track_count": a.get("tracks") or 0,
        })
    logger.debug("Plex music: %d artist candidates", len(items))
    return items


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
                        # SOURCE-OF-TRUTH note (SoulSync gap analysis,
                        # 2026-08-15): Lidarr stays the STRUCTURAL source
                        # for music on purpose — sizes (sizeOnDisk gates
                        # deletion scoring), internal ids (the lidarr:{id}
                        # identity spine of embeddings + proposals),
                        # monitored state, import history and all write
                        # paths have no SoulSync equivalent. SoulSync is
                        # primary for METADATA only (music_metadata.
                        # enrich_artist merges it first).
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

    # ── PLEX MUSIC (Lidarr optional) ──────────────────
    if (not category or category == "music") and not (settings.LIDARR_URL and settings.LIDARR_API_KEY):
        candidates.extend(await _plex_music_candidates())

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
    if service == "plex":
        # Plex-music proposals (Lidarr not configured) — without this branch
        # every one of them went straight to limbo and _plex_delete_artist
        # was unreachable. /identity is Plex's cheapest endpoint.
        base, token = settings.effective_plex_url, settings.effective_plex_token
        if not base or not token:
            return False
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{str(base).rstrip('/')}/identity",
                                     headers={"X-Plex-Token": token,
                                              "Accept": "application/json"})
            return r.status_code == 200
        except Exception:
            return False
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


async def _plex_delete_artist(key: str, client=None) -> bool:
    """Delete a Plex artist WITH its files (Plex 'Allow media deletion' must
    be on — the owner's server has it), then re-read the item: a 200 means
    Plex kept it (deletion not allowed, or a scan raced) and that is a
    failure, never a success. On success the artist leaves the Plex music
    index at once so the next proposal run cannot re-propose it before the
    daily walk confirms."""
    base = str(settings.effective_plex_url).rstrip("/")
    token = settings.effective_plex_token
    if not base or not token:
        logger.error("[plex] delete: Plex not configured")
        return False
    headers = {"Accept": "application/json", "X-Plex-Token": token}
    owns = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        r = await client.delete(f"{base}/library/metadata/{key}", headers=headers)
        if r.status_code not in (200, 204):
            logger.error("[plex] delete HTTP %s for artist %s — is 'Allow media deletion' "
                         "enabled on the Plex server?", r.status_code, key)
            return False
        chk = await client.get(f"{base}/library/metadata/{key}", headers=headers)
        if chk.status_code == 200:
            logger.error("[plex] artist %s is still there after the delete — files kept", key)
            return False
        try:
            from src.services.lyrics import mark_artist_gone
            mark_artist_gone(key)
        except Exception as e:
            logger.debug("[plex] index update after delete failed: %s", e)
        return True
    except Exception as e:
        logger.error("[plex] delete failed for artist %s: %s", key, e)
        return False
    finally:
        if owns:
            await client.aclose()


# A delete with deleteFiles=true is synchronous on the arr side: it returns
# only once every file is gone, and on a NAS a big series takes minutes. The
# old flat 15 s read timeout marked those "error" while the arr finished the
# delete — and "error" is not approvable, so the row was stuck. Generous read
# timeout, short connect timeout (a down arr still fails fast).
_DELETE_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
# After a read timeout the arr may still be mid-delete: poll the item until
# it 404s (deleted) or give up (outcome unknown → limbo, retryable).
_DELETE_VERIFY_POLLS = 6
_DELETE_VERIFY_INTERVAL_S = 10.0


async def _verify_arr_item_gone(client, url: str, headers: dict) -> Optional[bool]:
    """True once the arr answers 404 for the item, None if that never
    happened (still present, or the arr stayed silent) — never False: a
    timed-out delete may still complete, so "still there" is not proof of
    failure either."""
    for attempt in range(_DELETE_VERIFY_POLLS):
        try:
            r = await client.get(url, headers=headers)
            if r.status_code == 404:
                return True
        except Exception as e:
            logger.debug("[arr] post-timeout verify GET failed: %s", e)
        if attempt < _DELETE_VERIFY_POLLS - 1:
            await asyncio.sleep(_DELETE_VERIFY_INTERVAL_S)
    return None


async def _execute_arr_delete(p: DeletionProposal) -> Optional[bool]:
    """True = deleted, False = the arr refused / failed, None = outcome
    unknown (connection lost or timed out and the follow-up check could not
    confirm) — the caller parks None in limbo instead of a dead-end error."""
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
    _ARRS = {
        "radarr": (settings.RADARR_URL, settings.RADARR_API_KEY,
                   settings.effective_radarr_url, "api/v3/movie",
                   {"deleteFiles": "true", "addImportExclusion": "true"}),
        "sonarr": (settings.SONARR_URL, settings.SONARR_API_KEY,
                   settings.effective_sonarr_url, "api/v3/series",
                   {"deleteFiles": "true", "addImportListExclusion": "true"}),
        "lidarr": (settings.LIDARR_URL, settings.LIDARR_API_KEY,
                   settings.effective_lidarr_url, "api/v1/artist",
                   {"deleteFiles": "true", "addImportListExclusion": "true"}),
    }
    try:
        if p.service in _ARRS:
            conf_url, api_key, base, path, params = _ARRS[p.service]
            if not (conf_url and api_key):
                return False
            url = f"{base}/{path}/{p.media_id}"
            headers = {"X-Api-Key": api_key}
            async with httpx.AsyncClient(timeout=_DELETE_TIMEOUT) as client:
                try:
                    r = await client.delete(url, headers=headers, params=params)
                except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                    # never reached the arr — nothing was deleted; retryable
                    logger.error("[%s] delete could not connect: %s", p.service, e)
                    return None
                except (httpx.TimeoutException, httpx.RemoteProtocolError,
                        httpx.ReadError) as e:
                    logger.warning(
                        "[%s] delete of %r got no answer (%s) — checking "
                        "whether the arr finished it anyway",
                        p.service, p.title, type(e).__name__)
                    gone = await _verify_arr_item_gone(client, url, headers)
                    if gone:
                        logger.info("[%s] %r is gone — delete completed "
                                    "after the timeout", p.service, p.title)
                    else:
                        logger.error("[%s] %r delete outcome unknown — parked "
                                     "for retry", p.service, p.title)
                    return gone
            return _check(r, p.service)
        if p.service == "plex":
            return await _plex_delete_artist(str(p.media_id))
    except Exception as e:
        logger.error("[arr] delete failed: %s", e)
    return False
