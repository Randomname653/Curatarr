"""
Curatarr - Setup Router

Handles the first-run setup wizard.

Auth model:
  - /status, /fields are public (the frontend wizard reads them before any
    user account exists).
  - /test, /complete, /build-models are gated by ``require_admin_or_first_run``:
    callable WITHOUT auth while no admin User exists (initial onboarding),
    admin-only thereafter (prevents post-setup SSRF + .env-overwrite by
    arbitrary callers).
"""

import logging
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from src.config import settings
from src.routers.auth import require_admin_or_first_run
from src.services.setup_wizard import (
    test_plex, test_ollama, test_arr, test_tmdb, test_lastfm, test_spotify,
    write_env, build_ollama_models, SETUP_FIELDS,
    endpoint_privacy_note,   # Pass 97
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/status")
async def setup_status():
    """Return whether setup is complete and what's configured."""
    return {
        "first_run": settings.FIRST_RUN,
        "configured": settings.is_configured,
        "has_plex": bool(settings.effective_plex_url and settings.effective_plex_token),
        "has_ollama": bool(settings.effective_ollama),
        "has_tmdb": bool(settings.TMDB_API_KEY),
        "has_lastfm": bool(settings.LASTFM_API_KEY),
        "has_spotify": bool(settings.SPOTIFY_CLIENT_ID and settings.SPOTIFY_CLIENT_SECRET),
        "has_radarr": bool(settings.RADARR_URL and settings.RADARR_API_KEY),
        "has_sonarr": bool(settings.SONARR_URL and settings.SONARR_API_KEY),
        "has_lidarr": bool(settings.LIDARR_URL and settings.LIDARR_API_KEY),
    }


@router.get("/fields")
async def get_setup_fields():
    """Return setup field definitions for the wizard UI."""
    return {"fields": SETUP_FIELDS}


class TestRequest(BaseModel):
    service: str       # plex / ollama / radarr / sonarr / lidarr / tmdb / lastfm / spotify
    url: Optional[str] = None
    token: Optional[str] = None
    api_key: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None


@router.post("/test")
async def test_connection(
    req: TestRequest,
    _gate=Depends(require_admin_or_first_run),
):
    """Test a service connection during setup.

    Pass 97: when the service is one of the endpoint-style ones (plex /
    ollama / *arr), check whether the URL points at a private address
    and attach a ``privacy_warning`` if not. Frontend renders this as a
    yellow banner. We never block — the user might legitimately point
    Ollama at a cloud GPU box — but we never go silent about it either.
    """
    if req.service == "plex":
        result = await test_plex(req.url or "", req.token or "")
    elif req.service == "ollama":
        result = await test_ollama(req.url or settings.effective_ollama)
    elif req.service in ("radarr", "sonarr", "lidarr"):
        result = await test_arr(req.url or "", req.api_key or "", req.service)
    elif req.service == "tmdb":
        result = await test_tmdb(req.api_key or "")
    elif req.service == "lastfm":
        result = await test_lastfm(req.api_key or "")
    elif req.service == "spotify":
        result = await test_spotify(req.client_id or "", req.client_secret or "")
    else:
        raise HTTPException(status_code=400, detail=f"Unknown service: {req.service}")

    # Only the endpoint-style services have a URL to classify. TMDB /
    # Last.fm / Spotify go to fixed public hostnames by design — no
    # warning makes sense there.
    if req.service in ("plex", "ollama", "radarr", "sonarr", "lidarr"):
        url = req.url or (settings.effective_ollama if req.service == "ollama" else "")
        warn = endpoint_privacy_note(url)
        if warn and isinstance(result, dict):
            result["privacy_warning"] = warn
    return result


class SetupCompleteRequest(BaseModel):
    plex_url: str
    plex_token: str
    ollama_endpoint: str = "http://localhost:11434"
    base_curator_model: str = "gemma4:31b"
    base_summarizer_model: str = "granite4.1:8b"
    embedding_model: str = "nomic-embed-text-v2-moe"
    tmdb_api_key: str = ""
    # omdb was in SETUP_FIELDS and in the .env template from the start, but
    # never in this request model — pydantic silently dropped the value, so
    # the wizard could not actually save an OMDb key. The schema lives in
    # three places (SETUP_FIELDS, this model, the frontend forms); a test
    # now asserts the three agree.
    omdb_api_key: str = ""
    lastfm_api_key: str = ""
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    radarr_url: str = ""
    radarr_api_key: str = ""
    sonarr_url: str = ""
    sonarr_api_key: str = ""
    lidarr_url: str = ""
    lidarr_api_key: str = ""
    soulsync_url: str = ""
    soulsync_api_key: str = ""


@router.post("/complete")
async def complete_setup(
    req: SetupCompleteRequest,
    background_tasks: BackgroundTasks,
    _gate=Depends(require_admin_or_first_run),
):
    """
    Finalize setup: write .env and kick off model building.
    The app needs a restart after this to reload settings.
    """
    # Write .env
    write_env(req.dict())

    # Build Ollama models in background (takes a moment)
    background_tasks.add_task(
        build_ollama_models,
        req.ollama_endpoint,
        req.base_curator_model,
        req.base_summarizer_model,
    )

    return {
        "status": "ok",
        "message": "Configuration saved. Building Ollama models in background. Please restart Curatarr.",
        "restart_required": True,
    }


@router.post("/build-models")
async def build_models_endpoint(
    background_tasks: BackgroundTasks,
    _gate=Depends(require_admin_or_first_run),
):
    """Build curatarr-curator and curatarr-summarizer Ollama models
    (plus curatarr-pitcher when the two-bake split is enabled)."""
    from src.config import settings
    results = await build_ollama_models(
        settings.effective_ollama,
        settings.BASE_CURATOR_MODEL,
        settings.BASE_SUMMARIZER_MODEL,
        base_pitcher=(settings.BASE_PITCHER_MODEL
                      if (settings.PITCHER_MODEL or "").strip() else None),
    )
    return {
        "curator": results.get("curator", False),
        "summarizer": results.get("summarizer", False),
        "pitcher": results.get("pitcher"),
    }
