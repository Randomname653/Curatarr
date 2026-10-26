"""
Curatarr - Anime ID Mapping Service

Downloads and caches the anime-lists crossref database:
  AniDB ID ↔ TVDB ID ↔ AniList ID (via AniDB)

Source: https://github.com/Anime-Lists/anime-lists
Updated weekly by the community.

Usage:
  mapping = await get_anime_mapping()
  anidb_id = mapping.tvdb_to_anidb.get(73739)
  anilist_id = mapping.anidb_to_anilist.get(anidb_id)
"""

import asyncio
import logging
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

ANIME_LIST_URL = "https://raw.githubusercontent.com/Anime-Lists/anime-lists/master/anime-list-master.xml"
from src.paths import DATA_DIR
from src.database.loop_guard import sqlite_connect
from src.services.http_client import tls_context
MAPPING_DB_PATH = DATA_DIR / "cache" / "anime_mapping.db"
MAX_AGE_DAYS = 7  # re-download weekly


class AnimeMapping:
    def __init__(self):
        self.tvdb_to_anidb: dict[int, int] = {}
        self.anidb_to_tvdb: dict[int, int] = {}
        self.tvdb_to_anilist: dict[int, int] = {}  # via AniDB lookup
        self.anidb_to_anilist: dict[int, int] = {}
        self.total_entries: int = 0
        self.loaded_at: Optional[datetime] = None

    def lookup_tvdb(self, tvdb_id: int) -> dict:
        """Return all known IDs for a TVDB series."""
        anidb = self.tvdb_to_anidb.get(tvdb_id)
        return {
            "tvdb_id": tvdb_id,
            "anidb_id": anidb,
            "anilist_id": self.anidb_to_anilist.get(anidb) if anidb else None,
        }


# Module-level singleton
_mapping: Optional[AnimeMapping] = None
_load_lock: Optional[asyncio.Lock] = None
# An empty load (first install + failed download) used to be cached for the
# whole process life, so anime routing stayed blind until a restart. Empty
# results are now only held for this long before the next caller retries.
_EMPTY_RETRY_SECONDS = 15 * 60
_empty_until: Optional[datetime] = None


def _get_load_lock() -> asyncio.Lock:
    """Lazily build the lock so it's bound to the running event loop."""
    global _load_lock
    if _load_lock is None:
        _load_lock = asyncio.Lock()
    return _load_lock


