"""
Curatarr — Library Manager Router (Pass 16b foundation)

Endpoints powering the Settings → Library Defaults panel + Setup-Modal,
plus the foundation for the upcoming Library sidebar pages (16c–g).

Endpoint surface:
  GET  /api/library/status               — per-arr config + connection state
  POST /api/library/test                 — test URL+API-key (without saving)
  POST /api/library/configure            — save URL+API-key + reload settings
  GET  /api/library/profiles/{service}   — root folders + quality/metadata profiles
  GET  /api/library/defaults             — saved defaults per service
  PUT  /api/library/defaults/{service}   — set defaults for one service

Defaults live in AppState as ``lib_default:<service>:<key>`` rows so we
don't need a new schema migration.

Connection settings live in ``.env`` and are written via the existing
setup_wizard write_env helper (re-using the file format the wizard owns).
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.config import settings
from src.database.models import User
from src.routers.auth import get_current_user, require_admin
from src.services.app_state import get_state, set_state

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────

class ArrTestRequest(BaseModel):
    service: str = Field(..., pattern="^(sonarr|radarr|lidarr)$")
    # Pass 16b.1: both optional. Empty fields fall back to saved values
    # so the user doesn't have to re-paste the API key just to re-test.
    url: Optional[str] = None
    api_key: Optional[str] = None


class ArrConfigureRequest(BaseModel):
    service: str = Field(..., pattern="^(sonarr|radarr|lidarr)$")
    url: str
    api_key: str


class ArrDefaultsRequest(BaseModel):
    root_folder_path: Optional[str] = None
    quality_profile_id: Optional[int] = None
    language_profile_id: Optional[int] = None     # Sonarr only
    metadata_profile_id: Optional[int] = None     # Lidarr only
    monitor_option: Optional[str] = None           # mostly Lidarr-relevant
    series_type: Optional[str] = None              # Sonarr only — "standard|anime|daily"


# ── Helpers ───────────────────────────────────────────────────────────────────

_DEFAULT_KEYS = (
    "root_folder_path",
    "quality_profile_id",
    "language_profile_id",
    "metadata_profile_id",
    "monitor_option",
    "series_type",
)


def _read_defaults(service: str) -> dict:
    """Read all saved defaults for one service from AppState."""
    out = {}
    for key in _DEFAULT_KEYS:
        val = get_state(f"lib_default:{service}:{key}")
        if val is not None:
            # Numeric fields stored as strings in app_state — coerce back
            if "_id" in key:
                try:
                    out[key] = int(val)
                except (TypeError, ValueError):
                    out[key] = None
            else:
                out[key] = val
    return out


def _get_arr_url_key(service: str) -> tuple[Optional[str], Optional[str]]:
    """Read URL + API-key for a service from runtime settings."""
    if service == "sonarr":
        return settings.SONARR_URL, settings.SONARR_API_KEY
    if service == "radarr":
        return settings.RADARR_URL, settings.RADARR_API_KEY
    if service == "lidarr":
        return settings.LIDARR_URL, settings.LIDARR_API_KEY
    return None, None


def _make_client(service: str, url: str, api_key: str):
    """Instantiate the right client class for a service."""
    from src.services.arr_client import SonarrClient, RadarrClient, LidarrClient
    if service == "sonarr":
        return SonarrClient(url, api_key)
    if service == "radarr":
        return RadarrClient(url, api_key)
    if service == "lidarr":
        return LidarrClient(url, api_key)
    raise ValueError(f"unknown service: {service}")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status")
async def library_status(_user: User = Depends(get_current_user)):
    """
    Return per-arr config + connection state for the Library settings UI.

    For each service: configured (has URL+key in env), and a quick
    last-known connection status (from cache; we DON'T spam test calls
    on every page load — use /test for fresh checks).
    """
    out = {}
    for svc in ("sonarr", "radarr", "lidarr"):
        url, key = _get_arr_url_key(svc)
        configured = bool(url and key)
        last_test = get_state(f"lib_test:{svc}:last")
        out[svc] = {
            "configured": configured,
            "url": url or "",
            "has_key": bool(key),
            "last_test": last_test,
            "defaults": _read_defaults(svc),
        }
    return out


@router.post("/test")
async def library_test(
    req: ArrTestRequest,
    _user: User = Depends(require_admin),
):
    """
    Test a URL + API-key combination without saving. Used by the Setup-Modal
    and the Settings panel before committing.

    Pass 16b.1: empty url / api_key fall back to the saved values. So
    "Test connection" with both fields blank tests the existing config
    without forcing the admin to re-enter the API key from memory.
    """
    saved_url, saved_key = _get_arr_url_key(req.service)
    effective_url = req.url or saved_url or ""
    effective_key = req.api_key or saved_key or ""
    if not effective_url or not effective_key:
        return {
            "ok": False,
            "service": req.service,
            "error": "URL or API key missing — fill in fields or save config first",
        }
    # Pass 97: warn if the endpoint isn't loopback / RFC1918 / .local.
    # We don't block — the user may legitimately point at a remote ARR —
    # but the UI shows a yellow banner so a typo / accidental public URL
    # doesn't silently leak the API key over the open internet.
    from src.services.setup_wizard import endpoint_privacy_note
    privacy_warning = endpoint_privacy_note(effective_url)

    client = _make_client(req.service, effective_url, effective_key)
    try:
        async with client:
            try:
                status = await client.test_connection()
                version = (status or {}).get("version", "")
                set_state(f"lib_test:{req.service}:last",
                          f"ok|{version}|{__import__('datetime').datetime.utcnow().isoformat()}")
                return {
                    "ok": True,
                    "service": req.service,
                    "version": version,
                    "instance_name": (status or {}).get("instanceName"),
                    "privacy_warning": privacy_warning,
                }
            except Exception as exc:
                set_state(f"lib_test:{req.service}:last",
                          f"fail|{exc}|{__import__('datetime').datetime.utcnow().isoformat()}")
                return {
                    "ok": False,
                    "service": req.service,
                    "error": str(exc),
                    "privacy_warning": privacy_warning,
                }
    except Exception as exc:
        return {
            "ok": False,
            "service": req.service,
            "error": str(exc),
            "privacy_warning": privacy_warning,
        }


@router.post("/configure")
async def library_configure(
    req: ArrConfigureRequest,
    _user: User = Depends(require_admin),
):
    """
    Save URL + API-key for a service to .env. Reuses setup_wizard's
    write_env so the file format stays consistent. Reload settings after.
    """
    # Test first so we don't save broken creds
    test_result = await library_test(
        ArrTestRequest(service=req.service, url=req.url, api_key=req.api_key),
        _user=_user,
    )
    if not test_result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=f"Connection test failed: {test_result.get('error', 'unknown')}",
        )

    # Build a minimal config dict from current settings + the override
    from src.services.setup_wizard import write_env
    cfg = {
        "plex_url":            settings.PLEX_URL or "",
        "plex_token":          settings.PLEX_TOKEN or "",
        "ollama_endpoint":     settings.effective_ollama,
        "base_curator_model":  settings.BASE_CURATOR_MODEL or "qwen2.5:32b",
        "base_summarizer_model": settings.BASE_SUMMARIZER_MODEL or "dolphin3",
        "embedding_model":     settings.EMBEDDING_MODEL or "nomic-embed-text",
        "radarr_url":          settings.RADARR_URL or "",
        "radarr_api_key":      settings.RADARR_API_KEY or "",
        "sonarr_url":          settings.SONARR_URL or "",
        "sonarr_api_key":      settings.SONARR_API_KEY or "",
        "lidarr_url":          settings.LIDARR_URL or "",
        "lidarr_api_key":      settings.LIDARR_API_KEY or "",
        "tmdb_api_key":        settings.TMDB_API_KEY or "",
        "omdb_api_key":        settings.OMDB_API_KEY or "",
        "lastfm_api_key":      settings.LASTFM_API_KEY or "",
        "spotify_client_id":   settings.SPOTIFY_CLIENT_ID or "",
        "spotify_client_secret": settings.SPOTIFY_CLIENT_SECRET or "",
        "jwt_secret":          settings.JWT_SECRET,
    }
    # Override the changed service
    cfg[f"{req.service}_url"]     = req.url
    cfg[f"{req.service}_api_key"] = req.api_key

    write_env(cfg)
    # Live-reload the in-process settings so the next request sees the
    # new values without a full app restart.
    settings.__init__()
    logger.info("[library] %s reconfigured by admin (URL changed)", req.service)
    return {"ok": True, "service": req.service, "version": test_result.get("version")}


@router.get("/profiles/{service}")
async def library_profiles(
    service: str,
    _user: User = Depends(get_current_user),
):
    """
    Return root folders + quality profiles + (Lidarr) metadata profiles +
    (Sonarr) language profiles + tags for the Settings dropdowns.
    """
    if service not in ("sonarr", "radarr", "lidarr"):
        raise HTTPException(400, "unknown service")
    url, api_key = _get_arr_url_key(service)
    if not url or not api_key:
        raise HTTPException(400, f"{service} not configured")

    client = _make_client(service, url, api_key)
    out: dict = {"service": service}
    try:
        async with client:
            try:
                out["root_folders"]       = await client.list_root_folders()
            except Exception as e:
                out["root_folders"] = []
                out["root_folders_error"] = str(e)
            try:
                out["quality_profiles"]   = await client.list_quality_profiles()
            except Exception as e:
                out["quality_profiles"] = []
                out["quality_profiles_error"] = str(e)
            # Pass 16b.1: language_profiles removed — Sonarr v4 dropped them.
            # The arr_client method still exists (defensive) but the UI
            # doesn't need them anymore.
            if service == "lidarr":
                try:
                    out["metadata_profiles"] = await client.list_metadata_profiles()
                except Exception as e:
                    out["metadata_profiles"] = []
                    out["metadata_profiles_error"] = str(e)
            try:
                out["tags"] = await client.list_tags()
            except Exception as e:
                out["tags"] = []
                out["tags_error"] = str(e)
    except Exception as e:
        raise HTTPException(502, f"{service} unreachable: {e}")
    return out


@router.get("/defaults")
async def library_defaults_all(_user: User = Depends(get_current_user)):
    """All saved defaults for all configured arrs."""
    return {svc: _read_defaults(svc) for svc in ("sonarr", "radarr", "lidarr")}


@router.put("/defaults/{service}")
async def library_set_defaults(
    service: str,
    req: ArrDefaultsRequest,
    _user: User = Depends(require_admin),
):
    """Persist defaults for one service. Only writes provided keys
    (caller can patch a single field without resetting the rest)."""
    if service not in ("sonarr", "radarr", "lidarr"):
        raise HTTPException(400, "unknown service")
    saved = {}
    for key in _DEFAULT_KEYS:
        val = getattr(req, key, None)
        if val is None:
            continue
        set_state(f"lib_default:{service}:{key}", str(val))
        saved[key] = val
    logger.info("[library] %s defaults updated: %s", service, list(saved.keys()))
    return {"ok": True, "service": service, "saved": saved}


# ── Pass 16d: Synopsis Browser (items list + enrichment status + re-enrich) ──

def _flatten_arr_item(svc: str, item: dict, enrich: dict | None) -> dict:
    """Normalise an arr-side record into a single item shape the Synopsis
    Browser frontend expects.

    Carries the arr's ID + the title/year/size/added fields, plus our
    enrichment status (synopsis snippet + last-update timestamp + a
    has_enrichment flag) so the UI can sort/filter on either side.
    """
    if svc == "sonarr":
        out = {
            "id":              item.get("id"),
            "title":           item.get("title"),
            "year":            item.get("year"),
            "tmdb_id":         item.get("tmdbId"),
            "tvdb_id":         item.get("tvdbId"),
            "imdb_id":         item.get("imdbId"),
            "size_on_disk":    (item.get("statistics") or {}).get("sizeOnDisk", 0),
            "added":           item.get("added"),
            "status":          item.get("status"),
            "monitored":       item.get("monitored"),
            "root_folder":     item.get("rootFolderPath"),
            "tags":            item.get("tags") or [],
            "series_type":     item.get("seriesType"),
        }
    elif svc == "radarr":
        out = {
            "id":              item.get("id"),
            "title":           item.get("title"),
            "year":            item.get("year"),
            "tmdb_id":         item.get("tmdbId"),
            "imdb_id":         item.get("imdbId"),
            "size_on_disk":    item.get("sizeOnDisk", 0),
            "added":           item.get("added"),
            "status":          item.get("status"),
            "monitored":       item.get("monitored"),
            "root_folder":     item.get("rootFolderPath") or item.get("path"),
            "tags":            item.get("tags") or [],
            "has_file":        item.get("hasFile", False),
        }
    else:  # lidarr
        out = {
            "id":              item.get("id"),
            "title":           item.get("artistName"),
            "year":            None,
            "mbid":            item.get("foreignArtistId"),
            "size_on_disk":    (item.get("statistics") or {}).get("sizeOnDisk", 0),
            "added":           item.get("added"),
            "status":          item.get("status"),
            "monitored":       item.get("monitored"),
            "root_folder":     item.get("rootFolderPath") or item.get("path"),
            "tags":            item.get("tags") or [],
            "album_count":     (item.get("statistics") or {}).get("albumCount", 0),
        }

    # Enrichment status from our cache
    if enrich:
        synopsis = (enrich.get("plot_summary")
                    or enrich.get("overview")
                    or enrich.get("bio")
                    or "")
        out["synopsis"] = synopsis[:300] + ("…" if len(synopsis) > 300 else "")
        out["synopsis_full_length"] = len(synopsis)
        out["enrichment_source"] = enrich.get("source", "")
        out["enrichment_updated"] = enrich.get("_cached_at") or enrich.get("created_at")
        out["has_enrichment"] = bool(synopsis)
    else:
        out["synopsis"] = ""
        out["synopsis_full_length"] = 0
        out["enrichment_source"] = ""
        out["enrichment_updated"] = None
        out["has_enrichment"] = False

    return out


def _enrichment_lookup(svc: str, item: dict, cache) -> dict | None:
    """Best-effort lookup of an enriched profile from MetadataCache.

    Cache keys written by ``enrich_media_item`` are
    ``enriched:{media_type}:{id_key}`` where ``id_key`` falls back through
    anilist_id → anidb_id → tmdb_id → tvdb_id → title[:40].

    Pass 16h fixes:
      - Sonarr's anime tab was missing because cache keys for anime use
        ``enriched:anime:`` (set by chat cascade calling with
        ``media_type="anime"``) — we now check both show + anime keys,
        prioritising by the arr's seriesType.
      - Lidarr was lowercasing the artist name before lookup but the
        enricher stores title[:40] verbatim — case-mismatch caused a
        100% cache miss in the music browser. Fix: drop the .lower().
    """
    try:
        # ALL the key shapes the writers have used over the app's history —
        # the browser previously checked only tmdb/tvdb and missed everything
        # keyed by arr doc-id ("sonarr:3176", the ARR enrichment run), by
        # plex rating-key / title[:40] (the watch-history path), which is why
        # The Simpsons / Stranger Things showed "no synopsis" at 100% enriched.
        if svc == "sonarr":
            mts = ("anime", "show") if (item.get("seriesType") == "anime") else ("show", "anime")
            id_keys = [item.get("tmdbId"), item.get("tvdbId"),
                       f"sonarr:{item.get('id')}" if item.get("id") else None,
                       (item.get("title") or "")[:40] or None]
        elif svc == "radarr":
            mts = ("movie",)
            id_keys = [item.get("tmdbId"),
                       f"radarr:{item.get('id')}" if item.get("id") else None,
                       (item.get("title") or "")[:40] or None]
        else:  # lidarr
            mts = ("music",)
            name = item.get("artistName") or ""
            id_keys = [name[:40] or None,
                       f"lidarr:{item.get('id')}" if item.get("id") else None,
                       item.get("foreignArtistId")]
        id_keys = [k for k in id_keys if k]

        for mt in mts:
            for ext in id_keys:
                row = cache.get_cache(f"enriched:{mt}:{ext}")
                if row:
                    return row.get("response")
        # No live enriched profile — fall back to the raw API cache so the
        # browser can at least show the real synopsis instead of "no synopsis
        # on file" while the item waits for its (re-)polish.
        for mt in mts:
            for ext in id_keys:
                row = cache.get_cache(f"raw:{mt}:{ext}")
                resp = (row or {}).get("response")
                if isinstance(resp, dict) and (resp.get("overview")
                                               or resp.get("overview_extended")
                                               or resp.get("bio")):
                    return {
                        "plot_summary": resp.get("overview_extended") or resp.get("overview"),
                        "bio": resp.get("bio"),
                        "source": "raw metadata (awaiting polish)",
                        "_cached_at": row.get("created_at"),
                    }
    except Exception:
        pass
    return None


# ── Pass 16i/16k: Arr library cache (in-memory + DB-persisted) ────────────
#
# Library Manager doesn't need second-by-second freshness — series/movie/
# artist lists change minutes-to-hours apart. We cache the raw arr response
# for ``_LIB_CACHE_TTL`` seconds and serve from cache. On a transient
# connection failure we fall back to the stale cache so a briefly-down
# arr doesn't break the UI; the user sees a banner explaining the data
# is stale. The "↻ Refresh" button passes ``?refresh=true`` to bypass.
#
# Pass 16k: cache is now two-tier:
#   L1 = in-process dict (fastest; lost on restart)
#   L2 = MetadataCache rows under key ``arr_library:<svc>`` (persistent;
#        loaded into L1 by prewarm_arr_caches() at app startup)
# Live fetches populate both layers. Wall-clock timestamps are used so
# the freshness check survives process restarts (monotonic resets at
# start-of-process and would mark every reloaded entry as 0s old).
import time

_LIB_CACHE: dict[str, dict] = {}
_LIB_CACHE_TTL = 900       # 15 min freshness window (after which we re-fetch)
_LIB_DB_TTL_DAYS = 30      # how long to keep the persisted blob — long enough
                           # that a several-day outage of the arr still has
                           # something to serve

# Cache key in MetadataCache (gets auto-prefixed with _CACHE_VERSION)
def _lib_cache_key(svc: str) -> str:
    return f"arr_library:{svc}"


def _format_arr_error(svc: str, exc: Exception) -> str:
    """Build a useful error message even when the exception stringifies empty.

    aiohttp's ClientConnectorError sometimes has a non-empty repr() but an
    empty str(), which used to surface as ``"lidarr unreachable: "`` with
    nothing after the colon. Falling back through repr → class name keeps
    the user informed.
    """
    msg = str(exc) or repr(exc) or exc.__class__.__name__
    return f"{svc} unreachable: {msg}"


def _persist_lib_cache(svc: str, items_raw: list, tags: list, at: float) -> None:
    """Write the in-memory L1 cache entry to the L2 (DB) cache.

    Best-effort — failures are logged but don't break the live-fetch path.
    Stored as a single MetadataCache row so we get the existing schema
    versioning + cleanup-expired support for free.
    """
    try:
        from src.cache.metadata_cache import MetadataCache
        mc = MetadataCache()
        try:
            mc.set_cache(
                _lib_cache_key(svc),
                {"items_raw": items_raw, "tags": tags, "at": at},
                days=_LIB_DB_TTL_DAYS,
            )
        finally:
            mc.close()
    except Exception as e:
        logger.warning("[library] persist cache failed for %s: %s", svc, e)


def _load_lib_cache_from_db() -> int:
    """Populate ``_LIB_CACHE`` (L1) from the DB on startup.

    Returns the number of services successfully restored. Errors are
    logged but suppressed — pre-warm is best-effort and the on-demand
    fetch path always works as a fallback.
    """
    restored = 0
    try:
        from src.cache.metadata_cache import MetadataCache
        mc = MetadataCache()
        try:
            for svc in ("sonarr", "radarr", "lidarr"):
                row = mc.get_cache(_lib_cache_key(svc))
                if not row:
                    continue
                payload = row.get("response") or {}
                items_raw = payload.get("items_raw")
                tags      = payload.get("tags", [])
                at        = payload.get("at")
                if items_raw is None or at is None:
                    continue
                _LIB_CACHE[svc] = {"items_raw": items_raw, "tags": tags, "at": at}
                restored += 1
                age = max(0, time.time() - at)
                logger.info(
                    "[library] L2→L1 restored %s cache: %d items, %d tags, %.0fs old",
                    svc, len(items_raw), len(tags), age,
                )
        finally:
            mc.close()
    except Exception as e:
        logger.warning("[library] L2 cache load failed: %s", e)
    return restored


async def prewarm_arr_caches() -> None:
    """Startup hook (called from main.py lifespan).

    Step 1: load any persisted caches from L2 → L1 so the first user
    request after a restart serves instantly.

    Step 2: fire a background refresh for each configured arr so the
    cached data is freshened in the background. If the arr is down,
    the refresh quietly fails and we keep serving the stale L1 entry.

    Both steps are best-effort and never raise — the normal on-demand
    fetch path remains available regardless.
    """
    import asyncio
    restored = _load_lib_cache_from_db()
    logger.info("[library] prewarm: restored %d arr cache(s) from DB", restored)

    async def _bg_refresh(svc: str):
        # Wait a few seconds so we don't compete with anime mapping load,
        # plex sync, etc. fighting for startup CPU/network.
        await asyncio.sleep(8)
        try:
            await _fetch_arr_library(svc, force_refresh=True)
            logger.info("[library] prewarm refresh OK: %s", svc)
        except Exception as e:
            logger.info(
                "[library] prewarm refresh failed for %s (%s) — "
                "L1 cache remains; user will see stale or live-fetch on demand",
                svc, e,
            )

    # Pass 41: use track_task so the prewarm refreshes can't be GC'd
    # mid-fetch (would silently drop the warmer for an arr).
    from src.services.bg_tasks import track_task
    for svc in ("sonarr", "radarr", "lidarr"):
        url, api_key = _get_arr_url_key(svc)
        if not url or not api_key:
            continue
        track_task(_bg_refresh(svc), name=f"arr_prewarm_refresh:{svc}")


async def _fetch_arr_library(service: str, *, force_refresh: bool = False) -> tuple[list, list, dict]:
    """Fetch (items_raw, tags, cache_status) for a service, with TTL cache
    and stale fallback on failure.

    Returns ``(items_raw, tags, cache_info)`` where cache_info is a dict
    suitable for surfacing in the response so the UI can render a "from
    cache" / "stale" badge:

      ``{"source": "live"|"cache"|"stale", "age_s": int, "stale_error": str?}``

    Raises HTTPException(502) only when there's NO cached data to fall
    back on — first-time failure with a broken arr.
    """
    now = time.time()  # wall clock so cache age survives restarts (Pass 16k)
    hit = _LIB_CACHE.get(service)
    age = (now - hit["at"]) if hit else float("inf")

    # Serve fresh cache directly
    if hit and not force_refresh and age < _LIB_CACHE_TTL:
        return hit["items_raw"], hit["tags"], {
            "source": "cache", "age_s": int(age), "ttl_s": _LIB_CACHE_TTL,
        }

    # Cache miss / expired / forced — try live fetch
    url, api_key = _get_arr_url_key(service)
    if not url or not api_key:
        raise HTTPException(400, f"{service} not configured")
    client = _make_client(service, url, api_key)
    try:
        async with client:
            if service == "sonarr":
                items_raw = await client.get_series()
            elif service == "radarr":
                items_raw = await client.get_movies()
            else:
                items_raw = await client.get_artists()
            try:
                tags = await client.list_tags()
            except Exception as e:
                logger.debug("[library] %s tags fetch failed: %s", service, e)
                tags = (hit or {}).get("tags", [])
        # L1 (in-memory) + L2 (persisted DB row) populated together so the
        # next process restart starts warm.
        _LIB_CACHE[service] = {"items_raw": items_raw, "tags": tags, "at": now}
        _persist_lib_cache(service, items_raw, tags, now)
        return items_raw, tags, {"source": "live", "age_s": 0, "ttl_s": _LIB_CACHE_TTL}
    except Exception as e:
        if hit:
            logger.warning("[library] %s live fetch failed (%s) — serving stale cache (%.0fs old)",
                           service, e, age)
            return hit["items_raw"], hit["tags"], {
                "source": "stale", "age_s": int(age), "ttl_s": _LIB_CACHE_TTL,
                "stale_error": str(e) or repr(e) or e.__class__.__name__,
            }
        raise HTTPException(502, _format_arr_error(service, e))


@router.get("/items/{service}")
async def library_items(
    service: str,
    sort: str = "size_desc",
    needs_enrichment: bool = False,
    root_filter: str | None = None,   # substring match on root path (for TV/Anime split)
    curatarr_only: bool = False,       # filter to items with the 'curatarr' tag
    search: str | None = None,         # case-insensitive title substring
    limit: int = 200,
    refresh: bool = False,             # Pass 16i: bypass cache (force live fetch)
    _user: User = Depends(get_current_user),
):
    """List arr-side items + enrichment-status for the Synopsis Browser.

    Sorting options:
      size_desc           — biggest items first (default)
      size_asc
      added_desc          — most recently added first
      added_asc
      synopsis_updated_asc — items whose synopsis is OLDEST first (default
                             for "needs work" workflows)
      title_asc

    Pass 16i: cached for 15 min in-process. Stale cache served on transient
    connection failures. Pass ``refresh=true`` to bypass the cache.
    """
    if service not in ("sonarr", "radarr", "lidarr"):
        raise HTTPException(400, "unknown service")

    items_raw, tags, cache_info = await _fetch_arr_library(service, force_refresh=refresh)

    curatarr_tag_id = None
    if curatarr_only:
        for t in tags or []:
            if (t.get("label") or "").lower() == "curatarr":
                curatarr_tag_id = t.get("id")
                break

    from src.cache.metadata_cache import MetadataCache
    cache = MetadataCache()
    try:
        items = [
            _flatten_arr_item(service, raw, _enrichment_lookup(service, raw, cache))
            for raw in items_raw
        ]
    finally:
        cache.close()

    # Apply filters
    if needs_enrichment:
        items = [i for i in items if not i["has_enrichment"]]
    if search and search.strip():
        s = search.strip().lower()
        items = [i for i in items if s in (i.get("title") or "").lower()]
    if root_filter:
        rf = root_filter.lower()
        items = [i for i in items if (i.get("root_folder") or "").lower().find(rf) >= 0]
    if curatarr_only and curatarr_tag_id is not None:
        items = [i for i in items if curatarr_tag_id in (i.get("tags") or [])]
    elif curatarr_only:
        items = []  # tag doesn't even exist yet → empty list

    # Sort
    sort_map = {
        "size_desc":            (lambda x: -(x.get("size_on_disk") or 0)),
        "size_asc":             (lambda x:  (x.get("size_on_disk") or 0)),
        "added_desc":           (lambda x:  (x.get("added") or "")),  # ISO strings sort lexicographically
        "added_asc":            (lambda x:  (x.get("added") or "")),
        "synopsis_updated_asc": (lambda x:  (x.get("enrichment_updated") or "0000")),
        "title_asc":            (lambda x:  (x.get("title") or "").lower()),
    }
    key_fn = sort_map.get(sort, sort_map["size_desc"])
    if sort in ("size_desc", "added_desc"):
        items.sort(key=key_fn)
        if sort == "added_desc":
            items.reverse()
    else:
        items.sort(key=key_fn)

    return {
        "service": service,
        "total":   len(items),
        "items":   items[:limit],
        "limit":   limit,
        "filters": {
            "sort":              sort,
            "needs_enrichment":  needs_enrichment,
            "root_filter":       root_filter,
            "curatarr_only":     curatarr_only,
        },
        "cache":   cache_info,   # Pass 16i: {source, age_s, ttl_s, [stale_error]}
    }


class ReEnrichRequest(BaseModel):
    service: str = Field(..., pattern="^(sonarr|radarr|lidarr)$")
    arr_id: int                                      # the arr's internal id
    mode: str = Field(..., pattern="^(metadata|summary|both)$")


@router.post("/reenrich")
async def library_reenrich(
    req: ReEnrichRequest,
    _user: User = Depends(require_admin),    # Pass 16l: write/queue ops admin-only
):
    """Trigger a fresh enrichment for one arr-side item.

    mode = "metadata" : re-fetch from TMDB / AniList / OMDB / MusicBrainz
                        (drops cached profile, runs full pipeline)
    mode = "summary"  : re-run summarize_with_small_llm on existing raw
                        data — useful when the LLM's output was bad but
                        the upstream metadata is still fine.
    mode = "both"     : metadata refresh AND summary refresh.
    """
    url, api_key = _get_arr_url_key(req.service)
    if not url or not api_key:
        raise HTTPException(400, f"{req.service} not configured")

    # Resolve arr-side IDs we need for the enrichment lookup
    client = _make_client(req.service, url, api_key)
    try:
        async with client:
            re_year = re_tvdb = re_mbid = None
            if req.service == "sonarr":
                series = await client.get_series_details(req.arr_id)
                tmdb_id = series.get("tmdbId")
                title = series.get("title", "")
                # Was hardcoded "show" — anime then resolved via the wrong
                # (TMDB) path. Use the shared classifier so anime re-enriches
                # through AniList like the bulk run does.
                from src.services.arr_client import classify_sonarr_category
                media_type = classify_sonarr_category(series)
                re_year = series.get("year")
                re_tvdb = series.get("tvdbId")
                ext_id = tmdb_id or re_tvdb     # anime often has no tmdbId
            elif req.service == "radarr":
                movie = await client.get_movie(req.arr_id)
                tmdb_id = movie.get("tmdbId")
                title = movie.get("title", "")
                media_type = "movie"
                ext_id = tmdb_id
                re_year = movie.get("year")
            else:
                artist = await client.get_artist_details(req.arr_id)
                title = artist.get("artistName", "")
                ext_id = artist.get("foreignArtistId")   # the MusicBrainz id
                media_type = "music"
                tmdb_id = None
                re_mbid = ext_id
    except Exception as e:
        raise HTTPException(502, f"{req.service} unreachable: {e}")

    if not ext_id:
        raise HTTPException(400, "Item has no external ID — cannot re-enrich")

    # mode=metadata or both → bust the cached profile so the next
    # enrich_media_item call re-fetches
    from src.cache.metadata_cache import MetadataCache, _CACHE_VERSION
    cache = MetadataCache()
    try:
        if req.mode in ("metadata", "both"):
            cache_key = f"enriched:{media_type}:{ext_id}"
            doc_id = f"{req.service}:{req.arr_id}"
            try:
                # Direct DELETE — no helper for this; safe even if row absent.
                # Pass 16h: use _CACHE_VERSION instead of hardcoded "v2:" so a
                # version bump doesn't silently break re-enrich invalidation.
                cache.conn.execute("DELETE FROM api_cache WHERE cache_key = ?",
                                   (f"{_CACHE_VERSION}:{cache_key}",))
                # Also bust EVERY doc-id-keyed entry (enriched / raw /
                # raw_prefetch under e.g. "sonarr:3568") — those are what the
                # deletion discussion + pitch actually read, and a wrong-entity
                # resolution (Lupin III → Fujiko Mine) poisoned exactly those.
                cache.conn.execute("DELETE FROM api_cache WHERE cache_key LIKE ?",
                                   (f"%:{doc_id}",))

                # Pass 46 (Bug 4 / B7): bust the per-item embedding cache too.
                # The taste-vector recompute decides whether to re-embed by
                # comparing the stored ``emb_ts:{pid}`` against
                # ``EnrichmentStatus.enriched_at``. ``enrich_media_item`` on
                # this single-item path does NOT update ``enriched_at``
                # (only the bulk ``_run_enrichment`` does that), so without
                # an explicit emb-cache bust here the stored 90-day TTL
                # vector would keep representing the OLD profile long after
                # the user clicked "re-fetch metadata". Fix: look the
                # plex_rating_key up by title + category and DELETE both
                # ``emb:{pid}`` and ``emb_ts:{pid}``. Title-mismatch falls
                # back to the pre-existing TTL-based invalidation — safe
                # to miss, not safe to skip.
                from src.database.connection import get_db_session
                from src.database.models import EnrichmentStatus
                cleared_embs = 0
                with get_db_session() as _db:
                    rows = _db.query(EnrichmentStatus).filter(
                        EnrichmentStatus.title == title,
                        EnrichmentStatus.media_category == media_type,
                    ).all()
                    pids = [r.plex_rating_key for r in rows if r.plex_rating_key]
                for pid in pids:
                    cache.conn.execute(
                        "DELETE FROM api_cache WHERE cache_key IN (?, ?)",
                        (f"{_CACHE_VERSION}:emb:{pid}",
                         f"{_CACHE_VERSION}:emb_ts:{pid}"),
                    )
                    cleared_embs += 1

                cache.conn.commit()
                logger.info(
                    "[library] re-enrich: cleared cache %s (+%d emb rows)",
                    cache_key, cleared_embs,
                )
            except Exception as e:
                logger.warning("[library] cache clear failed: %s", e)
    finally:
        cache.close()

    # Fire enrichment in the background — don't block the user
    import asyncio
    from src.services.media_enricher import enrich_media_item
    from src.services.llm_priority import priority_enrichment

    async def _run():
        try:
            kwargs = {
                "title": title,
                "media_type": media_type,
                "year": re_year,     # disambiguates same-name title search
                # Write the corrected profile under the doc-id the discussion +
                # pitch read from — not only the resolved-id key.
                "plex_rating_key": f"{req.service}:{req.arr_id}",
            }
            if req.service == "sonarr":
                kwargs["tmdb_id"] = tmdb_id
                kwargs["tvdb_id"] = re_tvdb
            elif req.service == "radarr":
                kwargs["tmdb_id"] = tmdb_id
            else:  # lidarr — pin MusicBrainz to the exact artist
                kwargs["mbid"] = re_mbid
            #
            # Pass 60: wrap in priority_enrichment() so this user-triggered
            # re-enrich jumps ahead of an already-running bulk enrichment.
            # The bulk consumer's wait_for_enrichment_slot() checkpoint
            # pauses it after its current item until this finishes. The
            # summarizer is NOT evicted (unlike curator priority) — this
            # re-enrich needs it itself.
            async with priority_enrichment():
                await enrich_media_item(**kwargs)
            logger.info("[library] re-enrich done: %s/%s mode=%s",
                        req.service, title, req.mode)
        except Exception as e:
            logger.warning("[library] re-enrich failed for %s/%s: %s",
                           req.service, title, e)

    # Pass 41: track the re-enrich task so the user's "queued" status
    # message isn't lying — the task can't be silently GC'd before it
    # actually runs the enrichment pipeline.
    from src.services.bg_tasks import track_task
    track_task(_run(), name=f"library_reenrich:{req.service}:{req.arr_id}")
    return {
        "ok": True,
        "service": req.service,
        "title": title,
        "mode": req.mode,
        "queued": True,
    }


# ── Pass 16e: Add New (live search + add with curatarr tag) ───────────────

def _normalise_lookup_result(svc: str, item: dict) -> dict:
    """Strip the arr's lookup payload to what the Add-New cards need."""
    if svc == "sonarr":
        images = item.get("images") or []
        poster = next((img.get("remoteUrl") or img.get("url")
                       for img in images if img.get("coverType") == "poster"), "")
        return {
            "title":      item.get("title") or "",
            "year":       item.get("year"),
            "overview":   (item.get("overview") or "")[:400],
            "tvdb_id":    item.get("tvdbId"),
            "tmdb_id":    item.get("tmdbId"),
            "imdb_id":    item.get("imdbId"),
            "poster":     poster,
            "status":     item.get("status"),    # Continuing / Ended / Upcoming
            "network":    item.get("network"),
            "runtime":    item.get("runtime"),
            "already_added": bool(item.get("id")),  # arr returns existing id when already in library
        }
    if svc == "radarr":
        images = item.get("images") or []
        poster = next((img.get("remoteUrl") or img.get("url")
                       for img in images if img.get("coverType") == "poster"), "")
        return {
            "title":      item.get("title") or "",
            "year":       item.get("year"),
            "overview":   (item.get("overview") or "")[:400],
            "tmdb_id":    item.get("tmdbId"),
            "imdb_id":    item.get("imdbId"),
            "poster":     poster,
            "status":     item.get("status"),
            "studio":     item.get("studio"),
            "runtime":    item.get("runtime"),
            "already_added": bool(item.get("id")),
        }
    # lidarr
    images = item.get("images") or []
    poster = next((img.get("remoteUrl") or img.get("url")
                   for img in images if img.get("coverType") == "poster"), "")
    return {
        "title":         item.get("artistName") or "",
        "year":          None,
        "overview":      (item.get("overview") or "")[:400],
        "mbid":          item.get("foreignArtistId"),
        "poster":        poster,
        "status":        item.get("status"),
        "disambiguation": item.get("disambiguation"),
        "type":          item.get("artistType"),
        "already_added": bool(item.get("id")),
    }


