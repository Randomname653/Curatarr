"""Curatarr - Database Connection"""

from contextlib import contextmanager
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config import settings
from src.database.models import Base

# Check if we're using in-memory SQLite and adjust pool settings accordingly
if "sqlite:///:memory:" in settings.DATABASE_URL:
    engine = create_engine(
        settings.DATABASE_URL,
        connect_args={
            "check_same_thread": False,
        },
        pool_pre_ping=True,
        poolclass=StaticPool,
    )
else:
    engine = create_engine(
        settings.DATABASE_URL,
        connect_args={
            "check_same_thread": False,
            "timeout": 60,  # SQLite connection-level timeout (seconds)
        } if "sqlite" in settings.DATABASE_URL else {},
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_timeout=60,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

if "sqlite" in settings.DATABASE_URL:
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.execute("PRAGMA wal_autocheckpoint=1000")
        cursor.execute("PRAGMA cache_size=-64000")
        cursor.close()

    @event.listens_for(engine, "checkout")
    def on_checkout(dbapi_connection, connection_record, connection_proxy):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.close()


@contextmanager
def get_db_session():
    """Context manager for use in services/background tasks."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db():
    """FastAPI dependency — yields a session and closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _migrate_columns() -> None:
    """Add columns introduced after initial schema creation (SQLite-safe)."""
    from sqlalchemy import text
    new_cols = [
        # Subtitle metrics: the episode runtime they were measured against
        # (MediaTechProfile holds the SERIES total, which is a different number)
        ("media_subtitle_profiles", "duration_min", "FLOAT"),
        # Starter anchors: which title a curator-opener is about
        ("chat_starters",         "anchor_title",      "TEXT"),
        ("chat_starters",         "anchor_media_type", "TEXT"),
        # Proactive-message TTL: ignored bubbles expire instead of nagging
        ("proactive_messages",    "expires_at",  "DATETIME"),
        ("proactive_messages",    "impressions", "INTEGER DEFAULT 0"),
        ("deletion_proposals",    "category",    "TEXT"),
        ("deletion_proposals",    "poster_url",  "TEXT"),
        ("deletion_proposals",    "synopsis",    "TEXT"),
        ("deletion_proposals",    "genres",      "TEXT"),
        ("cached_recommendations","synopsis",    "TEXT"),
        # Two-lane recommendations: "library" (owned, unwatched) vs "discovery"
        # (not owned, taste-fit). Existing rows backfill to "discovery".
        ("cached_recommendations","lane",        "VARCHAR(20) DEFAULT 'discovery'"),
        # Music / Spotify enrichment columns
        ("watch_history",         "spotify_uri", "TEXT"),
        ("watch_history",         "source",      "TEXT"),
        # Pass 3.5: per-discussion thread isolation for chat history
        ("conversation_messages", "thread_id",   "TEXT"),
        # Pass 16f: pre-resolved MusicBrainz artist ID for the Spotify-Backlog
        # → Lidarr-add flow. Indexed below in the same migration so lookups
        # by artist name stay fast.
        ("watch_history",         "artist_mbid", "TEXT"),
        # Pass 16n: column was added to the EnrichmentStatus model but the
        # auto-migration was missed. Fresh DBs get it from create_all,
        # but existing DBs were stuck running a one-off add_col.py
        # script until this entry. SQLite ALTER TABLE is idempotent
        # via the existing-column check below.
        ("enrichment_status",     "vector_ready", "BOOLEAN DEFAULT 0"),
        # Pass 17: file-level activity timestamp on each deletion proposal.
        # NULL on existing rows — backfilled at next proposal-generation.
        ("deletion_proposals",    "latest_activity_at", "DATETIME"),
        # Resolving IDs so the discussion path can on-demand fast-enrich an
        # un-enriched title instead of cold-reading (the Fringe case). NULL on
        # legacy rows → those fall through to the no-verified-data hedge; new
        # rows carry the id and self-heal on first discussion.
        ("deletion_proposals",    "tvdb_id", "INTEGER"),
        ("deletion_proposals",    "tmdb_id", "INTEGER"),
        # RESONANCE 4-pillar judge: STAGNANT proposals flagged as soft "your call".
        ("deletion_proposals",    "stagnant", "BOOLEAN DEFAULT 0"),
        # Redundant-version (duplicate) tracking on the tech profile.
        ("media_tech_profiles",   "versions", "INTEGER DEFAULT 1"),
        ("media_tech_profiles",   "redundant_mb", "FLOAT DEFAULT 0"),
        # Remux class: untouched disc streams get their own size norms
        # instead of reading as "3x bloated" against encode medians.
        ("media_tech_profiles",   "is_remux", "INTEGER DEFAULT 0"),
        # "Curatarr Recommended" Plex playlists: per-user plex token captured
        # at PIN login (account-private playlist writes), and resolving ids on
        # cached recommendations (library lane only; discovery stays NULL).
        ("users",                  "plex_token", "VARCHAR(512)"),
        ("cached_recommendations", "tmdb_id", "INTEGER"),
        ("cached_recommendations", "tvdb_id", "INTEGER"),
        ("cached_recommendations", "year", "INTEGER"),
        ("cached_recommendations", "plex_rating_key", "VARCHAR(64)"),
        # Pass 99-fu13 / Phase 2 #37: per-item enrichment-source tracking.
        # ``fetch_tier`` distinguishes a provisional fast-pass enrichment
        # ("fast" — only the cheap sources were consulted) from the
        # canonical full pass ("full"); NULL on legacy rows is treated as
        # "full" by the readers, so the migration is back-compat safe.
        # ``sources_state`` holds a JSON map of per-source status +
        # timestamps so the source-upgrade scheduler (#41) can find
        # items where a slow source ("mb", "tmdb") is still pending.
        # ``provisional`` is the denormalised "still upgradable" flag —
        # True while a fast row hasn't been promoted, flipped to False
        # once the slow source has been tried (whether it found data or
        # not). All three are nullable / default-false; #38 onwards
        # writes them.
        ("enrichment_status",     "fetch_tier",    "VARCHAR(16)"),
        ("enrichment_status",     "sources_state", "TEXT"),
        ("enrichment_status",     "provisional",   "BOOLEAN DEFAULT 0"),
        # Pillar judge: ProtectedMedia also stores AUTOMATIC protections the
        # 4-pillar judge grants. ``source`` splits "manual" (user whitelist)
        # from "judge" (auto); ``verdict`` records HARD_KEEP / KEEP_WITH_FLAG
        # (the latter also feeds the downscale list). Pillar reasoning reuses
        # the existing ``reason`` Text column. Legacy rows backfill to "manual".
        ("protected_media",       "source",  "VARCHAR(16) DEFAULT 'manual'"),
        ("protected_media",       "verdict", "VARCHAR(20)"),
        ("protected_media",       "title",   "VARCHAR(512)"),
        ("protected_media",       "arr_url", "VARCHAR(512)"),
    ]
    # Indexes that need to exist on top of the new columns. ALTER TABLE
    # ADD COLUMN doesn't pick up the ``index=True`` flag from the model
    # definition on existing DBs, so we create them explicitly. SQLite's
    # CREATE INDEX IF NOT EXISTS is idempotent — safe to re-run.
    new_indexes = [
        # Pass 16h: artist_mbid index needed for the Spotify-Backlog
        # ``only_resolved=True`` filter and Phase 1.4's ``is_(None)`` scan
        # over hundreds of thousands of plays.
        ("ix_watch_history_artist_mbid", "watch_history", "artist_mbid"),
        # Pass 17: index for the "🆕 recent activity" filter on deletion
        # proposals. Range query on a few hundred rows, but the index keeps
        # it cheap even when proposals are regenerated frequently.
        ("idx_dp_latest_activity",       "deletion_proposals", "latest_activity_at"),
        # Pass 99-fu13 / Phase 2 #37: keeps the source-upgrade scheduler
        # query (#41) cheap as the table grows past 50 k rows. Typical
        # query: ``WHERE fetch_tier = 'fast' AND provisional = 1``.
        ("idx_es_fetch_tier",            "enrichment_status", "fetch_tier"),
    ]

    import logging as _logging
    _mig_log = _logging.getLogger(__name__)
    with engine.connect() as conn:
        for table, col, typ in new_cols:
            try:
                existing = [row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))]
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {typ}"))
            except Exception as e:
                # Pass 43 (B3): the existing-column guard above means this
                # branch only fires on genuinely-unexpected failures
                # (locked DB, table missing). Log them so a stalled
                # migration is debuggable instead of silently swallowed.
                _mig_log.debug("[migrate] add column %s.%s failed: %s", table, col, e)
        for idx_name, table, col in new_indexes:
            try:
                conn.execute(text(
                    f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({col})"
                ))
            except Exception as e:
                _mig_log.debug("[migrate] create index %s on %s(%s) failed: %s",
                               idx_name, table, col, e)
        conn.commit()


