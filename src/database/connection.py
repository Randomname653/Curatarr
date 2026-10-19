"""Curatarr - Database Connection"""

from contextlib import contextmanager
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config import settings
from src.database.models import Base

# How long a statement waits for another writer's lock before "database is
# locked". It was 60 s while coroutines ran SQLite on the event loop: a lock
# wait there froze the whole server, and a long timeout was the only way to
# ride out a writer in another process. With the loop kept free of database
# work (src/database/loop_guard.py, tests/test_loop_db_ratchet.py) a wait
# blocks only its worker thread, so the timeout can be what a write should
# actually tolerate.
BUSY_TIMEOUT_S = 15

# Check if we're using in-memory SQLite and adjust pool settings accordingly
if "sqlite:///:memory:" in settings.DATABASE_URL:
    engine = create_engine(
        settings.DATABASE_URL,
        connect_args={
            "check_same_thread": False,
        },
        poolclass=StaticPool,
    )
else:
    engine = create_engine(
        settings.DATABASE_URL,
        connect_args={
            "check_same_thread": False,
            "timeout": BUSY_TIMEOUT_S,  # SQLite connection-level timeout (seconds)
        } if "sqlite" in settings.DATABASE_URL else {},
        # A local file needs no liveness ping per checkout. Ten connections:
        # the worker threads that now carry every read, without a request
        # queueing behind a background job for a pooled connection; a
        # checkout that still waits 10 s points at a leak, not at load.
        pool_size=10,
        max_overflow=10,
        pool_timeout=10,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

from src.database import loop_guard  # noqa: E402
loop_guard.install(engine)


def _apply_sqlite_pragmas(dbapi_connection) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_S * 1000}")
    cursor.execute("PRAGMA wal_autocheckpoint=1000")
    cursor.execute("PRAGMA cache_size=-64000")
    cursor.execute("PRAGMA temp_store=MEMORY")
    cursor.close()


if "sqlite" in settings.DATABASE_URL:
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        _apply_sqlite_pragmas(dbapi_connection)


