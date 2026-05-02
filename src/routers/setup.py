"""
Curatarr 1.0 - Setup Router

Handles the first-run setup wizard.
All endpoints work without authentication (FIRST_RUN=true state).
"""

import logging
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
from typing import Optional

from src.config import settings
from src.services.setup_wizard import (
    test_plex, test_ollama, test_arr, test_tmdb, test_lastfm,
    write_env, build_ollama_models, SETUP_FIELDS,
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
        "has_radarr": bool(settings.RADARR_URL and settings.RADARR_API_KEY),
        "has_sonarr": bool(settings.SONARR_URL and settings.SONARR_API_KEY),
        "has_lidarr": bool(settings.LIDARR_URL and settings.LIDARR_API_KEY),
    }


@router.get("/fields")
async def get_setup_fields():
    """Return setup field definitions for the wizard UI."""
    return {"fields": SETUP_FIELDS}


class TestRequest(BaseModel):
    service: str       # plex / ollama / radarr / sonarr / lidarr / tmdb / lastfm
    url: Optional[str] = None
    token: Optional[str] = None
    api_key: Optional[str] = None


@router.post("/test")
async def test_connection(req: TestRequest):
    """Test a service connection during setup."""
    if req.service == "plex":
        return await test_plex(req.url or "", req.token or "")
    if req.service == "ollama":
        return await test_ollama(req.url or settings.effective_ollama)
    if req.service in ("radarr", "sonarr", "lidarr"):
        return await test_arr(req.url or "", req.api_key or "", req.service)
    if req.service == "tmdb":
        return await test_tmdb(req.api_key or "")
    if req.service == "lastfm":
        return await test_lastfm(req.api_key or "")
    raise HTTPException(status_code=400, detail=f"Unknown service: {req.service}")


class SetupCompleteRequest(BaseModel):
    plex_url: str
    plex_token: str
    ollama_endpoint: str = "http://localhost:11434"
    base_curator_model: str = "qwen2.5:32b"
    base_summarizer_model: str = "qwen2.5:3b"
    embedding_model: str = "nomic-embed-text"
    tmdb_api_key: str = ""
    lastfm_api_key: str = ""
    radarr_url: str = ""
    radarr_api_key: str = ""
    sonarr_url: str = ""
    sonarr_api_key: str = ""
    lidarr_url: str = ""
    lidarr_api_key: str = ""


@router.post("/complete")
async def complete_setup(req: SetupCompleteRequest, background_tasks: BackgroundTasks):
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
async def build_models_endpoint(background_tasks: BackgroundTasks):
    """Build curatarr-curator and curatarr-summarizer Ollama models."""
    from src.config import settings
    results = await build_ollama_models(
        settings.effective_ollama,
        settings.BASE_CURATOR_MODEL,
        settings.BASE_SUMMARIZER_MODEL,
    )
    return {
        "curator": results.get("curator", False),
        "summarizer": results.get("summarizer", False),
    }
