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
    # These are the baked modelfile names created by setup wizard
    CURATOR_MODEL: str = "curatarr-curator"       # large model, system prompt baked in
    SUMMARIZER_MODEL: str = "curatarr-summarizer"  # small model, system prompt baked in
    EMBEDDING_MODEL: str = "nomic-embed-text"
    # Base models (used to build the above)
    BASE_CURATOR_MODEL: str = "qwen2.5:32b"
    BASE_SUMMARIZER_MODEL: str = "dolphin3"  # Llama 3.1 8B — better cultural knowledge than qwen2.5:3b

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
    LASTFM_API_KEY: Optional[str] = None   # music tags + similar artists (optional)
    # AniList: no key needed (public GraphQL)
    # MusicBrainz: no key needed

    # ── Sync ─────────────────────────────────────────────────────────────────
    SYNC_ON_STARTUP: bool = True
    SYNC_INTERVAL_HOURS: int = 24

    # ── Binge detection ──────────────────────────────────────────────────────
    BINGE_EPISODE_THRESHOLD: int = 3      # episodes in one session = binge
    BINGE_SESSION_HOURS: int = 6          # session window
    BINGE_SERIES_PERCENT: float = 0.5     # >50% of season in 48h = binge

    # ── CORS ─────────────────────────────────────────────────────────────────
    # Override via .env as JSON: CORS_ORIGINS=["http://myserver:8000"]
    CORS_ORIGINS: list = ["http://localhost:8000", "http://127.0.0.1:8000"]

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
