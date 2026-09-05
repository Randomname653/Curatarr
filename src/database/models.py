"""
ARR Suite LLM - Database Models (Phase A)

SQLite database models for users, sessions, and encrypted user data.
"""

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint,
)
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
    # Bumped on logout and deactivation; JWTs carry it as "ver" and are
    # rejected on mismatch - the revocation a stateless token otherwise lacks.
    token_version = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)
    # The user's OWN plex.tv token, captured at PIN login. Powers per-account
    # Plex writes ("Curatarr Recommended" playlists are account-private — the
    # owner token cannot create them for other household members). Plaintext
    # by design: same trust level as PLEX_TOKEN in .env on this single-
    # household box, and UserResponse serializes explicit fields only, so
    # there is no API leak path. NULL = hasn't logged in since this column
    # landed → playlist push skips them until their next login (which also
    # self-heals revoked tokens, since every login re-stores the newest one).
    plex_token = Column(String(512), nullable=True)


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


# Pass 48: ``ChatInteraction`` was a write-only table — chat.py wrote a
# row per turn but nothing in src/ ever queried it. The redundant copy
# of (user message, assistant response) duplicated ``ConversationMessage``,
# and the ``feedback`` column belonged to a removed thumbs-up/down UI
# with no learning loop on the other end. Model class deleted; the
# physical SQLite table is left in place for old DBs so we don't risk a
# destructive migration on someone's running install. A follow-up
# migration can DROP TABLE chat_interactions; until then it just doesn't
# accumulate new rows.


class UserPinHash(Base):
    """User PIN hash for AES-256 encryption key derivation."""
    __tablename__ = "user_pin_hashes"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    pin_hash = Column(String(256), nullable=False)  # PBKDF2 hash
    salt = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
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
    # Music / Spotify
    spotify_uri = Column(String(100), nullable=True)   # full spotify:track:xxx URI
    source      = Column(String(16),  nullable=True)   # "plex" | "spotify" | "manual"
    # Pass 16f: pre-resolved MusicBrainz artist ID — populated by Phase 1.4
    # (resolve_artist_mbids) so the Spotify-Backlog → Lidarr add flow has
    # the MBID immediately, no live lookup at click time.
    artist_mbid = Column(String(40), nullable=True, index=True)

    __table_args__ = (
        Index("idx_wh_user_item", "user_id", "plex_item_id"),
    )


class TasteVectorEntry(Base):
    """Computed taste vector per user, refreshed after each Plex sync.

    COLUMN NAMES LIE (eval 1.11) — they predate the per-category rework and
    a SQLite rename means a copy-migration, so they are documented instead:
      genre_affinity    = the FULL per-category summary dict
                          {cat: {genre_affinity, themes, moods, top_titles,
                           watch_count, avg_completion, temporal, …}}
      actor_affinity    = per-category THEME lists (never actors)
      director_affinity = per-category MOOD lists (never directors)
    Writer: taste_engine.compute_all_taste_vectors; readers:
    recommendations_engine (type_data) + chat context builders."""
    __tablename__ = "taste_vectors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    genre_affinity = Column(Text, nullable=True)        # JSON — see docstring: full summary dict
    actor_affinity = Column(Text, nullable=True)        # JSON — see docstring: themes
    director_affinity = Column(Text, nullable=True)     # JSON — see docstring: moods
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
    # A bubble the user ignores is not a message, it is a nag. Unclicked
    # messages expire (TTL set at creation) and every surfacing counts —
    # past the impression cap the queue moves on without them.
    expires_at = Column(DateTime, nullable=True)
    impressions = Column(Integer, default=0)


