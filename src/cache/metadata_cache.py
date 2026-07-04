"""
ARR Suite LLM - Metadata Cache (Phase A)

SQLite cache for metadata API responses to avoid rate limit issues.
"""

import sqlite3
import json
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from pathlib import Path
import logging

from src.config import settings

logger = logging.getLogger(__name__)

# Bump this when title-match logic, response shape, or any other "what does
# the cached value mean" assumption changes. Old entries become effectively
# invisible (cache miss → fresh fetch) — they don't get physically deleted
# (Pass 48 removed ``cleanup_expired``), but ``get_cache`` filters by
# ``expires_at`` at read time, so stale rows are functionally inert. Without
# this version key, the cache served stale "Strike It Rich" results for "It"
# queries even after _titles_close_enough was tightened in Pass 14.4 — the
# cache key was unchanged, so the new logic never ran.
_CACHE_VERSION = "v2"


class MetadataCache:
    """
    SQLite cache for metadata API responses.
    
    Stores:
    - TMDB movie/series data
    - OMDb data
    - AniDB data
    - Media hashes for deduplication
    
    Features:
    - Cache expiration (7 days default)
    - Hash-based deduplication
    - Automatic cleanup of old entries
    """
    
    def __init__(self, cache_path: Path = None):
        """
        Initialize metadata cache.

        Args:
            cache_path: Path to cache database (default: data/cache/metadata.db)
        """
        self.cache_path = Path(cache_path or settings.ENRICHMENT_CACHE)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

        # Pass 29: the metadata.db connection used to be created with
        # all-default pragmas — no WAL, no busy_timeout. Under concurrent
        # enrichment (multiple producer/consumer tasks each opening their
        # own MetadataCache) this surfaced as
        #   sqlite3.OperationalError: database is locked
        # on every collision, because the default busy_timeout is 0
        # ("fail immediately"). Match the pragma profile the main DB uses
        # in src/database/connection.py.
        self.conn = sqlite3.connect(
            self.cache_path,
            check_same_thread=False,
            timeout=60.0,   # python-side wait when SQLite reports BUSY
        )
        self.conn.row_factory = sqlite3.Row
        try:
            cur = self.conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=60000")
            cur.execute("PRAGMA wal_autocheckpoint=1000")
            cur.close()
        except Exception as e:
            logger.warning("[metadata_cache] PRAGMA setup failed: %s", e)
        self._init_db()
    
    def _init_db(self):
        """Initialize database schema.

        Pass 48: ``media_items`` table + ``idx_media_hash`` / ``idx_expiration``
        indices were removed. Nothing in src/ ever read from media_items —
        ``get_media`` / ``set_media`` / ``get_all`` / ``get_all_unenriched``
        had no callers, and the parallel ``api_cache`` table covered the
        actual cache needs. Existing DBs keep the orphaned table (no
        destructive migration), but new installs don't create it.
        """
        cursor = self.conn.cursor()

        # API response cache — the only cache surface this class actually serves.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS api_cache (
                cache_key TEXT PRIMARY KEY,
                response TEXT,
                created_at TEXT,
                expires_at TEXT
            )
        """)

        self.conn.commit()

    def _expires_at(self, days: int = 7) -> str:
        """Calculate expiration date."""
        return (datetime.now() + timedelta(days=days)).isoformat()

    def get_cache(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """
        Get cached API response.

        Args:
            cache_key: Cache key (e.g., "tmdb:movie:123") — gets prefixed
                       with ``_CACHE_VERSION`` so old-schema entries become
                       invisible after a logic bump.

        Returns:
            Cached response or None

        Pass 95: read-only fallback to un-versioned key. When the cache
        version was bumped (currently ``v2``), every existing entry
        became invisible by design — the assumption being that callers
        would re-fetch. That assumption broke for anime: 4,027
        ``enriched:anime:*`` entries were written under v1 and never
        re-fetched because the Pre-filter (enrichment.py) skips items
        already marked ``EnrichmentStatus.enriched=True``. Result: the
        taste-engine looked up the 731 watch-history anime plex_item_ids,
        none had a v2 entry, and the resulting anime taste vector was
        built with zero enrichment context — the curator lost its anime
        edge entirely.

        This fallback serves the v1 entry when v2 doesn't exist. The v1
        schema is a subset of v2 (genres/themes/mood/title — enough to
        build a meaningful embedding text). When an item is genuinely
        re-enriched later, the producer writes a richer v2 entry that
        wins the lookup. We do NOT write-back v1 → v2 here on read,
        because that would silently pollute the v2 namespace with the
        leaner v1 data — actual re-enrichment is the right path for
        full v2 quality.
        """
        versioned_key = f"{_CACHE_VERSION}:{cache_key}"
        now_iso = datetime.now().isoformat()
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM api_cache WHERE cache_key = ? AND expires_at > ?",
            (versioned_key, now_iso),
        )

        row = cursor.fetchone()
        if row:
            return {
                "response": json.loads(row['response']),
                "created_at": row['created_at'],
                "expires_at": row['expires_at'],
            }

        # Pass 95: fallback to un-versioned key (pre-v2 schema).
        cursor.execute(
            "SELECT * FROM api_cache WHERE cache_key = ? AND expires_at > ?",
            (cache_key, now_iso),
        )
        row = cursor.fetchone()
        if row:
            return {
                "response": json.loads(row['response']),
                "created_at": row['created_at'],
                "expires_at": row['expires_at'],
            }
        return None

    def live_key_set(self, prefix: str) -> set:
        """All NON-EXPIRED cache keys starting with ``prefix`` (version prefix
        stripped; un-versioned v1 keys included, matching get_cache's read
        fallback). One query instead of thousands of point lookups — used by
        the enrichment pre-filter to detect 'flag says enriched but the cache
        entry is dead' items in bulk."""
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT cache_key FROM api_cache
            WHERE (cache_key LIKE ? OR cache_key LIKE ?)
              AND expires_at > ?
            """,
            (f"{_CACHE_VERSION}:{prefix}%", f"{prefix}%",
             datetime.now().isoformat()),
        )
        out = set()
        vpre = f"{_CACHE_VERSION}:"
        for (k,) in cur.fetchall():
            out.add(k[len(vpre):] if k.startswith(vpre) else k)
        cur.close()
        return out

    def set_cache(self, cache_key: str, response: Dict[str, Any], days: int = 7):
        """
        Cache API response.

        Args:
            cache_key: Cache key (auto-prefixed with ``_CACHE_VERSION``)
            response: API response dict
            days: Cache expiration in days
        """
        versioned_key = f"{_CACHE_VERSION}:{cache_key}"
        cursor = self.conn.cursor()

        # Pass 29: single atomic INSERT OR REPLACE eliminates the
        # SELECT-then-INSERT/UPDATE TOCTOU race that caused
        #   UNIQUE constraint failed: api_cache.cache_key
        # when two enrichment tasks raced to cache the same lookup
        # (TMDB/AniList batch-fetch a single popular title from
        # multiple producers). Also halves the round-trip cost: one
        # statement instead of SELECT + branch + INSERT/UPDATE.
        # ``created_at`` uses the existing column's value via COALESCE
        # so a REPLACE doesn't lose the original creation timestamp.
        cursor.execute(
            """
            INSERT INTO api_cache (cache_key, response, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                response   = excluded.response,
                expires_at = excluded.expires_at
            """,
            (
                versioned_key,
                json.dumps(response, default=str),
                datetime.now().isoformat(),
                self._expires_at(days),
            ),
        )

        self.conn.commit()
    
    # Pass 48: removed methods (had zero callers in src/):
    #   - ``_generate_hash`` (only used internally by set_media)
    #   - ``get_media`` / ``set_media`` (media_items dead, see _init_db)
    #   - ``get_all`` / ``get_all_unenriched`` (media_items dead)
    #   - ``cleanup_expired`` (no scheduler job invoked it; weekly DB
    #     vacuum reclaims pages, and api_cache filters expired rows at
    #     read time anyway, so old rows are functionally invisible)

    def close(self):
        """Close database connection."""
        self.conn.close()