@contextmanager
def get_db_session():
    """Context manager for use in services/background tasks."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except BaseException:
        # BaseException: a cancelled task (CancelledError) rolls back here
        # too, instead of leaving it to the pool's reset-on-return.
        db.rollback()
        raise
    finally:
        db.close()


def get_db():
    """FastAPI dependency — yields a session and closes it after the request.
    Whatever the route left uncommitted is rolled back explicitly."""
    db = SessionLocal()
    try:
        yield db
    finally:
        try:
            if db.in_transaction():
                db.rollback()
        finally:
            db.close()


# ── Short write transactions ────────────────────────────────────────────────
#
# A separate small engine whose every transaction starts with BEGIN IMMEDIATE,
# so the write lock is taken up front and held only for the block's duration.
# The pysqlite driver runs in autocommit mode here (isolation_level=None): it
# never opens an implicit transaction of its own that a BEGIN could collide
# with ("cannot start a transaction within a transaction"); SQLAlchemy's
# "begin" event emits the only BEGIN. Scoped to this engine on purpose — on
# the main engine every read session, including a chat stream's for minutes,
# would hold the write lock.
_write_engine = None


def _build_write_engine(url):
    eng = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": BUSY_TIMEOUT_S},
        pool_size=2,
        max_overflow=2,
        pool_timeout=BUSY_TIMEOUT_S,
    )

    @event.listens_for(eng, "connect")
    def _connect(dbapi_connection, connection_record):
        dbapi_connection.isolation_level = None
        _apply_sqlite_pragmas(dbapi_connection)

    @event.listens_for(eng, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    loop_guard.install(eng)
    return eng


def get_write_engine():
    """The write engine for the CURRENT main engine's database (rebuilt if
    the main engine was swapped, as the tests do). An in-memory database
    cannot be shared with a second engine, so it writes through the main
    one."""
    global _write_engine
    if engine.url.get_backend_name() != "sqlite" or engine.url.database in (None, "", ":memory:"):
        return engine
    if _write_engine is None or _write_engine.url != engine.url:
        if _write_engine is not None:
            _write_engine.dispose()
        _write_engine = _build_write_engine(engine.url)
    return _write_engine


@contextmanager
def write_session():
    """A session for one short write transaction: BEGIN IMMEDIATE on entry,
    COMMIT on a clean exit, ROLLBACK on any exception (cancellation too).
    Never ``await`` inside the block — that would hold the write lock across
    the event loop. Run it on a worker thread (``src.database.runner``)."""
    db = SessionLocal(bind=get_write_engine())
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def checkpoint_and_dispose() -> None:
    """Close every pooled connection and fold the WAL into the main file, so
    data/ holds one self-consistent database (an empty -wal) when the app lets
    go of it — called last on shutdown, right before Syncthing is re-enabled
    for data/. A reader in another process can keep the checkpoint from
    finishing; that is logged, not raised."""
    import logging as _logging
    log = _logging.getLogger(__name__)
    global _write_engine
    if _write_engine is not None:
        _write_engine.dispose()
        _write_engine = None
    engine.dispose()
    if engine.url.get_backend_name() == "sqlite" and engine.url.database not in (None, "", ":memory:"):
        try:
            with engine.connect() as conn:
                busy, log_frames, done = conn.exec_driver_sql(
                    "PRAGMA wal_checkpoint(TRUNCATE)").one()
            if busy:
                log.warning("[shutdown] WAL checkpoint incomplete (%s/%s frames) — "
                            "another connection still holds the database", done, log_frames)
            else:
                log.info("[shutdown] WAL checkpointed and truncated")
        except Exception as e:  # noqa: BLE001 - shutdown must go on
            log.warning("[shutdown] WAL checkpoint failed: %s", e)
        finally:
            engine.dispose()


# Schema generation, stored in PRAGMA user_version by _migrate_columns.
# 1 = every column listed in _migrate_columns (2026-10). Bump it together with
# any new one-shot backfill and gate that backfill on the old value.
SCHEMA_VERSION = 1


def _migrate_columns() -> None:
    """Add columns introduced after initial schema creation (SQLite-safe)."""
    from sqlalchemy import text
    new_cols = [
        # Session revocation: JWT "ver" claim must match this per-user counter
        ("users",                 "token_version", "INTEGER DEFAULT 0"),
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
        # The score's terms behind the card's "Why?" (NULL on older rows).
        ("deletion_proposals",    "score_factors", "TEXT"),
        # SoulSync ban of a deleted artist, and deleted items back in the library.
        ("deletion_proposals",    "soulsync_ban", "TEXT"),
        ("deletion_proposals",    "returned_at", "DATETIME"),
        ("deletion_proposals",    "returned_info", "TEXT"),
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
        # Healing (2026-09): not-found backoff counter + match-quality record
        # (services/enrichment_state.py). Existing "Not found" rows are
        # backfilled below when the counter column is first added.
        ("enrichment_status",     "attempt_count",    "INTEGER DEFAULT 0"),
        ("enrichment_status",     "last_attempt_at",  "DATETIME"),
        ("enrichment_status",     "next_retry_at",    "DATETIME"),
        ("enrichment_status",     "match_basis",      "VARCHAR(16)"),
        ("enrichment_status",     "match_confidence", "FLOAT"),
        # Negative pin: candidates the owner excluded ("Not this one").
        ("media_match_overrides", "rejected_ids",     "TEXT"),
        # Series editions (2026-09-15): the file-source count and AniDB's
        # "disc still censored" verdict came a day after the table; rows
        # without files_disc are re-walked by editions_sync.
        ("music_lookup_misses",   "last_reason",         "VARCHAR(200)"),
        ("series_editions",       "files_disc",          "INTEGER"),
        ("series_editions",       "anidb_disc_censored", "BOOLEAN DEFAULT 0"),
        ("series_editions",       "anidb_tags",          "VARCHAR(200)"),
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
        # Healing: the Step-5 pre-filter and the KB "open" breakdown filter
        # on due-ness every run — same scale argument as fetch_tier.
        ("idx_es_next_retry_at",         "enrichment_status", "next_retry_at"),
    ]

    import logging as _logging
    _mig_log = _logging.getLogger(__name__)
    added: set = set()
    # ONE transaction for every step. pysqlite does not wrap DDL, so each
    # ALTER used to autocommit on its own: a crash between an ALTER and the
    # final commit kept the column but lost its one-shot backfill for good
    # (the next boot saw the column, never put it in ``added``, never
    # backfilled). SQLite DDL is transactional, so under BEGIN IMMEDIATE the
    # columns and their backfills land together or not at all. Driver-level
    # autocommit keeps pysqlite from opening a transaction of its own that
    # the explicit BEGIN would collide with.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            tables = {r[0] for r in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for table, col, typ in new_cols:
                if table not in tables:
                    # create_all builds every model table first, so this is a
                    # table the model no longer declares — nothing to add to.
                    _mig_log.debug("[migrate] %s.%s skipped: no table %s", table, col, table)
                    continue
                existing = [row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))]
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {typ}"))
                    added.add((table, col))
            for idx_name, table, col in new_indexes:
                if table in tables:
                    conn.execute(text(
                        f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({col})"
                    ))
            if ("enrichment_status", "attempt_count") in added:
                # Deploy-day backfill (once, when the counter is born): every
                # existing not-found row has in truth been retried every 3 days
                # for months. Marking them "tried 2+" (the counter starts here;
                # earlier attempts are uncounted) puts them into Needs attention
                # at once, and dating the next try from THEIR last write spreads
                # the retries by history instead of making ~800 rows fall due in
                # the same minute — which would trip the run's outage guard.
                conn.execute(text(
                    "UPDATE enrichment_status "
                    "SET attempt_count = 2, "
                    "    last_attempt_at = enriched_at, "
                    "    next_retry_at = datetime(enriched_at, '+3 days') "
                    "WHERE error LIKE 'Not found%' AND enriched_at IS NOT NULL"
                ))
            # Record the schema generation this boot brought the file to. A
            # future one-shot backfill keys on it (``if user_version < N``)
            # rather than on whether its column happened to be added just now.
            if (conn.exec_driver_sql("PRAGMA user_version").scalar() or 0) < SCHEMA_VERSION:
                conn.exec_driver_sql(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
            conn.exec_driver_sql("COMMIT")
        except BaseException as e:
            try:
                conn.exec_driver_sql("ROLLBACK")
            except Exception:  # noqa: BLE001 - rollback in failure path; the error is raised below
                pass
            # Was logged at DEBUG and swallowed: a column skipped because the
            # DB was locked (the music enricher writing) surfaced hours later
            # as "no such column" on some unrelated page. A boot that cannot
            # bring the schema up to the models must say so and stop.
            _mig_log.error("[migrate] column migration failed and was rolled back: %s", e)
            raise
    if added:
        _mig_log.info("[migrate] added %d column(s): %s", len(added),
                      ", ".join(f"{t}.{c}" for t, c in sorted(added)))


def _deletion_proposal_index_ddl() -> list[str]:
    """``CREATE INDEX IF NOT EXISTS`` for every index the model declares on
    ``deletion_proposals`` — derived from the model so a newly added index
    can't be forgotten by the table rebuild below or the repair step."""
    from sqlalchemy.schema import CreateIndex
    from src.database.models import DeletionProposal
    return [str(CreateIndex(ix, if_not_exists=True).compile(dialect=engine.dialect))
            for ix in sorted(DeletionProposal.__table__.indexes, key=lambda i: i.name)]