class ChatStarter(Base):
    """Pooled conversation starters for the chat landing view.

    Generated in background batches (custodian task, LLM-phrased from
    deterministic watch-history facts), then consumed like the proactive
    pool: rotation per page load, retired on click, on TTL expiry, or
    after sitting unclicked through too many impressions. ``form`` and
    ``daypart`` exist so selection can enforce variety and fit the hour —
    the anti-sameness rules live in code, not in the prompt.
    """
    __tablename__ = "chat_starters"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    text = Column(String(300), nullable=False)
    form = Column(String(24), nullable=True)     # question|observation|challenge|callback|tonight_pick
    daypart = Column(String(12), nullable=True)  # morning|day|evening|night|any
    fact_used = Column(String(200), nullable=True)
    # The title the starter is ABOUT, when it is about one — clicking the
    # chip opens a discussion anchored on it (verified data, watch status),
    # the same way a proposal click anchors on its title.
    anchor_title = Column(String(512), nullable=True)
    anchor_media_type = Column(String(32), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    impressions = Column(Integer, default=0)
    used_at = Column(DateTime, nullable=True)


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
    category  = Column(String(32), nullable=True)         # movie/show/anime/music
    poster_url = Column(String(512), nullable=True)
    synopsis   = Column(Text, nullable=True)
    genres     = Column(String(300), nullable=True)
    # Resolving IDs captured from the ARR item at proposal-generation time, so
    # the DISCUSSION path can pass them to ensure_verified_data and on-demand
    # fast-enrich + cache an un-enriched title (themes / year / significance)
    # instead of cold-reading a thin synopsis. Without these the discussion only
    # had the Sonarr/Radarr id, which can't resolve a show by title alone (the
    # Fringe case — confident confabulation on zero verified data).
    tvdb_id = Column(Integer, nullable=True)
    tmdb_id = Column(Integer, nullable=True)
    # Pass 17: most recent file-level activity timestamp (latest episode
    # file imported / movie file added / track file added). Distinct from
    # ``created_at`` (when this proposal row was generated) and from
    # series.added (when Sonarr added the series). Powers the "🆕 Just-
    # arrived" filter — items that came alive recently regardless of
    # when the parent entity was first added to the arr.
    latest_activity_at = Column(DateTime, nullable=True)
    # RESONANCE 4-pillar judge: a "merely fine" title is verdict STAGNANT —
    # surfaced as a proposal but flagged in the UI as a soft "your call" review,
    # not a hard cut. Legacy taste-mismatch proposals are never stagnant.
    stagnant = Column(Boolean, default=False)

    __table_args__ = (
        Index("idx_dp_user_status", "user_id", "status"),
        Index("idx_dp_latest_activity", "latest_activity_at"),
    )


class ProtectedMedia(Base):
    """Permanent whitelist for media that should never be proposed for deletion."""
    __tablename__ = "protected_media"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    # Stores either the external ID (TMDB / AniList) or the exact title.
    identifier = Column(String(255), nullable=False, index=True)
    category = Column(String(32), nullable=True)  # movie, show, anime
    reason = Column(Text, nullable=True)          # manual: short note; judge: pillar reasoning (Begründung)
    # Pillar judge: this table now also holds AUTOMATIC protections the 4-pillar
    # curation judge grants, not just the manual whitelist. ``source`` tells them
    # apart; ``verdict`` records which call protected it (KEEP_WITH_FLAG items also
    # feed the separate "downscale to reclaim space" list).
    source = Column(String(16), default="manual")  # "manual" (user whitelist) | "judge" (auto)
    verdict = Column(String(20), nullable=True)     # judge: HARD_KEEP | KEEP_WITH_FLAG
    title = Column(String(512), nullable=True)      # human-readable name for the admin debug view
    arr_url = Column(String(512), nullable=True)    # jump link into Radarr/Sonarr/Lidarr (downscale work list)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_protected_lookup", "user_id", "identifier"),
    )