async def get_anime_mapping(force_refresh: bool = False) -> AnimeMapping:
    """Get the anime mapping, downloading if necessary.

    Concurrent callers serialize on a real asyncio.Lock so a single download
    happens and every caller receives the same populated mapping (no empty
    fallback on a coincident timeout).
    """
    global _mapping, _empty_until

    if _mapping is not None and not force_refresh and _is_settled(_mapping):
        return _mapping

    async with _get_load_lock():
        # Double-check after acquiring the lock — a previous waiter may have
        # already populated the mapping.
        if _mapping is not None and not force_refresh and _is_settled(_mapping):
            return _mapping
        _mapping = await _load_or_download()
        if _mapping.total_entries == 0:
            _empty_until = datetime.utcnow() + timedelta(seconds=_EMPTY_RETRY_SECONDS)
            logger.warning("Anime mapping is empty — will retry the download in %d min",
                           _EMPTY_RETRY_SECONDS // 60)
        else:
            _empty_until = None
        return _mapping


def _is_settled(mapping: AnimeMapping) -> bool:
    """A populated mapping is final; an empty one only until its retry time."""
    if mapping.total_entries > 0:
        return True
    return _empty_until is not None and datetime.utcnow() < _empty_until


async def _load_or_download() -> AnimeMapping:
    """Load from local DB or download fresh. The cache file reads and writes
    and the XML parse run on worker threads: tens of thousands of rows, once
    after startup, while the server is already answering."""
    MAPPING_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Check if local DB exists and is fresh
    if MAPPING_DB_PATH.exists():
        age = datetime.utcnow() - datetime.fromtimestamp(MAPPING_DB_PATH.stat().st_mtime)
        if age < timedelta(days=MAX_AGE_DAYS):
            mapping = await asyncio.to_thread(_load_from_db)
            if mapping.total_entries > 0:
                logger.info("Anime mapping loaded from cache: %d entries", mapping.total_entries)
                return mapping

    # Download fresh
    logger.info("Downloading anime-lists mapping from GitHub...")
    try:
        async with httpx.AsyncClient(verify=tls_context(), timeout=30) as client:
            r = await client.get(ANIME_LIST_URL)
        if r.status_code != 200:
            logger.warning("Failed to download anime-lists: HTTP %s", r.status_code)
            return await asyncio.to_thread(_load_from_db)  # fall back to cached

        mapping = await asyncio.to_thread(_parse_xml, r.text)
        if mapping.total_entries == 0:
            # A truncated/garbled download parses to nothing. Saving that used
            # to DROP the populated cache table and leave the DB empty.
            logger.warning("Anime mapping download parsed to 0 entries — keeping cached copy")
            return await asyncio.to_thread(_load_from_db)
        try:
            await asyncio.to_thread(_save_to_db, mapping)
        except Exception as e:
            # The parsed mapping is still good for this process; the old cache
            # survives because the swap is transactional.
            logger.warning("Anime mapping cache write failed: %s", e)
            return mapping
        logger.info("Anime mapping downloaded and cached: %d entries", mapping.total_entries)
        return mapping

    except Exception as e:
        logger.warning("Anime mapping download failed: %s — using cached", e)
        return await asyncio.to_thread(_load_from_db)


def _parse_xml(xml_text: str) -> AnimeMapping:
    """Parse anime-list-master.xml into mapping dicts."""
    mapping = AnimeMapping()
    try:
        root = ET.fromstring(xml_text)  # nosec B314 - weekly community mapping file; modern expat blocks amplification
        for anime in root.findall("anime"):
            anidb_id_str = anime.get("anidbid")
            tvdb_id_str = anime.get("tvdbid")

            if not anidb_id_str or not tvdb_id_str:
                continue

            try:
                anidb_id = int(anidb_id_str)
            except ValueError:
                continue

            # tvdbid can be numeric or "unknown"
            try:
                tvdb_id = int(tvdb_id_str)
            except ValueError:
                continue

            mapping.tvdb_to_anidb[tvdb_id] = anidb_id
            mapping.anidb_to_tvdb[anidb_id] = tvdb_id
            mapping.total_entries += 1

    except ET.ParseError as e:
        logger.error("XML parse error: %s", e)

    mapping.loaded_at = datetime.utcnow()
    return mapping


def _save_to_db(mapping: AnimeMapping):
    """Persist mapping to local SQLite for fast startup.

    Builds the new table beside the old one and swaps inside one transaction:
    the old DROP-then-INSERT ran in autocommit, so a crash (or an empty parse)
    left the cache wiped. An empty mapping is never saved.
    """
    if mapping.total_entries == 0 or not mapping.tvdb_to_anidb:
        logger.warning("Refusing to save an empty anime mapping over the cache")
        return
    conn = sqlite_connect(MAPPING_DB_PATH, isolation_level=None)
    try:
        conn.execute("BEGIN")
        # AniList ids are learned at runtime via update_anilist_id(); the XML
        # never carries them, so the weekly refresh used to reset them to NULL.
        learned: dict[int, int] = {}
        has_old = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='anime_mapping'"
        ).fetchone()
        if has_old:
            learned = dict(conn.execute(
                "SELECT anidb_id, anilist_id FROM anime_mapping WHERE anilist_id IS NOT NULL"
            ).fetchall())
        conn.execute("DROP TABLE IF EXISTS anime_mapping_new")
        # One row per AniDB entry. Saving from tvdb_to_anidb kept a single
        # AniDB id per TVDB series, so every other season's entry vanished
        # from the cache after the first restart. ``is_primary`` marks the
        # entry tvdb_to_anidb resolves to, so a load rebuilds the same lookup.
        conn.execute("""
            CREATE TABLE anime_mapping_new (
                anidb_id INTEGER NOT NULL,
                tvdb_id  INTEGER NOT NULL,
                anilist_id INTEGER,
                is_primary INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (anidb_id)
            )
        """)
        rows = [
            (anidb, tvdb, mapping.anidb_to_anilist.get(anidb) or learned.get(anidb),
             1 if mapping.tvdb_to_anidb.get(tvdb) == anidb else 0)
            for anidb, tvdb in mapping.anidb_to_tvdb.items()
        ]
        conn.executemany("INSERT OR REPLACE INTO anime_mapping_new VALUES (?,?,?,?)", rows)
        conn.execute("DROP TABLE IF EXISTS anime_mapping")
        conn.execute("ALTER TABLE anime_mapping_new RENAME TO anime_mapping")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tvdb ON anime_mapping (tvdb_id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)
        """)
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('downloaded_at', ?)",
                     (datetime.utcnow().isoformat(),))
        conn.execute("COMMIT")
        for anidb, anilist in learned.items():
            if anidb in mapping.anidb_to_tvdb and anidb not in mapping.anidb_to_anilist:
                mapping.anidb_to_anilist[anidb] = anilist
                mapping.tvdb_to_anilist[mapping.anidb_to_tvdb[anidb]] = anilist
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _load_from_db() -> AnimeMapping:
    """Load mapping from local SQLite cache."""
    mapping = AnimeMapping()
    if not MAPPING_DB_PATH.exists():
        return mapping
    try:
        conn = sqlite_connect(MAPPING_DB_PATH)
        try:
            rows = conn.execute("SELECT anidb_id, tvdb_id, anilist_id, is_primary "
                                "FROM anime_mapping").fetchall()
        except sqlite3.OperationalError:
            # A cache written before is_primary: one row per TVDB series,
            # every row the primary one.
            rows = [(*r, 1) for r in conn.execute(
                "SELECT anidb_id, tvdb_id, anilist_id FROM anime_mapping").fetchall()]
        for anidb_id, tvdb_id, anilist_id, primary in rows:
            mapping.anidb_to_tvdb[anidb_id] = tvdb_id
            if primary or tvdb_id not in mapping.tvdb_to_anidb:
                mapping.tvdb_to_anidb[tvdb_id] = anidb_id
            if anilist_id:
                mapping.anidb_to_anilist[anidb_id] = anilist_id
                if primary:
                    mapping.tvdb_to_anilist[tvdb_id] = anilist_id
        mapping.total_entries = len(rows)

        ts = conn.execute("SELECT value FROM meta WHERE key='downloaded_at'").fetchone()
        if ts:
            mapping.loaded_at = datetime.fromisoformat(ts[0])
        conn.close()
    except Exception as e:
        logger.warning("Failed to load anime mapping from DB: %s", e)
    return mapping


def update_anilist_id(anidb_id: int, anilist_id: int):
    """Store a discovered AniDB→AniList mapping for future use."""
    global _mapping
    if _mapping:
        _mapping.anidb_to_anilist[anidb_id] = anilist_id
        tvdb = _mapping.anidb_to_tvdb.get(anidb_id)
        if tvdb:
            _mapping.tvdb_to_anilist[tvdb] = anilist_id

    if MAPPING_DB_PATH.exists():
        try:
            conn = sqlite_connect(MAPPING_DB_PATH)
            conn.execute(
                "UPDATE anime_mapping SET anilist_id=? WHERE anidb_id=?",
                (anilist_id, anidb_id)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning("[anime-mapping] could not store AniDB %s -> AniList %s: %s",
                           anidb_id, anilist_id, e)


async def get_mapping_stats() -> dict:
    """The mapping file, and how many of the library's anime carry an AniList
    id. The file's own AniList column was never filled (update_anilist_id
    has no caller; resolving through AniDB ids drifts, see ROADMAP), so the
    card read "0 resolved, 7,514 pending" forever (2026-10-26). What counts
    is the enriched profiles: title + year, the offline snapshot, the pins."""
    mapping = await get_anime_mapping()
    out = {
        "total_entries": mapping.total_entries,
        "tvdb_mapped": len(mapping.tvdb_to_anidb),
        "downloaded_at": mapping.loaded_at.isoformat() if mapping.loaded_at else None,
        "db_path": str(MAPPING_DB_PATH),
    }
    try:
        from src.database.runner import run_read
        out.update(await run_read(anilist_coverage))
    except Exception as e:  # noqa: BLE001 - the card shows the file numbers alone
        logger.warning("AniList coverage not read: %s", e)
    return out


def anilist_coverage() -> dict:
    """{"anime", "with_anilist", "pinned", "auto_pinned"}: the Sonarr anime,
    those whose enriched profile names an AniList id, and the pins among
    them (the owner's and the TVDB crossref's). Blocking: run via run_read."""
    from src.cache.metadata_cache import MetadataCache
    from src.database.connection import get_db_session
    from src.database.models import ArrEnrichmentStatus as AES, MediaMatchOverride as MO
    with get_db_session() as db:
        ids = [a for (a,) in db.query(AES.arr_id).filter(AES.service == "sonarr", AES.category == "anime")]
        notes = [n or "" for (n,) in db.query(MO.note).filter(
            MO.service == "sonarr", MO.anilist_id.isnot(None))]
    with MetadataCache() as cache:
        got = cache.get_cache_many([f"enriched:anime:sonarr:{a}" for a in ids])
    with_id = sum(1 for e in got.values() if (e.get("response") or {}).get("anilist_id"))
    return {"anime": len(ids), "with_anilist": with_id, "pinned": len(notes),
            "auto_pinned": sum(1 for n in notes if n.startswith("auto:"))}
