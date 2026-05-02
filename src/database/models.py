"""
ARR Suite LLM - Database Models (Phase A)

SQLite database models for users, sessions, and encrypted user data.
"""

from sqlalchemy import Column, String, Integer, Boolean, DateTime, Text, JSON, Float, ForeignKey, Index
from sqlalchemy.orm import declarative_base
from datetime import datetime

Base = declarative_base()


class User(Base):
    """User model with Plex OAuth integration."""
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    plex_user_id = Column(String(64), unique=True, nullable=False, index=True)
    plex_username = Column(String(256), nullable=False)
    is_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)


class Session(Base):
    """User session management."""
    __tablename__ = "sessions"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    access_token = Column(String(512), unique=True, nullable=False, index=True)
    refresh_token = Column(String(512), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    ip_address = Column(String(45), nullable=True)  # IPv6 capable
    user_agent = Column(String(512), nullable=True)
    
    # Index for quick lookups
    __table_args__ = (
        Index("idx_user_expires", "user_id", "expires_at"),
    )


class MediaItem(Base):
    """Media knowledge base with vector embeddings."""
    __tablename__ = "media_items"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    tmdb_id = Column(Integer, unique=True, nullable=True, index=True)
    tvdb_id = Column(Integer, unique=True, nullable=True, index=True)
    anilist_id = Column(Integer, unique=True, nullable=True, index=True)
    media_type = Column(String(32), nullable=False)  # movie, episode, anime
    title = Column(String(512), nullable=False)
    original_title = Column(String(512), nullable=True)
    year = Column(Integer, nullable=True)
    genres = Column(JSON, nullable=True)  # Encrypted if sensitive
    overview = Column(Text, nullable=True)
    rating = Column(Float, nullable=True)
    vote_count = Column(Integer, nullable=True)
    
    # Metadata hash for deduplication
    metadata_hash = Column(String(64), unique=True, nullable=True, index=True)

    # Enrichment status
    enriched = Column(Boolean, default=False)
    enrichment_date = Column(DateTime, nullable=True)
    enrichment_source = Column(String(32), nullable=True)  # tmdb, anidb, omdb
    
    # Metadata as JSON (encrypted at rest)
    metadata_json = Column(Text, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChatInteraction(Base):
    """User chat interactions with curator LLM."""
    __tablename__ = "chat_interactions"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    message = Column(Text, nullable=False)
    response = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    feedback = Column(Integer, default=0)  # -1 (thumbs down), 0 (no feedback), 1 (thumbs up)
    
    # Context for RAG
    context_media_ids = Column(String, nullable=True)  # Comma-separated media IDs
    user_taste_vector_hash = Column(String(64), nullable=True)
    
    # Index for quick lookups
    __table_args__ = (
        Index("idx_user_timestamp", "user_id", "timestamp"),
    )


class UserPinHash(Base):
    """User PIN hash for AES-256 encryption key derivation."""
    __tablename__ = "user_pin_hashes"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    pin_hash = Column(String(256), nullable=False)  # PBKDF2 hash
    salt = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BatchJob(Base):
    """Batch processing jobs (media enrichment, etc.)."""
    __tablename__ = "batch_jobs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    job_type = Column(String(64), nullable=False)  # media_enrichment, taste_vector_generation
    status = Column(String(32), default="pending")  # pending, running, completed, failed
    progress = Column(Integer, default=0)  # Percentage 0-100
    total_items = Column(Integer, nullable=True)
    processed_items = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WatchHistoryEntry(Base):
    """Single playback event from Plex, per user."""
    __tablename__ = "watch_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    plex_user_id = Column(String(64), nullable=False)
    plex_item_id = Column(String(64), nullable=False)
    title = Column(String(512), nullable=False)
    media_type = Column(String(32), nullable=True)
    series_title = Column(String(512), nullable=True)
    season = Column(Integer, nullable=True)
    episode = Column(Integer, nullable=True)
    viewed_at = Column(DateTime, nullable=False, index=True)
    duration_ms = Column(Integer, nullable=True)
    view_offset_ms = Column(Integer, nullable=True)
    completed = Column(Boolean, default=False)
    genres = Column(Text, nullable=True)
    tmdb_id = Column(Integer, nullable=True)

    __table_args__ = (
        Index("idx_wh_user_item", "user_id", "plex_item_id"),
    )


class TasteVectorEntry(Base):
    """Computed taste vector per user, refreshed after each Plex sync."""
    __tablename__ = "taste_vectors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    genre_affinity = Column(Text, nullable=True)        # JSON {genre: score}
    actor_affinity = Column(Text, nullable=True)        # JSON
    director_affinity = Column(Text, nullable=True)     # JSON
    top_titles = Column(Text, nullable=True)            # JSON list
    watch_count = Column(Integer, default=0)
    avg_completion = Column(Float, default=0.0)
    computed_at = Column(DateTime, default=datetime.utcnow)
    summary_text = Column(Text, nullable=True)          # LLM plain-text taste summary


class LibraryConfig(Base):
    """User-configured mapping of Plex library sections to media types."""
    __tablename__ = "library_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plex_section_key = Column(String(16), nullable=False, unique=True)
    plex_section_title = Column(String(256), nullable=False)
    plex_section_type = Column(String(32), nullable=False)   # artist/show/movie
    media_category = Column(String(32), nullable=False)      # music/anime/show/movie
    configured_at = Column(DateTime, default=datetime.utcnow)
    configured_by = Column(Integer, ForeignKey("users.id"), nullable=True)


class ProactiveMessage(Base):
    """Messages generated by the curator without user prompting."""
    __tablename__ = "proactive_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    trigger_type = Column(String(64), nullable=False)
    trigger_data = Column(Text, nullable=True)   # JSON
    message = Column(Text, nullable=False)
    read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class EncryptedTasteVector(Base):
    """AES-256-GCM encrypted taste vector per user per media category."""
    __tablename__ = "encrypted_taste_vectors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    media_category = Column(String(32), nullable=False)   # music/movie/show/anime
    salt = Column(String(64), nullable=False)             # hex, 32 bytes
    encrypted_blob = Column(Text, nullable=False)         # JSON {ciphertext,nonce,tag,version}
    computed_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    watch_count = Column(Integer, default=0)
    summary_text = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_etv_user_cat", "user_id", "media_category", unique=True),
    )


class DeletionProposal(Base):
    """Deletion candidate with user feedback."""
    __tablename__ = "deletion_proposals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    media_id = Column(String(64), nullable=False)
    title = Column(String(512), nullable=False)
    service = Column(String(32), nullable=False)          # radarr/sonarr/lidarr
    arr_url = Column(String(512), nullable=True)          # direct link to ARR entry
    reason = Column(Text, nullable=True)
    confidence = Column(Float, default=0.5)
    storage_mb = Column(Float, default=0.0)
    status = Column(String(32), default="pending")        # pending/approved/rejected/deleted
    user_comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_dp_user_status", "user_id", "status"),
    )