class MediaMatchOverride(Base):
    """Owner-pinned entity resolution for ONE arr item — durable across
    rescans and re-enrichments (ported from SoulSync's match-override layer,
    MIT). The live failure class: Radarr held BOTH 2025/26 "Good Boy" films
    and Batman Beyond's profile was confabulated from a mis-attributed
    source — automatic resolution guards can shrink that class, only an
    owner pin can CLOSE a case. Read at the very start of
    fetch_and_prepare_raw: pinned external ids override whatever the arr
    item or a title search would have produced, before any fuzzy logic.
    Applying an override purges the item's cached raw/enriched rows and
    flips its enrichment status, so the pipeline rebuilds on the pin."""
    __tablename__ = "media_match_overrides"

    id = Column(Integer, primary_key=True, autoincrement=True)
    service = Column(String(32), nullable=False)    # radarr | sonarr | lidarr
    arr_id = Column(Integer, nullable=False)
    category = Column(String(32), nullable=True)    # movie | show | anime | music
    title = Column(String(512), nullable=True)      # human-readable, for the admin view
    # The pinned ids — only the set ones override; NULL = leave resolution alone.
    tmdb_id = Column(Integer, nullable=True)
    tvdb_id = Column(Integer, nullable=True)
    anilist_id = Column(Integer, nullable=True)
    mal_id = Column(Integer, nullable=True)
    imdb_id = Column(String(20), nullable=True)
    mbid = Column(String(64), nullable=True)        # MusicBrainz artist id
    note = Column(Text, nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("service", "arr_id", name="uq_match_override_item"),
    )


class CuratorPrinciple(Base):
    """A generalizable curation PRINCIPLE the curator learned from a debate with
    the owner — the autonomous self-learning layer.

    Distinct from EpisodicMemory (title-scoped, decays) and ProtectedMedia (a
    per-title whitelist): a principle is a lasting, title-agnostic RULE the owner
    established or endorsed in conversation, injected into the judge's
    constitution so the curator's taste sharpens over time instead of staying
    sharp-but-amnesiac (brilliant per-chat, forgetful across threads).

    Lifecycle:
      status 'shadow'   — captured + surfaced to the owner, NOT yet affecting
                          judgments (the trust-building rollout).
      status 'active'   — promoted; injected into the judge + chat framework.
      status 'rejected' — the owner dismissed it.
    novelty 'contradiction' is the one human touch-point: it conflicts with an
    existing active principle, so it always waits for the owner regardless of
    rollout state (the self-correction / anti-gaming guard).
    """
    __tablename__ = "curator_principles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    text = Column(Text, nullable=False)               # the principle — ONE sentence
    basis = Column(String(32), nullable=True)         # user_established / user_endorsed_curator / converged
    category = Column(String(32), nullable=True)      # movie/show/anime/music, or NULL = global
    status = Column(String(16), default="shadow")     # shadow / active / rejected
    novelty = Column(String(16), nullable=True)       # new / refinement / contradiction
    related = Column(Text, nullable=True)             # the existing rule it refines / contradicts
    origin_thread_id = Column(String(128), nullable=True)
    origin_summary = Column(Text, nullable=True)      # short note on the debate it came from
    embedding_json = Column(Text, nullable=True)      # for novelty-check + per-title retrieval
    times_reinforced = Column(Integer, default=0)     # recurrence = confidence booster, not a gate
    created_at = Column(DateTime, default=datetime.utcnow)
    activated_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_cp_user_status", "user_id", "status"),
    )


class CuratorResolutionLog(Base):
    """Append-only history of how each keep/delete debate resolved.

    Pass 66. Distinct from ``ProtectedMedia`` (the current whitelist
    STATE): this is the HISTORY of every settled deletion debate — the
    data foundation for the year-in-review recap.

    One row per resolved title:

      outcome          "kept" | "deleted"
      resolution_type  "consensus" — the two sides ended up agreeing: the
                         user convinced the curator, or the curator
                         convinced the user. A genuine meeting of minds.
                       "override"  — the title was kept OVER the curator's
                         standing objection. The curator never conceded the
                         title has merit; it only accepted that the title
                         stays. The user overruled it.
      curator_stance   the curator's FINAL take, short ("disposable
                       franchise noise"). On an override this is the
                       objection it never dropped; on a consensus it's
                       where it actually landed.
      override_reason  CATEGORY of why the user overrode ("Sentimental/
                       Partner", "Completionism", "Nostalgia", …) — NULL on
                       consensus.

    The recap is then a single ``SELECT … GROUP BY`` over this table.
    Nothing migrates ``ProtectedMedia`` — state and history stay separate.
    """
    __tablename__ = "curator_resolution_log"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title           = Column(String(512), nullable=False)
    category        = Column(String(32), nullable=True)   # movie/show/anime/music
    outcome         = Column(String(16), nullable=False)  # kept / deleted
    resolution_type = Column(String(16), nullable=False)  # consensus / override
    curator_stance  = Column(Text, nullable=True)
    override_reason = Column(String(64), nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("idx_crl_user_created", "user_id", "created_at"),
        Index("idx_crl_user_title", "user_id", "title"),
    )


