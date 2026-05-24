"""
Curatarr 1.0 - Configuration
Loaded from .env (written by Setup Wizard on first run).
"""

import os
from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import field_validator, HttpUrl
from pydantic.types import SecretStr
from typing import Optional


class Settings(BaseSettings):
    # ── App ──────────────────────────────────────────────────────────────────
    APP_NAME: str = "Curatarr"
    VERSION: str = "1.0.0"
    PORT: int = 8000
    DEBUG: bool = False
    FIRST_RUN: bool = True   # set to False by setup wizard after completion

    # ── Security ─────────────────────────────────────────────────────────────
    JWT_SECRET: Optional[SecretStr] = None
    PBKDF2_ITERATIONS: int = 1_000_000
    AES_KEY_SIZE: int = 32

    # ── Database ─────────────────────────────────────────────────────────────
    DATABASE_URL: str = "sqlite:///./data/curatarr.db"
    CHROMADB_PATH: str = "./data/chromadb"
    ENRICHMENT_CACHE: str = "./data/cache/enrichment.db"

    # ── Plex ─────────────────────────────────────────────────────────────────
    PLEX_URL: Optional[HttpUrl] = None
    PLEX_TOKEN: Optional[SecretStr] = None
    PLEX_CLIENT_ID: str = "Curatarr"
    PLEX_REDIRECT_URI: HttpUrl = "http://localhost:8000"

    # ── Ollama ───────────────────────────────────────────────────────────────
    OLLAMA_ENDPOINT: HttpUrl = "http://localhost:11434"
    # ── Model names — set these in .env, do not edit here ────────────────────
    # Baked modelfile names (created by python build_models.py)
    CURATOR_MODEL: str = "curatarr-curator"
    SUMMARIZER_MODEL: str = "curatarr-summarizer"
    EMBEDDING_MODEL: str = "nomic-embed-text"
    # Primary models — .env is the single source of truth.
    # Defaults here are only used when .env has no entry.
    BASE_CURATOR_MODEL: str = "qwen3.6:27b"        # chat + recommendations
    BASE_SUMMARIZER_MODEL: str = "granite4.1:3b"   # enrichment + memory extraction
    # ── Why granite4.1:3b? ────────────────────────────────────────────────
    # Tournament finale (2026-05-24, 200 items × 4 models, see
    # tournament_finale_*.md) crowned granite4.1:3b as the strongest
    # summariser: 8/8 known-difficult curated items passed (incl. The 4400
    # number trap that its 8B sibling failed), 99.5% JSON validity,
    # 8.6s p50 (≈1.7× faster than the previous 8B production). It also
    # frees ~4GB VRAM for the curator slot. Defense-in-Depth layers
    # (Idea-A archetype blacklist + Idea-C number self-correction loop in
    # media_enricher.py) stay — the 2026-05-24 blacklist-off A/B proved
    # all three tested models regress to "harem" attribution on Bunny Girl
    # the moment the blacklist is removed.
    # Backup models — defined in .env for easy switching, not yet used in code.
    BACKUP_CURATOR_MODEL: str = "laguna-xs.2:q4_K_M"
    BACKUP_SUMMARIZER_MODEL: str = "granite4.1:8b"   # previous production, kept as safety fallback
    # When True, <think>...</think> blocks are stripped from LLM responses.
    # Set False — current models (granite4.1:3b, qwen3.6:27b) do not emit think blocks.
    # Keep this flag so switching to a reasoning model only requires an .env change.
    LLM_THINK_TAGS: bool = False
    # Kept for .env backward-compat; no longer used — enrichment uses a single LLM worker.
    ENRICH_PARALLEL_SLOTS: int = 0

    # ── ARR Services ─────────────────────────────────────────────────────────
    RADARR_URL: Optional[str] = None
    RADARR_API_KEY: Optional[str] = None
    SONARR_URL: Optional[str] = None
    SONARR_API_KEY: Optional[str] = None
    LIDARR_URL: Optional[str] = None
    LIDARR_API_KEY: Optional[str] = None

    # ── Metadata APIs ────────────────────────────────────────────────────────
    TMDB_API_KEY: Optional[str] = None
    OMDB_API_KEY: Optional[str] = None   # optional — free at omdbapi.com, 1000 req/day
    LASTFM_API_KEY: Optional[str] = None         # music tags + similar artists (optional)
    SPOTIFY_CLIENT_ID: Optional[str] = None      # Client Credentials — no user login needed
    SPOTIFY_CLIENT_SECRET: Optional[str] = None  # from developer.spotify.com
    # AniList: no key needed (public GraphQL)
    # MusicBrainz: no key needed

    # ── Sync ─────────────────────────────────────────────────────────────────
    SYNC_ON_STARTUP: bool = True
    SYNC_INTERVAL_HOURS: int = 24

    # ── Binge detection ──────────────────────────────────────────────────────
    BINGE_EPISODE_THRESHOLD: int = 3      # episodes in one session = binge
    BINGE_SESSION_HOURS: int = 6          # session window
    BINGE_SERIES_PERCENT: float = 0.5     # >50% of season in 48h = binge

    # ── Enrichment TTL & game-pause ──────────────────────────────────────────
    # Items enriched longer than TTL_DAYS ago are queued for refresh (staggered).
    ENRICHMENT_TTL_DAYS: int = 90
    # Max items marked for refresh per daily scheduler run (spread across ~90 days).
    ENRICHMENT_REFRESH_BATCH_SIZE: int = 150
    # ARR pre-enrichment batch: items enriched nightly (02:30) before deletion sync.
    # Each item takes ~15-25s (TMDB + LLM + embedding). 80 ≈ 20-30 min per night.
    ARR_PRE_ENRICH_BATCH: int = 80
    # Comma-separated extra game .exe names (e.g. "RDR2.exe,HogwartsLegacy.exe").
    # Tier-1 launcher signals and DB-classified processes are always checked first.
    EXTRA_GAME_PROCESSES: str = ""

    # ── CORS ─────────────────────────────────────────────────────────────────
    # Override via .env as JSON: CORS_ORIGINS=["http://myserver:8000"]
    CORS_ORIGINS: list = ["http://localhost:8000", "http://127.0.0.1:8000"]

    # ── API surface hardening ────────────────────────────────────────────────
    # Pass 97: Swagger / ReDoc are off by default. Curatarr binds to
    # 0.0.0.0 (see start.bat) so /api/docs is reachable from the LAN
    # and gives a free map of every endpoint to anyone who finds the
    # port. Set ENABLE_DOCS=true in .env when you need the interactive
    # API explorer for development.
    ENABLE_DOCS: bool = False

    # Legacy aliases from old .env files
    PLEX_BASE_URL: str = ""
    PLEX_AUTH_TOKEN: str = ""
    OLLAMA_BASE_URL: str = ""
    OLLAMA_MODEL: str = ""
    LLM_MODEL: str = ""
    LOG_LEVEL: str = "INFO"
    DEBUG_MODE: str = "false"
    MAX_RECOMMENDATIONS: int = 10
    RECOMMENDATION_SCORE_THRESHOLD: float = 0.7
    WATCH_HISTORY_RETENTION_DAYS: int = 730
    WATCH_HISTORY_BATCH_SIZE: int = 100
    MDBLIST_API_KEY: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "allow"}

    @property
    def effective_plex_url(self) -> str:
        if self.PLEX_URL:
            return str(self.PLEX_URL).rstrip("/")
        return self.PLEX_BASE_URL.rstrip("/")

    @property
    def effective_plex_token(self) -> str:
        if self.PLEX_TOKEN:
            return self.PLEX_TOKEN.get_secret_value()
        return self.PLEX_AUTH_TOKEN

    @property
    def effective_ollama(self) -> str:
        if self.OLLAMA_ENDPOINT:
            return str(self.OLLAMA_ENDPOINT).rstrip("/")
        return (self.OLLAMA_BASE_URL or "http://localhost:11434").rstrip("/")

    @property
    def effective_jwt_secret(self) -> str:
        if self.JWT_SECRET:
            return self.JWT_SECRET.get_secret_value()
        return ""

    @property
    def effective_radarr_url(self) -> str:
        return self.RADARR_URL.rstrip("/") if self.RADARR_URL else ""

    @property
    def effective_sonarr_url(self) -> str:
        return self.SONARR_URL.rstrip("/") if self.SONARR_URL else ""

    @property
    def effective_lidarr_url(self) -> str:
        return self.LIDARR_URL.rstrip("/") if self.LIDARR_URL else ""

    @property
    def is_configured(self) -> bool:
        """True when the minimum required credentials are present."""
        return bool(
            self.effective_plex_url
            and self.effective_plex_token
            and self.JWT_SECRET
        )


settings = Settings()