class ProtectedMedia(Base):
    """Permanent whitelist for media that should never be proposed for deletion."""
    __tablename__ = "protected_media"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    # Wir speichern die ID (TMDB/AniList) oder den exakten Titel
    identifier = Column(String(255), nullable=False, index=True) 
    category = Column(String(32), nullable=True) # movie, show, anime
    reason = Column(Text, nullable=True)         # "Mitbewohner", "Sammlerstück"
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_protected_lookup", "user_id", "identifier"),
    )


class EnrichmentStatus(Base):
    """Tracks enrichment progress per media item."""
    __tablename__ = "enrichment_status"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plex_rating_key = Column(String(64), unique=True, nullable=False)
    title = Column(String(512), nullable=False)
    media_category = Column(String(32), nullable=False)
    enriched = Column(Boolean, default=False)
    vector_ready = Column(Boolean, default=False)
    enriched_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)


class AppState(Base):
    """Persistent app-wide state — replaces reading .env for runtime checks."""
    __tablename__ = "app_state"

    key = Column(String(128), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ConversationMessage(Base):
    """Persisted chat history per user for LLM context window."""
    __tablename__ = "conversation_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String(16), nullable=False)   # user / assistant / system
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    tokens_approx = Column(Integer, default=0)   # rough estimate for window management


class EpisodicMemory(Base):
    """
    Episodic memory store for the curator LLM.
    Each memory is a meaningful observation, statement, or pattern
    extracted from user interactions and behavior.
    """
    __tablename__ = "episodic_memories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    memory_type = Column(String(32), nullable=False)    # explicit_statement / feedback / etc
    content = Column(Text, nullable=False)              # human-readable memory
    metadata_json = Column(Text, nullable=True)         # {title, source, ...}
    media_category = Column(String(32), nullable=True)  # music/movie/show/anime or null
    importance = Column(Float, default=0.5)             # 0..1
    embedding_json = Column(Text, nullable=True)        # vector as JSON array
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    last_accessed = Column(DateTime, nullable=True)
    access_count = Column(Integer, default=0)

    __table_args__ = (
        Index("idx_em_user_type", "user_id", "memory_type"),
        Index("idx_em_user_cat", "user_id", "media_category"),
    )


class CachedRecommendation(Base):
    """Pre-generated recommendations, refreshed after each sync/enrichment."""
    __tablename__ = "cached_recommendations"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=False)
    category    = Column(String(20), nullable=False)
    title       = Column(String(500), nullable=False)
    reason      = Column(Text)
    confidence  = Column(Float, default=0.7)
    genres      = Column(String(200))
    poster_url  = Column(String(500))
    cached_at   = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_cached_recs_user_cat", "user_id", "category"),
    )