class PlexRating(Base):
    """User-set Plex star ratings (1-5 ★), captured from PlexAmp / Plex Web.

    Pass 82. Scoped to MUSIC ONLY by design: in this user's setup the
    ``userRating`` field on movies/shows is overwritten by Kometa with
    aggregated platform ratings (TMDB / IMDb averages), so using it as
    "personal opinion" would be misleading. Music ratings come directly
    from the user via PlexAmp and are the real signal.

    Rating scale: Plex stores ratings on a 0-10 float scale where each
    UI star = 2.0 points (5★ = 10.0, 4.5★ = 9.0, … 1★ = 2.0, 0.5★ = 1.0).
    We store the raw Plex value to preserve half-star precision; the
    analysis layer converts to 1-5 stars for thresholds.

    One row per ``(user_id, plex_item_id)`` — upserted on every sync.
    Ratings can sit on tracks (type 10), albums (type 9), or artists
    (type 8); the ``media_type`` column distinguishes. ``artist_name``
    is captured on every row regardless of level so the deletion-scoring
    "max rating across this artist's content" lookup is a single index
    scan instead of a join through Plex's grandparent/parent hierarchy.
    """
    __tablename__ = "plex_ratings"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    plex_item_id  = Column(String(64), nullable=False, index=True)
    media_type    = Column(String(16), nullable=False)   # "track" | "album" | "artist"
    rating        = Column(Float, nullable=False)         # 0-10 Plex scale (5★ = 10)
    rated_at      = Column(DateTime, nullable=True)       # from Plex ``lastRatedAt``

    # Denormalised artist name for fast "max rating per artist" lookups.
    # Always lowercase-comparable; we store as-typed and use func.lower at
    # query time for case-insensitive matches against Lidarr artistName.
    artist_name   = Column(String(512), nullable=True, index=True)

    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "plex_item_id", name="uq_plex_rating_user_item"),
        Index("idx_plex_rating_artist_lookup", "user_id", "artist_name"),
    )


class EnrichmentStatus(Base):
    """Tracks enrichment progress per media item.

    Phase-2 additions (``fetch_tier`` / ``sources_state`` / ``provisional``,
    Pass 99-fu13) record WHICH external sources were consulted for this
    item and whether the result is the canonical "full" enrichment or a
    provisional "fast" pass that the source-upgrade scheduler should
    later promote. All three columns are nullable / default-false on
    legacy rows; readers must treat NULL ``fetch_tier`` as "full" for
    back-compat with pre-fu13 rows.
    """
    __tablename__ = "enrichment_status"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plex_rating_key = Column(String(64), unique=True, nullable=False)
    title = Column(String(512), nullable=False)
    media_category = Column(String(32), nullable=False)
    enriched = Column(Boolean, default=False)
    vector_ready = Column(Boolean, default=False)
    enriched_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)

    # Phase 2 (#37): per-item enrichment-source tracking + fast/full tier.
    # Written by the producer (#38), read by the source-upgrade scheduler
    # (#41) and the breakdown UI (#40). See module docstring above for
    # back-compat semantics on NULL ``fetch_tier``.
    fetch_tier    = Column(String(16), nullable=True)   # "fast" | "full" | NULL(=full, legacy)
    sources_state = Column(Text,       nullable=True)   # JSON: {"tmdb":{"status":"ok","at":"…"},…}
    provisional   = Column(Boolean,    default=False)   # True while a "fast" row is still upgradable