def _migrate_deletion_proposals_autoincrement() -> None:
    """Pass 90c: convert ``deletion_proposals.id`` to AUTOINCREMENT.

    SQLAlchemy's ``Column(Integer, primary_key=True, autoincrement=True)``
    on SQLite generates plain ``INTEGER NOT NULL PRIMARY KEY`` — which is
    an alias for ROWID and FREES IDs on DELETE. The regenerate cycle
    (recommendations.py / scheduler.py) used to ``DELETE FROM
    deletion_proposals WHERE status='pending'`` then ``INSERT`` a fresh
    batch; SQLite happily reused the just-freed ROWIDs for the new rows,
    silently re-mapping stale frontend ``proposal_id`` values to entirely
    different titles in the new batch. Pass 90a (title verification) and
    Pass 90b (soft-delete instead of hard-delete) defend against this at
    the application layer; THIS migration removes the root cause by
    adding the ``AUTOINCREMENT`` keyword so IDs are strictly monotone.

    Idempotent: the CREATE TABLE statement in ``sqlite_master`` is
    inspected and migration is skipped when ``AUTOINCREMENT`` is already
    present. SQLite's standard rebuild-via-rename pattern: rename the
    existing table aside, create the new one with AUTOINCREMENT, copy
    the rows, drop the old, then seed ``sqlite_sequence`` so the next
    ID continues from the current max (without this seed, the first
    INSERT after migration would get a small number and conflict with
    existing higher ROWIDs).

    No FK references to ``deletion_proposals.id`` exist anywhere else
    in the schema (CuratorResolutionLog + ProtectedMedia store the
    title text directly, not the FK), so the rename-and-drop is safe.
    """
    from sqlalchemy import text
    import logging as _logging
    _mig_log = _logging.getLogger(__name__)

    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='deletion_proposals'"
        )).first()
        if not row:
            return  # fresh install — table will be created by create_all with the model's flag
        ddl = (row[0] or "").upper()
        if "AUTOINCREMENT" in ddl:
            return  # already migrated

        _mig_log.info("[migrate] Pass 90c: converting deletion_proposals.id → AUTOINCREMENT")
        try:
            # Wrap in IMMEDIATE so we hold the write lock for the duration —
            # otherwise a concurrent writer between RENAME and DROP could
            # see the half-migrated state.
            conn.execute(text("BEGIN IMMEDIATE"))
            conn.execute(text("ALTER TABLE deletion_proposals RENAME TO deletion_proposals_old_pre90c"))
            conn.execute(text("""
                CREATE TABLE deletion_proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    media_id VARCHAR(64) NOT NULL,
                    title VARCHAR(512) NOT NULL,
                    service VARCHAR(32) NOT NULL,
                    arr_url VARCHAR(512),
                    reason TEXT,
                    confidence FLOAT,
                    storage_mb FLOAT,
                    status VARCHAR(32),
                    user_comment TEXT,
                    created_at DATETIME,
                    resolved_at DATETIME,
                    category TEXT,
                    poster_url TEXT,
                    synopsis TEXT,
                    genres TEXT,
                    latest_activity_at DATETIME,
                    tvdb_id INTEGER,
                    tmdb_id INTEGER,
                    stagnant BOOLEAN,
                    FOREIGN KEY(user_id) REFERENCES users (id)
                )
            """))
            conn.execute(text("""
                INSERT INTO deletion_proposals
                    (id, user_id, media_id, title, service, arr_url, reason,
                     confidence, storage_mb, status, user_comment, created_at,
                     resolved_at, category, poster_url, synopsis, genres,
                     latest_activity_at, tvdb_id, tmdb_id, stagnant)
                SELECT id, user_id, media_id, title, service, arr_url, reason,
                       confidence, storage_mb, status, user_comment, created_at,
                       resolved_at, category, poster_url, synopsis, genres,
                       latest_activity_at, tvdb_id, tmdb_id, stagnant
                FROM deletion_proposals_old_pre90c
            """))
            # Seed sqlite_sequence so AUTOINCREMENT continues from current max.
            # Without this, the first INSERT after migration would get id=1
            # and collide with existing rows.
            conn.execute(text("""
                INSERT INTO sqlite_sequence (name, seq)
                VALUES ('deletion_proposals',
                        COALESCE((SELECT MAX(id) FROM deletion_proposals), 0))
            """))
            # Recreate the Pass-17 index on the new table (the old one moved
            # with the rename but only on the renamed-aside table).
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_dp_latest_activity "
                "ON deletion_proposals (latest_activity_at)"
            ))
            conn.execute(text("DROP TABLE deletion_proposals_old_pre90c"))
            conn.execute(text("COMMIT"))
            _mig_log.info("[migrate] Pass 90c: AUTOINCREMENT migration complete")
        except Exception as e:
            try:
                conn.execute(text("ROLLBACK"))
            except Exception:
                pass
            _mig_log.error(
                "[migrate] Pass 90c FAILED: %s — table left in pre-migration state. "
                "Manually rollback with: DROP TABLE deletion_proposals_old_pre90c "
                "(if it exists)", e,
            )


def _secure_db_files() -> None:
    """Ensure SQLite files in data/ are 0600 (POSIX only)."""
    import os
    import stat
    import logging as _logging
    from src.paths import DATA_DIR

    _mig_log = _logging.getLogger(__name__)
    if not DATA_DIR.exists():
        return

    for ext in ("*.db", "*.sqlite3", "*.sqlite3-wal", "*.sqlite3-shm", "*.db-wal", "*.db-shm"):
        for p in DATA_DIR.rglob(ext):
            try:
                os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
            except Exception as e:
                _mig_log.debug("Could not chmod %s (likely Windows): %s", p, e)


def init_db():
    """Create all tables and migrate any new columns."""
    Base.metadata.create_all(bind=engine)
    _migrate_columns()
    _migrate_deletion_proposals_autoincrement()
    _secure_db_files()
