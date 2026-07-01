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
    # Route deletion proposals through the 3-pillar LLM judge
    # (src/services/pillars.py) instead of the legacy taste-mismatch pitch.
    # Off by default — the legacy path stays the fallback until flipped.
    PILLARS_ENABLED: bool = False
    # Autonomous self-learning: capture generalizable curation principles from
    # the owner's DEBATES with the curator (shadow), novelty-check them, and
    # inject the ACTIVE ones into the judge. Off by default — a captured
    # principle stays 'shadow' (surfaced, not affecting judgments) until the
    # owner promotes it. See src/services/curator_principles.py.
    PRINCIPLES_ENABLED: bool = False
    # Once you trust the shadow captures, flip this on to let NEW / REFINEMENT
    # principles activate AUTOMATICALLY (a CONTRADICTION always waits for you).
    # Off by default — the supervised shadow rollout comes first.
    PRINCIPLE_AUTO_ACTIVATE: bool = False
    # Primary models — .env is the single source of truth.
    # Defaults here are only used when .env has no entry.
    BASE_CURATOR_MODEL: str = "qwen3.6:27b"        # chat + recommendations
    BASE_SUMMARIZER_MODEL: str = "granite4.1:8b"   # enrichment + memory extraction
    # ── Why granite4.1:8b again (after Phase A briefly used 3b)? ──────────
    # Phase A (2026-05-24 finale, single v5 prompt, 200 items × 4 models)
    # picked granite4.1:3b based on best curated pass-rate at the time.
    # Phase A3 (2026-05-24 three-round A/B/C bench, v5.1/v5.2/v5.3 × 3
    # models × 100 items, see tournament_round2_2026-05-24_21-20.md)
    # showed granite4.1:8b + v5.2 (with the new NO-FILL rule) is the more
    # stable production combination:
    #   - 98% pass-rate median across R1/R2/R3
    #   - 0% cast hallucinations
    #   - 2.1pp std-dev (lowest of all 9 combos)
    #   - 14s p50 (slower than 3b but acceptable)
    # The VRAM trade-off (~7GB vs 3GB for 3b) is handled by the existing
    # llm_priority.py eviction policy — when Curator becomes active the
    # Summarizer is evicted cleanly within ~1s. Defense layers (Idea-A
    # archetype blacklist + Idea-C number self-correction loop) stay.
    # Backup models — defined in .env for easy switching, not yet used in code.
    BACKUP_CURATOR_MODEL: str = "laguna-xs.2:q4_K_M"
    BACKUP_SUMMARIZER_MODEL: str = "granite4.1:3b"   # speed fallback / VRAM-constrained mode
    # When True, <think>...</think> blocks are stripped from LLM responses.
    # Set False — current models (granite4.1:8b, qwen3.6:27b) do not emit think blocks.
    # Keep this flag so switching to a reasoning model only requires an .env change.
    LLM_THINK_TAGS: bool = False
    # Kept for .env backward-compat; no longer used — enrichment uses a single LLM worker.
    ENRICH_PARALLEL_SLOTS: int = 0
    # Max concurrent big-model (curator) generations across the WHOLE server.
    # A single GPU can only run one without spilling to CPU, so the default 1
    # serializes chat + recommendations + proactive + verification: a second
    # user (or job) queues for the slot instead of dragging every response to
    # a crawl. Raise only if the host has VRAM for parallel big generations.
    MAX_CONCURRENT_CURATOR: int = 1

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