class AppState(Base):
    """Persistent app-wide state — replaces reading .env for runtime checks."""
    __tablename__ = "app_state"

    key = Column(String(128), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ConversationMessage(Base):
    """Persisted chat history per user for LLM context window.

    ``thread_id`` isolates discussions: free chat lives on ``general`` (or NULL
    for legacy rows that were written before Pass 3.5), each deletion-proposal
    discussion on ``deletion_proposal:{id}``, each proactive-message discussion
    on ``proactive_message:{id}``. ``_load_conversation`` filters by thread so
    one topic's history can't bleed into another.
    """
    __tablename__ = "conversation_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String(16), nullable=False)   # user / assistant / system
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    tokens_approx = Column(Integer, default=0)   # rough estimate for window management
    thread_id = Column(String(64), nullable=True, index=True)

    __table_args__ = (
        Index("idx_conv_user_thread", "user_id", "thread_id", "created_at"),
    )


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
    # Which lane this rec belongs to: "library" (owned but unwatched — watch
    # from your shelf) or "discovery" (not owned, taste-fit — worth acquiring).
    # Old rows predate lanes and were all taste-discovery → default "discovery".
    lane        = Column(String(20), default="discovery")
    title       = Column(String(500), nullable=False)
    reason      = Column(Text)
    confidence  = Column(Float, default=0.7)
    genres      = Column(String(200))
    poster_url  = Column(String(500))
    synopsis    = Column(Text, nullable=True)
    cached_at   = Column(DateTime, default=datetime.utcnow)
    # Resolving ids for the "Curatarr Recommended" Plex playlists + the
    # watched-a-rec follow-up. LIBRARY-lane rows get them from the arr
    # candidate pool + tech-profile resolution at cache time; DISCOVERY-lane
    # rows stay NULL (LLM-suggested titles own nothing yet). plex_rating_key
    # is the REAL Plex ratingKey (movie item / series), unlike the synthetic
    # "radarr:{id}" doc-keys used elsewhere.
    tmdb_id         = Column(Integer, nullable=True)
    tvdb_id         = Column(Integer, nullable=True)
    year            = Column(Integer, nullable=True)
    plex_rating_key = Column(String(64), nullable=True)

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


class GameProcess(Base):
    """
    User-classified processes: is_game=True → pause LLM during enrichment.
    is_game=False → ignore (never shown again in the classify prompt).
    """
    __tablename__ = "game_processes"

    process_name = Column(String(200), primary_key=True)
    is_game      = Column(Boolean, nullable=False)
    added_at     = Column(DateTime, default=datetime.utcnow)


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


class MediaTechProfile(Base):
    """Per-item technical profile collected from Plex (Media/Part/Stream):
    resolution, codec, HDR, bitrate, total size + runtime. Drives the
    MB-per-minute size norms + outlier detection so the curator flags genuine
    bloat, not blanket file size. One canonical row per library item; for series
    the episode files are aggregated (size + runtime summed, incl. specials),
    for music the artist's tracks are aggregated."""
    __tablename__ = "media_tech_profiles"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    plex_rating_key = Column(String(64), unique=True, nullable=False, index=True)
    media_type      = Column(String(20), nullable=False)   # movie / show / anime / music
    title           = Column(String(512), nullable=True)
    tmdb_id         = Column(Integer, nullable=True, index=True)  # for ARR-side lookup
    tvdb_id         = Column(Integer, nullable=True, index=True)
    size_mb         = Column(Float, default=0.0)           # total (series = sum of episode files)
    duration_min    = Column(Float, default=0.0)           # total runtime incl. specials
    mb_per_min      = Column(Float, nullable=True)         # size_mb / duration_min — the norm key
    resolution      = Column(String(16), nullable=True)    # 4k / 1080 / 720 / sd
    codec           = Column(String(16), nullable=True)    # hevc / h264 / av1 / …
    hdr             = Column(Boolean, default=False)
    audio_langs     = Column(String(200), nullable=True)   # comma-separated
    sub_langs       = Column(String(200), nullable=True)
    item_count      = Column(Integer, default=1)           # episodes / tracks aggregated
    # Redundant-version (hoarding) signal: how many quality versions of THIS title
    # the library keeps (4K Remux + 1080p Bluray = 2), and the disk those extra
    # copies waste. mb_per_min is computed from the PRIMARY (largest) version so a
    # duplicate doesn't masquerade as bitrate bloat.
    versions        = Column(Integer, default=1)
    redundant_mb    = Column(Float, default=0.0)
    # Remux = untouched disc stream — its bitrate is legitimately several times
    # an encode's, so remuxes form their OWN size class (movie|1080|h264|remux)
    # instead of reading as "3x bloated" against the encode median. Detected
    # from the file path ("remux" in the primary part's name); for series it's
    # majority-of-episodes.
    is_remux        = Column(Boolean, default=False)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MediaSubtitleProfile(Base):
    """Execution signals read off a title's subtitle track — the first data in
    this system that touches the WORK rather than metadata about it.

    Deliberately a sibling of MediaTechProfile rather than columns on it: the
    tech sync is a 12h full-library bulk scrape that rewrites every field it
    knows, while these numbers are fetched per candidate, may legitimately be
    absent, and need their own "we looked and found nothing" stamp. Bolting
    them onto that table would leave the next author one omission away from
    silently clobbering them.

    ``checked`` + ``metrics_v`` carry the tri-state the enrichment layer uses
    everywhere: a row with checked=True and no numbers means "definitively no
    usable track" (forced-only, none on file); a transient failure writes no
    row at all so the next pass retries.
    """
    __tablename__ = "media_subtitle_profiles"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    plex_rating_key = Column(String(64), unique=True, nullable=False, index=True)
    media_type      = Column(String(20), nullable=False)
    title           = Column(String(512), nullable=True)
    tmdb_id         = Column(Integer, nullable=True, index=True)
    tvdb_id         = Column(Integer, nullable=True, index=True)
    # Which track was measured, and what kind it was.
    source          = Column(String(24), nullable=True)   # plex_sidecar | opensubtitles
    language        = Column(String(16), nullable=True)   # the track's own tag
    is_sdh          = Column(Boolean, default=False)
    # The numbers themselves.
    words_per_min   = Column(Float, nullable=True)
    silent_min      = Column(Float, nullable=True)
    silent_share    = Column(Float, nullable=True)
    mattr           = Column(Float, nullable=True)        # NOT raw TTR (length-biased)
    total_words     = Column(Integer, nullable=True)
    coverage        = Column(Float, nullable=True)        # cue span / runtime
    # The runtime these figures were computed against. For a series that is ONE
    # episode, never the season total — MediaTechProfile aggregates every
    # episode, and rendering "2 of 115 min without dialogue" from an episode's
    # silence and a season's length reads as nonsense.
    duration_min    = Column(Float, nullable=True)
    checked         = Column(Boolean, default=False)
    metrics_v       = Column(String(16), nullable=True)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MediaSizeNorm(Base):
    """Learned MB-per-minute distribution for a media class, recomputed after each
    tech sync. ``class_key`` encodes the grouping granularity with ``*`` wildcards
    (e.g. ``movie|4k|hevc|hdr`` … ``movie|4k|*|*`` … ``movie|*|*|*``) so the outlier
    detector can try the finest class that has enough samples, then fall back."""
    __tablename__ = "media_size_norms"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    class_key    = Column(String(80), unique=True, nullable=False, index=True)
    media_type   = Column(String(20), nullable=False)
    median       = Column(Float, nullable=False)          # mb_per_min p50
    p25          = Column(Float, nullable=True)
    p75          = Column(Float, nullable=True)
    p90          = Column(Float, nullable=True)
    std          = Column(Float, nullable=True)
    sample_count = Column(Integer, default=0)
    computed_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