def _ensure_deletion_proposal_indexes() -> None:
    """Repair: recreate any missing ``deletion_proposals`` index.

    The first version of the Pass-90c rebuild below lost both of them: index
    names are global in SQLite, so the renamed-aside table kept owning
    ``idx_dp_latest_activity`` (the new table's CREATE INDEX IF NOT EXISTS
    was a no-op) and DROP TABLE then took it along; ``idx_dp_user_status``
    was never recreated at all. create_all doesn't add indexes to an
    existing table, so DBs migrated back then have run without them since.
    Idempotent — a no-op once the indexes exist."""
    from sqlalchemy import text
    import logging as _logging
    _mig_log = _logging.getLogger(__name__)
    with engine.connect() as conn:
        exists = conn.execute(text(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='deletion_proposals'"
        )).first()
        if not exists:
            return
        for ddl in _deletion_proposal_index_ddl():
            try:
                conn.execute(text(ddl))
            except Exception as e:
                # Lookup indexes only (no uniqueness), so boot goes on — slowly.
                _mig_log.error("[migrate] deletion_proposals index repair failed "
                               "(%s): %s", ddl, e)
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
    present (fresh installs get it from create_all via the model's
    ``sqlite_autoincrement`` flag). SQLite's standard rebuild-via-rename
    pattern: rename the existing table aside, create the new one, copy the
    rows, drop the old. The copy keeps every row's explicit id, and SQLite
    advances ``sqlite_sequence`` past the largest one by itself, so the next
    ID continues from the current max.

    The new table's DDL comes from the MODEL, and the copy moves every column
    the old and new tables share. The first version used a hard-coded column
    list that predated score_factors / soulsync_ban / returned_at /
    returned_info, so the rebuild silently dropped them — on every fresh
    install, whose first boot ran it — and every DeletionProposal query
    failed with "no such column" until the next restart re-added them.

    No FK references to ``deletion_proposals.id`` exist anywhere else
    in the schema (CuratorResolutionLog + ProtectedMedia store the
    title text directly, not the FK), so the rename-and-drop is safe.
    """
    import logging as _logging
    from src.database.models import DeletionProposal
    _mig_log = _logging.getLogger(__name__)

    # Driver-level autocommit: pysqlite opens no implicit transaction of its
    # own, so the explicit BEGIN IMMEDIATE below can never collide with one.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        ddl = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='deletion_proposals'"
        ).scalar()
        if not ddl:
            return  # create_all builds it with the model's AUTOINCREMENT flag
        if "AUTOINCREMENT" in ddl.upper():
            return  # already migrated

        _mig_log.info("[migrate] Pass 90c: converting deletion_proposals.id → AUTOINCREMENT")
        try:
            # IMMEDIATE holds the write lock for the duration, so a
            # concurrent writer can never see the half-migrated state; SQLite
            # DDL is transactional, so a failure rolls back to the old table.
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            _rebuild_from_model(conn, DeletionProposal.__table__)
            conn.exec_driver_sql("COMMIT")
            _mig_log.info("[migrate] Pass 90c: AUTOINCREMENT migration complete")
        except Exception as e:
            try:
                conn.exec_driver_sql("ROLLBACK")
            except Exception:  # noqa: BLE001 - rollback in failure path; logged as ERROR below
                pass
            _mig_log.error(
                "[migrate] Pass 90c FAILED: %s — rolled back, table left in "
                "its pre-migration state", e,
            )


def _rebuild_from_model(conn, table) -> None:
    """Rebuild ``table`` from the current model definition, inside the
    caller's transaction. Copies only the columns both versions share, so a
    column the model declares is never lost and one the old table lacks
    takes its default."""
    from sqlalchemy.schema import CreateIndex, CreateTable

    name, aside = table.name, f"{table.name}_old_rebuild"
    # Index names are global in SQLite and RENAME keeps them on the
    # renamed-aside table — drop them first, or the CREATE INDEX IF NOT
    # EXISTS below is a no-op and DROP TABLE takes them along.
    for (idx_name,) in conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name=? AND sql IS NOT NULL", (name,)
    ).fetchall():
        conn.exec_driver_sql(f'DROP INDEX IF EXISTS "{idx_name}"')
    conn.exec_driver_sql(f'ALTER TABLE "{name}" RENAME TO "{aside}"')
    conn.exec_driver_sql(str(CreateTable(table).compile(dialect=engine.dialect)))
    old_cols = {r[1] for r in conn.exec_driver_sql(f'PRAGMA table_info("{aside}")')}
    shared = ", ".join(f'"{c.name}"' for c in table.columns if c.name in old_cols)
    conn.exec_driver_sql(f'INSERT INTO "{name}" ({shared}) SELECT {shared} FROM "{aside}"')  # nosec B608 - identifiers come from the model and PRAGMA table_info, no input
    for ix in sorted(table.indexes, key=lambda i: i.name):
        conn.exec_driver_sql(str(CreateIndex(ix, if_not_exists=True).compile(dialect=engine.dialect)))
    conn.exec_driver_sql(f'DROP TABLE "{aside}"')


def _secure_db_files() -> None:
    """Ensure SQLite files in data/ and .env are 0600 (POSIX only)."""
    import os
    import stat
    import logging as _logging
    from src.paths import DATA_DIR, ENV_FILE

    _mig_log = _logging.getLogger(__name__)
    if ENV_FILE.exists():
        try:
            os.chmod(ENV_FILE, stat.S_IRUSR | stat.S_IWUSR)
        except OSError as e:
            _mig_log.warning("Could not chmod %s to 0600: %s", ENV_FILE, e)

    if not DATA_DIR.exists():
        return

    for ext in ("*.db", "*.sqlite3", "*.sqlite3-wal", "*.sqlite3-shm", "*.db-wal", "*.db-shm"):
        for p in DATA_DIR.rglob(ext):
            try:
                os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
            except FileNotFoundError:
                pass   # a -wal/-shm sidecar checkpointed away mid-scan
            except OSError as e:
                _mig_log.warning("Could not chmod %s to 0600: %s", p, e)


def _drop_retired_tables() -> None:
    """Tables whose feature is gone. ``user_pin_hashes`` held PBKDF2 hashes of
    the "encryption passphrase" that never encrypted anything; a short PIN
    and its salt side by side are recoverable offline, so the rows go rather
    than linger (2026-10-10). secure_delete zeroes the freed pages instead of
    leaving the bytes in the file until the next VACUUM."""
    import logging as _logging
    from sqlalchemy import text
    try:
        with engine.begin() as conn:
            if not conn.execute(text(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='user_pin_hashes'")).first():
                return
            conn.execute(text("PRAGMA secure_delete=ON"))
            try:
                conn.execute(text("DROP TABLE user_pin_hashes"))
            finally:
                conn.execute(text("PRAGMA secure_delete=OFF"))
        _logging.getLogger(__name__).info("[migrate] retired table user_pin_hashes dropped")
    except Exception as e:  # noqa: BLE001 - a leftover table is harmless, a dead boot is not
        _logging.getLogger(__name__).warning("[migrate] could not drop user_pin_hashes: %s", e)


def init_db():
    """Create all tables and migrate any new columns."""
    Base.metadata.create_all(bind=engine)
    _drop_retired_tables()
    _migrate_columns()
    _migrate_deletion_proposals_autoincrement()
    _ensure_deletion_proposal_indexes()
    _secure_db_files()