@router.get("/search/{service}")
async def library_search(
    service: str,
    q: str,
    _user: User = Depends(get_current_user),
):
    """Live search wrapper around the arr's own lookup endpoint.

    Returns a normalised list of matches the Add-New UI can render
    without knowing service-specific shapes.
    """
    if service not in ("sonarr", "radarr", "lidarr"):
        raise HTTPException(400, "unknown service")
    q = (q or "").strip()
    if len(q) < 2:
        return {"service": service, "matches": []}
    url, api_key = _get_arr_url_key(service)
    if not url or not api_key:
        raise HTTPException(400, f"{service} not configured")

    client = _make_client(service, url, api_key)
    try:
        async with client:
            if service == "sonarr":
                raw = await client.lookup_series(q)
            elif service == "radarr":
                raw = await client.lookup_movie(q)
            else:
                raw = await client.lookup_artist(q)
    except Exception as e:
        raise HTTPException(502, f"{service} lookup failed: {e}")

    matches = [_normalise_lookup_result(service, m) for m in (raw or [])][:25]
    return {"service": service, "matches": matches}


@router.get("/semantic-search")
async def semantic_library_search(
    q: str,
    category: Optional[str] = None,
    limit: int = 10,
    user: User = Depends(get_current_user),
):
    """Natural-language search over the OWN library.

    Rides the same ChromaDB retrieval the chat's hidden RAG uses
    (services.semantic_search) — ranked owned titles with the caller's
    watched-status tag and the size tag. Read-only, so no admin gate;
    coverage follows the enrichment/vector index."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"query": q, "category": category, "results": []}
    cat = category if category in ("movie", "show", "anime", "music") else None
    from src.services.semantic_search import semantic_hits
    hits = await semantic_hits(q, n_results=max(1, min(int(limit or 10), 25)),
                               domain=cat, user_id=user.id)
    return {"query": q, "category": cat, "results": hits}


class LibraryAddRequest(BaseModel):
    service: str = Field(..., pattern="^(sonarr|radarr|lidarr)$")
    # ID fields — caller provides whichever the service needs
    tvdb_id: Optional[int] = None      # Sonarr
    tmdb_id: Optional[int] = None      # Sonarr / Radarr
    mbid: Optional[str] = None         # Lidarr
    title: str
    year: Optional[int] = None
    # Per-add overrides (optional — fall back to saved defaults)
    monitored: bool = True
    search_for_missing: bool = True
    monitor_option: Optional[str] = None     # Lidarr only
    series_type: Optional[str] = None         # Sonarr only — overrides default


@router.post("/add")
async def library_add(
    req: LibraryAddRequest,
    _user: User = Depends(require_admin),    # Pass 16l: writing to shared arrs is admin-only
):
    """Add an item to the matching arr using saved defaults + auto-tag
    with 'curatarr'.

    Defaults come from /api/library/defaults; the per-request overrides
    above let the caller tweak monitored/search/monitor_option without
    touching the saved defaults.

    Tagging: ensures the 'curatarr' tag exists in the arr (creates it
    if missing) and attaches it to the new item so the user can filter
    'Curatarr-Added' in both the Synopsis Browser and the native arr UI.
    """
    url, api_key = _get_arr_url_key(req.service)
    if not url or not api_key:
        raise HTTPException(400, f"{req.service} not configured")

    defaults = _read_defaults(req.service)
    root_folder = defaults.get("root_folder_path")
    quality_id  = defaults.get("quality_profile_id")
    if not root_folder or not quality_id:
        raise HTTPException(
            400,
            f"{req.service} defaults incomplete — set root folder + quality "
            f"profile in Settings → Library first.",
        )

    from src.services.arr_client import ensure_curatarr_tag
    client = _make_client(req.service, url, api_key)
    try:
        async with client:
            tag_id = await ensure_curatarr_tag(client)
            tags = [tag_id] if tag_id is not None else []

            if req.service == "sonarr":
                if not req.tvdb_id:
                    raise HTTPException(400, "tvdb_id required for Sonarr add")
                series_type = req.series_type or defaults.get("series_type") or "standard"
                result = await client.add_series(
                    tvdb_id=req.tvdb_id,
                    title=req.title,
                    root_folder_path=root_folder,
                    quality_profile_id=quality_id,
                    monitored=req.monitored,
                    search_for_missing=req.search_for_missing,
                    series_type=series_type,
                    tags=tags,
                )
            elif req.service == "radarr":
                if not req.tmdb_id:
                    raise HTTPException(400, "tmdb_id required for Radarr add")
                if not req.year:
                    raise HTTPException(400, "year required for Radarr add")
                result = await client.add_movie(
                    tmdb_id=req.tmdb_id,
                    title=req.title,
                    year=req.year,
                    root_folder_path=root_folder,
                    quality_profile_id=quality_id,
                    monitored=req.monitored,
                    search_for_movie=req.search_for_missing,
                    tags=tags,
                )
            else:  # lidarr
                if not req.mbid:
                    raise HTTPException(400, "mbid required for Lidarr add")
                metadata_id = defaults.get("metadata_profile_id")
                if not metadata_id:
                    raise HTTPException(
                        400, "Lidarr metadata profile not set in defaults",
                    )
                monitor_opt = req.monitor_option or defaults.get("monitor_option") or "all"
                result = await client.add_artist(
                    mbid=req.mbid,
                    artist_name=req.title,
                    root_folder_path=root_folder,
                    quality_profile_id=quality_id,
                    metadata_profile_id=metadata_id,
                    monitored=req.monitored,
                    monitor_option=monitor_opt,
                    search_for_missing=req.search_for_missing,
                    tags=tags,
                )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"{req.service} add failed: {e}")

    logger.info(
        "[library] added %s/%s id=%s tag=%s",
        req.service, req.title, (result or {}).get("id"), tag_id,
    )
    return {
        "ok":         True,
        "service":    req.service,
        "title":      req.title,
        "arr_id":     (result or {}).get("id"),
        "tag_id":     tag_id,
    }


# ── Pass 16g: Spotify Backlog (aggregated artists + Lidarr add) ───────────

@router.get("/spotify-backlog")
async def spotify_backlog(
    limit: int = 100,
    only_resolved: bool = False,
    not_added_only: bool = False,
    user: User = Depends(get_current_user),
):
    """
    Top Spotify-only artists by play count, with pre-resolved MBID +
    top-3 tracks per artist.

    Items are filtered to Spotify-source plays whose plex_item_id is
    still `spotify:...` (i.e. NOT matched to a local Plex track via
    Phase 1) — the user's "stuff I only stream, never own" set.

    ``only_resolved=True`` hides artists whose MBID hasn't been
    pre-resolved yet by Phase 1.4. With a fresh import you might want
    only_resolved=False to see the whole pipeline progress; once
    Phase 1.4 has caught up you'd flip it on so the Add-to-Lidarr
    buttons are clickable for everything visible.

    ``not_added_only=True`` (Pass 30) hides artists whose pre-resolved
    MBID is already in the cached Lidarr library. Combines with
    ``only_resolved`` — both can be on at once for a strict "ready to
    add, not yet in Lidarr" view. Artists without a resolved MBID are
    kept visible under this filter (we can't determine "added" status
    without the MBID, so we default to showing them).
    """
    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry
    from sqlalchemy import func

    # Pass 16o: build a set of MBIDs already in the Lidarr library so we
    # can flag artists as "already added" in the response. Pulls from the
    # cached Lidarr listing — no extra API call. If the cache hasn't been
    # populated yet, we just skip the cross-check (badge stays off, the
    # add button stays clickable; Lidarr will return 400 if it's truly
    # already there, which we already surface to the user).
    in_lidarr: set[str] = set()
    lidarr_hit = _LIB_CACHE.get("lidarr")
    if lidarr_hit:
        for raw in lidarr_hit.get("items_raw", []):
            mbid = raw.get("foreignArtistId")
            if mbid:
                in_lidarr.add(mbid)

    # Pass 30: when not_added_only is on, the python-side in_lidarr filter
    # below culls already-added artists AFTER SQL aggregation. Without
    # headroom that would cap the visible page below ``limit`` (e.g. if
    # the top-100 listened artists include 60 already in Lidarr the user
    # would only see 40 cards). Fetch a multiplier from SQL so the filter
    # has room to operate, then truncate to ``limit`` at the end.
    sql_fetch_limit = limit * 4 if not_added_only and in_lidarr else limit

    with get_db_session() as db:
        # Aggregate by artist (series_title in our schema for music plays)
        q = (
            db.query(
                WatchHistoryEntry.series_title,
                func.count(WatchHistoryEntry.id).label("plays"),
                func.max(WatchHistoryEntry.artist_mbid).label("mbid"),
            )
            .filter(
                WatchHistoryEntry.user_id      == user.id,
                WatchHistoryEntry.media_type   == "music",
                WatchHistoryEntry.source       == "spotify",
                WatchHistoryEntry.plex_item_id.like("spotify%"),
                WatchHistoryEntry.series_title.isnot(None),
            )
            .group_by(WatchHistoryEntry.series_title)
        )
        if only_resolved:
            q = q.having(func.max(WatchHistoryEntry.artist_mbid).isnot(None))
        rows = q.order_by(func.count(WatchHistoryEntry.id).desc()).limit(sql_fetch_limit).all()

        # Pass 30: apply not_added_only BEFORE fetching top-tracks per
        # artist, so we don't run N+1 queries for rows we're about to
        # drop. Artists without a resolved MBID keep showing (we can't
        # know their added-status — safer default is "visible").
        if not_added_only and in_lidarr:
            rows = [r for r in rows if (not r.mbid) or (r.mbid not in in_lidarr)]
        # Truncate to the requested page size after filtering.
        rows = rows[:limit]

        # Top-3 tracks per artist (N+1 queries — N is small, queries are
        # tiny indexed lookups).
        artists = []
        for r in rows:
            top_tracks_rows = (
                db.query(
                    WatchHistoryEntry.title,
                    func.count(WatchHistoryEntry.id).label("p"),
                )
                .filter(
                    WatchHistoryEntry.user_id      == user.id,
                    WatchHistoryEntry.series_title == r.series_title,
                    WatchHistoryEntry.title.isnot(None),
                    # An artist sharing a name with a TV series would otherwise
                    # count that series' episodes as "top tracks".
                    WatchHistoryEntry.media_type == "music",
                )
                .group_by(WatchHistoryEntry.title)
                .order_by(func.count(WatchHistoryEntry.id).desc())
                .limit(3)
                .all()
            )
            artists.append({
                "artist_name": r.series_title,
                "play_count":  r.plays,
                "mbid":        r.mbid,
                "mbid_resolved":   bool(r.mbid),
                "in_lidarr":       bool(r.mbid) and r.mbid in in_lidarr,  # 16o
                "top_tracks":  [{"title": t.title, "plays": t.p} for t in top_tracks_rows],
            })

        # Stats for the toolbar header
        total_unique = (
            db.query(func.count(func.distinct(WatchHistoryEntry.series_title)))
            .filter(
                WatchHistoryEntry.user_id      == user.id,
                WatchHistoryEntry.media_type   == "music",
                WatchHistoryEntry.source       == "spotify",
                WatchHistoryEntry.plex_item_id.like("spotify%"),
                WatchHistoryEntry.series_title.isnot(None),
            )
            .scalar() or 0
        )
        total_resolved = (
            db.query(func.count(func.distinct(WatchHistoryEntry.series_title)))
            .filter(
                WatchHistoryEntry.user_id      == user.id,
                WatchHistoryEntry.media_type   == "music",
                WatchHistoryEntry.source       == "spotify",
                WatchHistoryEntry.plex_item_id.like("spotify%"),
                WatchHistoryEntry.artist_mbid.isnot(None),
            )
            .scalar() or 0
        )

    in_lidarr_count = sum(1 for a in artists if a.get("in_lidarr"))

    return {
        "artists":         artists,
        "shown":           len(artists),
        "limit":           limit,
        "only_resolved":   only_resolved,
        "not_added_only":  not_added_only,  # Pass 30
        "stats": {
            "total_unique_artists":   total_unique,
            "mbid_resolved_artists":  total_resolved,
            "mbid_pending_artists":   max(0, total_unique - total_resolved),
            # 16o: only counts artists in the current visible page (matches
            # the per-card "in_lidarr" flags). Lidarr cache absent → 0.
            "lidarr_cache_loaded":    bool(lidarr_hit),
            "in_lidarr_visible":      in_lidarr_count,
        },
    }


# ── Pass 96a: Unified per-library breakdown panel ─────────────────────────────
#
# Single source of truth for both the Library Configuration screen
# (/medianew/<category>) and the Enrichment Status screen. Previously each
# screen rendered its own ad-hoc number set: View 1 (Enrichment by Category)
# showed watch-history-scoped counts, View 2 (Library Configuration) showed
# whole-library + ARR counts, and the user had no way to tell why they
# disagreed (was Music 330% a bug? was Lidarr 0% a bug? how does "232 not
# found" relate to "6,341 never processed"?). Pass 91a/b/c patched the
# headline arithmetic, but the underlying duplication of two different
# breakdowns kept biting.
#
# This endpoint resolves that by returning ONE breakdown per configured
# library, with every number carrying its denominator + basis-explanation
# inline. Both screens render the same panel; differences become impossible.
#
# Mutually-exclusive pipeline states (from _write_enrichment_db in
# enrichment.py:712, lines 743-755):
#   - llm_polished     : enriched=True, error IS NULL
#                        (full LLM summary written — terminal state)
#   - rule_based       : enriched=True, error LIKE 'rule_based%'
#                        (heuristic fallback wrote a profile; will retry
#                         for LLM upgrade — 1-day cache TTL)
#   - awaiting_llm     : enriched=True, error LIKE 'api_cached%'
#                        (game-mode: API data persisted, LLM paused)
#   - not_findable     : enriched=True, error LIKE 'Not found%'
#                        (sentinel — all metadata APIs missed. 3-day TTL.)
#   - processing_error : enriched=False (any error or no error)
#                        (pipeline crashed mid-item — always retried)
#   - never_processed  : NO EnrichmentStatus row at all
#                        (calculated as ``denominator - sum(other states)``)

@router.get("/reclassify/scan")
async def reclassify_scan(_admin: User = Depends(require_admin)):
    """Admin dry-run for the Manage → Reclassify tool.

    Read-only scan of Sonarr for series filed in the wrong library:
    Western cartoons sitting in Anime (→ TV), and AniDB anime sitting in the
    TV library (→ Anime). Returns proposed moves with posters + Sonarr
    deep-links; the actual move is a separate, explicit POST.
    """
    from src.services.library_sorting import scan_misclassified
    return await scan_misclassified()


@router.post("/reclassify/apply")
async def reclassify_apply(body: dict, _admin: User = Depends(require_admin)):
    """Execute selected reclassifications (Manage → Reclassify, Stage 2).

    Body: ``{"items": [{"sonarr_id": int, "fix": {...}}]}``. PUTs each series
    back to Sonarr (root/seriesType/qualityProfile; ``moveFiles`` queues the
    physical relocation) and re-files it inside Curatarr without re-enriching.
    """
    from src.services.library_sorting import apply_reclassify
    return await apply_reclassify(body.get("items") or [])


@router.get("/breakdown")
async def library_breakdown(
    user: User = Depends(get_current_user),
    quick: bool = False,
):
    """Unified per-library breakdown for the Library Configuration +
    Enrichment Status panels.

    Each configured library returns:
      - inventory      : Plex section + ARR counts (where data lives)
      - watch_history  : unique titles + total plays (what the user consumed)
      - pipeline       : mutually-exclusive enrichment-state counts
                         (LLM-polished / rule-based / awaiting-LLM /
                          not-findable / processing-error / never-processed)
      - vector_store   : ChromaDB embedding coverage
      - music_extras   : (music only) MBID-resolve + Plex-match + Spotify-/
                         Last.fm-genre coverage from the music pipeline

    Every number includes its denominator + ``basis`` text so the UI can
    explain WHY a count is what it is — no more cryptic percentages.

    ``quick=True`` reuses cached ARR counts (no live ARR calls); use it
    for fast page loads. The Enrichment Status SSE refresh uses
    ``quick=False`` to keep ARR totals fresh between manual refreshes.
    """
    import asyncio as _asyncio
    import httpx as _httpx
    from sqlalchemy import func as _func
    from src.database.connection import get_db_session
    from src.database.models import (
        LibraryConfig as _LC,
        EnrichmentStatus as _ES,
        WatchHistoryEntry as _WH,
    )

    # ── 1. Read configured libraries ─────────────────────────────────────────
    with get_db_session() as db:
        configs = db.query(_LC).all()
        library_rows = [
            {
                "key":       c.plex_section_key,
                "title":     c.plex_section_title,
                "plex_type": c.plex_section_type,
                "category":  c.media_category,
            }
            for c in configs
        ]

    # ── 2. ARR counts (cached for 5 min; quick=True bypasses live fetch) ─────
    from src.routers.enrichment import _get_arr_counts
    if quick:
        import json as _json
        cached_raw = get_state("arr_counts_cache")
        arr_counts = _json.loads(cached_raw) if cached_raw else {}
    else:
        try:
            arr_counts = await _get_arr_counts()
        except Exception as e:
            logger.warning("[breakdown] ARR counts fetch failed: %s", e)
            arr_counts = {}

    # ── 3. Plex section item counts (one call per section, parallel) ────────
    plex_section_counts: dict[str, int] = {}
    plex_scanned_at: dict[str, str | None] = {}
    plex_url   = settings.effective_plex_url
    plex_token = settings.effective_plex_token
    if plex_url and plex_token and library_rows:
        headers = {
            "Accept": "application/json",
            "X-Plex-Token": plex_token,
            "X-Plex-Client-Identifier": settings.PLEX_CLIENT_ID,
        }
        # Section metadata (scanned_at + verify keys still exist) — single call
        try:
            async with _httpx.AsyncClient(timeout=15) as client:
                meta_resp = await client.get(f"{plex_url}/library/sections", headers=headers)
                if meta_resp.status_code == 200:
                    for sec in (meta_resp.json()
                                .get("MediaContainer", {})
                                .get("Directory", []) or []):
                        skey = str(sec.get("key", ""))
                        ts   = sec.get("scannedAt")
                        try:
                            plex_scanned_at[skey] = (
                                datetime.utcfromtimestamp(int(ts)).isoformat() + "Z"
                                if ts else None
                            )
                        except Exception:
                            plex_scanned_at[skey] = None

                async def _count(client_, key):
                    try:
                        r = await client_.get(
                            f"{plex_url}/library/sections/{key}/all",
                            params={"X-Plex-Container-Size": "0",
                                    "X-Plex-Container-Start": "0"},
                            headers=headers,
                        )
                        if r.status_code == 200:
                            return int(r.json()
                                       .get("MediaContainer", {})
                                       .get("totalSize", 0))
                    except Exception:
                        pass
                    return 0

                counts_out = await _asyncio.gather(
                    *[_count(client, row["key"]) for row in library_rows],
                    return_exceptions=True,
                )
                for row, val in zip(library_rows, counts_out):
                    plex_section_counts[row["key"]] = (
                        val if isinstance(val, int) else 0
                    )
        except Exception as e:
            logger.warning("[breakdown] Plex section fetch failed: %s", e)

    # ── 4. Per-category EnrichmentStatus state breakdown ────────────────────
    # One pass: group by (media_category, state_bucket) using SQL CASE.
    # Excludes Spotify ext-script seeds (Pass 91b — they aren't in any Plex
    # section so they don't belong to a library row here).
    from sqlalchemy import case as _case, and_ as _and
    state_buckets: dict[str, dict[str, int]] = {}
    with get_db_session() as db:
        # Pass 98: split the old "processing_error" catch-all into two honest
        # buckets:
        #   - ``queued_for_retry``   : enriched=False, error IS NULL — the
        #                              row exists in tracking but the pipeline
        #                              hasn't (re-)processed it yet. This
        #                              covers freshly-seeded items AND items
        #                              an admin reset (e.g. after the
        #                              not_findable burst caused by the
        #                              May-16 TMDB outage poisoning 6,464
        #                              movies in one hour).
        #   - ``processing_error``   : enriched=False, error IS NOT NULL —
        #                              the pipeline actually tried and
        #                              recorded an error string.
        # Pre-98 both collapsed into ``processing_error`` which was
        # misleading: a freshly-reset row got the same "warning" label as
        # a row that genuinely crashed. The two states deserve their own
        # bucket so the user can tell the difference at a glance.
        # Phase 2 #40: split the LLM-polished bucket on ``provisional`` —
        # provisional rows (fast-tier fetch, MB / Jikan / supplements still
        # pending the #41 upgrade pass) get their own bucket so the user
        # can see at a glance how many items are "enriched temporary".
        # The provisional-True case MUST come before the generic
        # ``error IS NULL → llm_polished`` so it wins; legacy rows have
        # provisional=False (default 0 in the migration), and the bool
        # coerces correctly under SQLite + SQLAlchemy.
        bucket_expr = _case(
            (_ES.enriched == False, _case(
                (_ES.error.is_(None), "queued_for_retry"),
                else_="processing_error",
            )),
            (_and(_ES.error.is_(None), _ES.provisional == True), "enriched_provisional"),
            (_ES.error.is_(None), "llm_polished"),
            (_ES.error.like("rule_based%"), "rule_based"),
            (_ES.error.like("api_cached%"), "awaiting_llm"),
            (_ES.error.like("Not found%"), "not_findable"),
            else_="processing_error",
        ).label("bucket")

        rows = (
            db.query(
                _ES.media_category,
                bucket_expr,
                _func.count(_ES.id).label("n"),
            )
            .filter(~_ES.plex_rating_key.like("ext-script:%"))
            # Group by the case-expression directly (not its label string).
            # SQLite tolerates both, but other dialects bind the label only
            # after SELECT resolution — passing the expression keeps this
            # portable if we ever swap engines.
            .group_by(_ES.media_category, bucket_expr)
            .all()
        )
        for cat, bucket, n in rows:
            state_buckets.setdefault(cat, {})[bucket] = int(n)

        vector_ready_counts = dict(
            db.query(_ES.media_category, _func.count(_ES.id))
            .filter(
                _ES.vector_ready == True,
                ~_ES.plex_rating_key.like("ext-script:%"),
            )
            .group_by(_ES.media_category)
            .all()
        )

        # Watch-history per category (unique titles + total plays) for the
        # logged-in user.
        wh_unique_show = dict(
            db.query(
                _WH.media_type,
                _func.count(_func.distinct(_WH.series_title)),
            )
            .filter(
                _WH.user_id == user.id,
                _WH.media_type.in_(["show", "anime", "music"]),
                _WH.series_title.isnot(None),
            )
            .group_by(_WH.media_type)
            .all()
        )
        wh_unique_movie = (
            db.query(_func.count(_func.distinct(_WH.title)))
            .filter(_WH.user_id == user.id, _WH.media_type == "movie")
            .scalar() or 0
        )
        wh_total_plays = dict(
            db.query(_WH.media_type, _func.count(_WH.id))
            .filter(_WH.user_id == user.id)
            .group_by(_WH.media_type)
            .all()
        )

        # ── Music-specific: Spotify pipeline phase coverage ──────────────────
        # Computed once even if multiple music libraries are configured (rare).
        music_extras = None
        has_music_lib = any(r["category"] == "music" for r in library_rows)
        if has_music_lib:
            # Phase 1: Plex match — spotify-source rows whose plex_item_id no
            # longer starts with 'spotify:' have been rewritten to point at a
            # real Plex track ID by match_spotify_to_plex.
            spotify_total_plays = (
                db.query(_func.count(_WH.id))
                .filter(_WH.user_id == user.id,
                        _WH.media_type == "music",
                        _WH.source == "spotify")
                .scalar() or 0
            )
            spotify_unmatched_plays = (
                db.query(_func.count(_WH.id))
                .filter(_WH.user_id == user.id,
                        _WH.media_type == "music",
                        _WH.source == "spotify",
                        _WH.plex_item_id.like("spotify%"))
                .scalar() or 0
            )
            spotify_matched_plays = max(
                0, spotify_total_plays - spotify_unmatched_plays
            )

            # Phase 1.4: MBID resolve — distinct spotify artists with
            # artist_mbid set. Counts only the still-unmatched set (matched
            # rows had their plex_item_id rewritten and already feed
            # taste-vector through the local Plex track).
            spotify_unique_artists = (
                db.query(_func.count(_func.distinct(_WH.series_title)))
                .filter(_WH.user_id == user.id,
                        _WH.media_type == "music",
                        _WH.source == "spotify",
                        _WH.plex_item_id.like("spotify%"),
                        _WH.series_title.isnot(None))
                .scalar() or 0
            )
            spotify_mbid_resolved = (
                db.query(_func.count(_func.distinct(_WH.series_title)))
                .filter(_WH.user_id == user.id,
                        _WH.media_type == "music",
                        _WH.source == "spotify",
                        _WH.plex_item_id.like("spotify%"),
                        _WH.series_title.isnot(None),
                        _WH.artist_mbid.isnot(None))
                .scalar() or 0
            )

            # Phase 1.5 + Phase 2: Genre coverage — both phases write to the
            # same ``genres`` column on the same rows, so we report combined
            # coverage. The split between "Spotify wrote it" vs "Last.fm
            # fallback wrote it" isn't tracked at the row level (would
            # require a new column); the honest number is the combined
            # "rows with genres" / "rows total".
            spotify_with_genres = (
                db.query(_func.count(_WH.id))
                .filter(_WH.user_id == user.id,
                        _WH.media_type == "music",
                        _WH.source == "spotify",
                        _WH.plex_item_id.like("spotify%"),
                        _WH.genres.isnot(None))
                .scalar() or 0
            )

            music_extras = {
                "phase_1_plex_match": {
                    "label":     "Phase 1 — Plex Match",
                    "matched":   spotify_matched_plays,
                    "total":     spotify_total_plays,
                    "remaining": spotify_unmatched_plays,
                    "pct":       round(
                        100 * spotify_matched_plays / max(spotify_total_plays, 1)
                    ),
                    "explainer": (
                        "Spotify plays where a local Plex track was found "
                        "and ``plex_item_id`` was rewritten from "
                        "``spotify:track:…`` to the Plex rating key. "
                        "Matched plays feed the taste vector through the "
                        "local track and skip Phases 1.4/1.5/2 entirely."
                    ),
                },
                "phase_1_4_mbid_resolve": {
                    "label":     "Phase 1.4 — MBID Resolve (MusicBrainz)",
                    "resolved":  spotify_mbid_resolved,
                    "total":     spotify_unique_artists,
                    "remaining": max(0, spotify_unique_artists - spotify_mbid_resolved),
                    "pct":       round(
                        100 * spotify_mbid_resolved / max(spotify_unique_artists, 1)
                    ),
                    "explainer": (
                        "Distinct still-unmatched Spotify artists with a "
                        "resolved MusicBrainz MBID (``artist_mbid``). MBID "
                        "enables the one-click ‘Add to Lidarr’ flow on the "
                        "Spotify-Backlog page without a live MB lookup. "
                        "Throttled to 1 req/s — use ``scripts/mbid_speedrunner.py`` "
                        "to clear a backlog faster than the daily cap allows."
                    ),
                },
                "phase_1_5_2_genres": {
                    "label":     "Phase 1.5 + 2 — Genre Coverage (Spotify + Last.fm)",
                    "with_genres": spotify_with_genres,
                    "total":     spotify_unmatched_plays,
                    "remaining": max(0, spotify_unmatched_plays - spotify_with_genres),
                    "pct":       round(
                        100 * spotify_with_genres / max(spotify_unmatched_plays, 1)
                    ),
                    "explainer": (
                        "Still-unmatched Spotify plays with genres filled. "
                        "Phase 1.5 (Spotify Client-Credentials API) runs first; "
                        "Phase 2 (Last.fm) is the fallback for anything Spotify "
                        "couldn't resolve. Both phases write the same "
                        "``genres`` column, so the split is reported combined."
                    ),
                },
            }

    # ── 5. Vector-store counts (ChromaDB, per-service prefix) ───────────────
    # Lazy import so the endpoint still loads when Chroma is misconfigured.
    def _vector_count(svc: str) -> int:
        try:
            from src.vector_store.chromadb_wrapper import get_chroma_db
            return get_chroma_db().count_by_id_prefix(f"{svc}:")
        except Exception:
            return 0

    # ── 6. Assemble per-library response ────────────────────────────────────
    arr_for_category = {
        "movie": "radarr",
        "show":  "sonarr",
        "anime": "sonarr",
        "music": "lidarr",
    }
    unit_for_category = {
        "movie": "movies",
        "show":  "series",
        "anime": "series",
        "music": "artists",
    }
    label_for_category = {
        "movie": "Movies",
        "show":  "Shows",
        "anime": "Anime",
        "music": "Music",
    }

    state_definitions = {
        "llm_polished":     ("✅ LLM-polished",
                             "enriched=True, error IS NULL — full LLM summary written from a canonical (full-tier) fetch. Terminal state."),
        "enriched_provisional": ("🌗 Enriched (provisional)",
                             "enriched=True, fetch_tier='fast' — LLM-polished from a fast-only fetch (Last.fm for music, no Jikan/OMDb/TMDB supplement). The hourly source-upgrade scheduler (#41) promotes 30 of these per hour to the full canonical fetch, oldest first."),
        "rule_based":       ("🔧 Rule-based only",
                             "Heuristic fallback wrote a profile; LLM upgrade pending. 1-day cache TTL before retry."),
        "awaiting_llm":     ("⏳ Awaiting LLM polish",
                             "Game-mode: API data was persisted, LLM was paused to free GPU. Next run finishes the polish."),
        "not_findable":     ("❌ Not findable",
                             "All metadata APIs (TMDB/AniList/MB/OMDb…) missed. 3-day TTL — may resolve if APIs add the title."),
        "queued_for_retry": ("🕒 Queued for retry",
                             "Row exists in tracking but the pipeline hasn't processed it yet — freshly seeded items, or items an admin reset (e.g. after a bad-API burst). Picked up on the next enrichment run."),
        "processing_error": ("⚠️ Processing error",
                             "Pipeline crashed mid-item AND recorded an error string. Always retried on the next run."),
        "never_processed":  ("📋 Never processed",
                             "No row in tracking — item hasn't reached the enrichment queue yet. Hit ‘Start enrichment’ to queue."),
    }

    # Sonarr backs BOTH 'show' and 'anime', so its ``total`` is the COMBINED
    # series count. Using it as a per-category denominator reports the other
    # category's items as "never processed" (all anime counted as unprocessed
    # shows and vice-versa) — that's why those bars read a bogus 33% / 74%
    # (and their sum exceeded the real Sonarr total). Flag any service that
    # backs more than one category; for those, the per-category Plex section
    # count is the correct denominator instead of the shared ARR total.
    from collections import Counter as _Counter
    _shared_arr_services = {
        s for s, n in _Counter(arr_for_category.values()).items() if n > 1
    }

    libraries_out = []
    for row in library_rows:
        cat = row["category"]
        if cat == "ignore":
            continue

        svc = arr_for_category.get(cat)
        arr_block = None
        if svc and svc in arr_counts and not arr_counts[svc].get("error"):
            ac = arr_counts[svc]
            arr_block = {
                "service":    svc,
                "total":      ac.get("total"),
                "downloaded": ac.get("downloaded"),
                "monitored":  ac.get("monitored"),
                "stale":      bool(ac.get("stale")),
                "stale_reason": ac.get("stale_reason"),
            }
            if svc == "lidarr":
                arr_block["total_albums"] = ac.get("total_albums")
        elif svc and svc in arr_counts:
            arr_block = {
                "service": svc,
                "error":   arr_counts[svc].get("error", "unreachable"),
            }

        plex_block = {
            "key":         row["key"],
            "title":       row["title"],
            "item_count":  plex_section_counts.get(row["key"], 0),
            "scanned_at":  plex_scanned_at.get(row["key"]),
        }

        # Pipeline state breakdown — fill in missing buckets with 0
        cat_buckets = state_buckets.get(cat, {})
        tracked_total = sum(cat_buckets.values())

        # Denominator picks the most authoritative whole-library count
        # available. For Movies/Music (1 category per ARR): ARR total wins
        # (file-level truth, includes items not yet in Plex). For an ARR that
        # backs several categories (Sonarr → show + anime), its total is the
        # combined count and would over-count per category, so we use the
        # per-category Plex section count instead. No ARR / no Plex section:
        # fall back to the tracked count.
        if (arr_block and isinstance(arr_block.get("total"), int)
                and svc not in _shared_arr_services):
            denominator = arr_block["total"]
            denom_basis = (
                f"{svc.capitalize()} total — every artist/series/movie "
                f"the ARR is aware of (managed or not yet downloaded)."
            )
        elif plex_block["item_count"]:
            denominator = plex_block["item_count"]
            denom_basis = (
                "Plex section item count — number of items in the matched "
                "Plex library section"
                + (f" ({svc.capitalize()} backs multiple categories, so its "
                   f"combined total can't be a per-category denominator)."
                   if svc in _shared_arr_services else ".")
            )
        else:
            denominator = tracked_total
            denom_basis = (
                "Tracked-only fallback — no ARR or Plex inventory number "
                "available, so the denominator is just the number of items "
                "with an EnrichmentStatus row."
            )

        # ``never_processed`` is the implied state: denominator minus every
        # item we have a row for. Clamped at 0 because the ARR/Plex
        # denominator can occasionally drift below tracked (e.g. items
        # removed from ARR but still in EnrichmentStatus).
        never_processed = max(0, denominator - tracked_total)

        states_out = {}
        for key, (label, explainer) in state_definitions.items():
            if key == "never_processed":
                count = never_processed
            else:
                count = cat_buckets.get(key, 0)
            states_out[key] = {
                "count":     count,
                "pct":       min(100.0, round(100 * count / max(denominator, 1), 1)),
                "label":     label,
                "explainer": explainer,
            }

        # Vector store coverage — use EnrichmentStatus.vector_ready
        # (per-category, accurate) over ChromaDB prefix counts (per-service,
        # mixes show+anime under sonarr:). Fall back to Chroma when the
        # category has no ARR (e.g. movies → radarr: but no LibraryConfig).
        vector_indexed = vector_ready_counts.get(cat, 0)
        vector_basis = (
            "EnrichmentStatus.vector_ready=True for this category — "
            "per-category, accurate (show vs anime stay split)."
        )
        if vector_indexed == 0 and svc:
            chroma_n = _vector_count(svc)
            if chroma_n:
                vector_indexed = chroma_n
                vector_basis = (
                    f"ChromaDB doc count with id prefix ``{svc}:`` — "
                    f"per-service, may mix show+anime under ``sonarr:``."
                )

        # Watch-history slice for this category
        if cat == "movie":
            wh_unique = int(wh_unique_movie or 0)
        else:
            wh_unique = int(wh_unique_show.get(cat, 0) or 0)
        wh_total  = int(wh_total_plays.get(cat, 0) or 0)

        lib_entry = {
            "category":     cat,
            "label":        label_for_category.get(cat, cat.capitalize()),
            "unit":         unit_for_category.get(cat, "items"),
            "arr_service":  svc,

            "inventory": {
                "plex_section": plex_block,
                "arr":          arr_block,
            },

            "watch_history": {
                "unique_titles": wh_unique,
                "total_plays":   wh_total,
                "scope": (
                    "Distinct movies you've watched (counted once per title, "
                    "no matter how many times you re-watched)."
                    if cat == "movie" else
                    "Distinct series you've watched (counted once per series, "
                    "summed across all episodes)."
                    if cat in ("show", "anime") else
                    "Distinct artists in your listening history (Plex + Spotify)."
                ),
            },

            "pipeline": {
                "denominator":       denominator,
                "denominator_basis": denom_basis,
                "tracked":           tracked_total,
                "states":            states_out,
            },

            "vector_store": {
                "indexed":     vector_indexed,
                "denominator": denominator,
                "pct":         min(100.0, round(100 * vector_indexed / max(denominator, 1), 1)),
                "basis":       vector_basis,
            },

            "music_extras": music_extras if cat == "music" else None,
        }
        libraries_out.append(lib_entry)

    return {
        "libraries":    libraries_out,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "arr_quick":    quick,
    }