class MediaIdentity(Base):
    """
    Cross-reference table for all known IDs per media item.
    One row per Plex item. Populated during sync from Plex Guid tags.
    Used by enrichment to pick the right API and avoid title-search ambiguity.
    """
    __tablename__ = "media_identities"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    plex_rating_key = Column(String(50), unique=True, nullable=False, index=True)
    media_type      = Column(String(20), nullable=False)   # movie/show/anime/music
    title           = Column(String(500), nullable=False)
    year            = Column(Integer, nullable=True)

    # External IDs — all optional, populated from Plex Guid tags + agent lookups
    tmdb_id         = Column(Integer, nullable=True, index=True)
    tvdb_id         = Column(Integer, nullable=True, index=True)
    imdb_id         = Column(String(20), nullable=True)    # tt1234567
    anidb_id        = Column(Integer, nullable=True, index=True)
    anilist_id      = Column(Integer, nullable=True, index=True)
    mal_id          = Column(Integer, nullable=True)       # MyAnimeList
    musicbrainz_id  = Column(String(50), nullable=True)    # artist MBID

    # Canonical cache key (set once IDs are resolved)
    canonical_cache_key = Column(String(200), nullable=True)

    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("ix_media_id_tmdb",    "tmdb_id"),
        Index("ix_media_id_anilist", "anilist_id"),
        Index("ix_media_id_anidb",   "anidb_id"),
    )


class ArrEnrichmentStatus(Base):
    """
    Enrichment status for ARR library items (Radarr/Sonarr/Lidarr).
    These may not have a Plex rating key if never watched.
    Keyed by service + arr_id.
    """
    __tablename__ = "arr_enrichment_status"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    service      = Column(String(20), nullable=False)   # radarr / sonarr / lidarr
    arr_id       = Column(Integer, nullable=False)
    category     = Column(String(20), nullable=False)   # movie / show / anime / music
    title        = Column(String(500), nullable=False)
    tmdb_id      = Column(Integer, nullable=True)
    tvdb_id      = Column(Integer, nullable=True)
    enriched     = Column(Boolean, default=False)
    enriched_at  = Column(DateTime, nullable=True)
    error        = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_arr_enrich_service_id", "service", "arr_id", unique=True),
    )
