"""
Curatarr — anime-offline-database loader (the AniDB-gold carrier).

AniDB's API is rate-limited into unusability (1 req/2-4s, ~150/day, IP bans),
but the manami-project publishes a WEEKLY aggregate of ten providers (AniDB,
MAL, AniList, Kitsu, …) as one ~60 MB JSON release: per anime all provider
ids, title + synonyms, the AniDB community tags ("footage reuse", "borderline
porn" — production/content signals no curated keyword list carries), score
aggregates, studios, episodes. One download a week replaces thousands of
banned API calls.

Prototype-validated (tests/franchise_proto.py): all test titles resolved via
provider id; the anidb-id space DRIFTED for one title (pointed at a 1-episode
OVA) while the anilist id was right — so lookups here go anilist > mal >
title+year > anidb, and a caller-supplied year is verified when present.

Mirrors anime_mapping.py: weekly re-download, SQLite cache, module singleton.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DUMP_URL = ("https://github.com/manami-project/anime-offline-database"
            "/releases/latest/download/anime-offline-database-minified.json")
# repo-root-anchored — background/standalone runners don't share the app CWD
from src.paths import ROOT as _REPO_ROOT
OFFLINE_DB_PATH = _REPO_ROOT / "data" / "cache" / "anime_offline.db"
MAX_AGE_DAYS = 7

_load_lock: Optional[asyncio.Lock] = None


def _get_load_lock() -> asyncio.Lock:
    global _load_lock
    if _load_lock is None:
        _load_lock = asyncio.Lock()
    return _load_lock


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _id_from(sources: list, provider: str) -> Optional[int]:
    for s in sources or []:
        if provider in s:
            m = re.search(r"/(\d+)$", s)
            if m:
                return int(m.group(1))
    return None


def _fresh() -> bool:
    if not OFFLINE_DB_PATH.exists():
        return False
    age = datetime.utcnow() - datetime.fromtimestamp(OFFLINE_DB_PATH.stat().st_mtime)
    if age >= timedelta(days=MAX_AGE_DAYS):
        return False
    try:
        con = sqlite3.connect(OFFLINE_DB_PATH)
        n = con.execute("SELECT COUNT(*) FROM offline_anime").fetchone()[0]
        con.close()
        return n > 10000
    except Exception:
        return False


async def ensure_offline_db(allow_download: bool = True) -> bool:
    """Make sure the weekly snapshot exists locally. Returns True when a
    usable DB is present. ``allow_download=False`` (JIT paths) never blocks
    on the 60 MB fetch — it just reports whether the local copy is usable."""
    if _fresh():
        return True
    if not allow_download:
        return OFFLINE_DB_PATH.exists()
    async with _get_load_lock():
        if _fresh():
            return True
        try:
            t0 = time.time()
            async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
                r = await client.get(DUMP_URL)
            if r.status_code != 200:
                logger.warning("[anime-offline] download HTTP %s", r.status_code)
                return OFFLINE_DB_PATH.exists()
            entries = (json.loads(r.content).get("data")) or []
            _rebuild(entries)
            logger.info("[anime-offline] snapshot rebuilt: %d entries in %.0fs",
                        len(entries), time.time() - t0)
            return True
        except Exception as e:
            logger.warning("[anime-offline] refresh failed: %s", e)
            return OFFLINE_DB_PATH.exists()


def _rebuild(entries: list[dict]) -> None:
    OFFLINE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(OFFLINE_DB_PATH)
    try:
        cur = con.cursor()
        cur.executescript("""
            DROP TABLE IF EXISTS offline_anime;
            DROP TABLE IF EXISTS offline_names;
            CREATE TABLE offline_anime (
                id INTEGER PRIMARY KEY,
                anidb INTEGER, anilist INTEGER, mal INTEGER,
                title TEXT, year INTEGER, type TEXT, episodes INTEGER,
                score_median REAL, studios TEXT, tags TEXT
            );
            CREATE TABLE offline_names (name TEXT, anime_id INTEGER);
        """)
        for i, e in enumerate(entries):
            src = e.get("sources") or []
            cur.execute(
                "INSERT INTO offline_anime VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (i, _id_from(src, "anidb.net"), _id_from(src, "anilist.co"),
                 _id_from(src, "myanimelist.net"),
                 e.get("title"), (e.get("animeSeason") or {}).get("year"),
                 e.get("type"), e.get("episodes"),
                 (e.get("score") or {}).get("median"),
                 json.dumps(e.get("studios") or []),
                 json.dumps(e.get("tags") or [])),
            )
            names = {_norm(e.get("title") or "")}
            names.update(_norm(s) for s in (e.get("synonyms") or []))
            cur.executemany("INSERT INTO offline_names VALUES (?,?)",
                            [(n, i) for n in names if n])
        cur.executescript("""
            CREATE INDEX idx_off_anilist ON offline_anime(anilist);
            CREATE INDEX idx_off_anidb   ON offline_anime(anidb);
            CREATE INDEX idx_off_mal     ON offline_anime(mal);
            CREATE INDEX idx_off_name    ON offline_names(name);
        """)
        con.commit()
    finally:
        con.close()


def lookup(*, anilist_id=None, mal_id=None, anidb_id=None,
           title: str = None, year: int = None) -> Optional[dict]:
    """Resolve one anime from the snapshot. Priority: anilist > mal >
    title+year (±1) > anidb (the drifting id space, last resort). A known
    year must roughly match the entry or the hit is rejected."""
    if not OFFLINE_DB_PATH.exists():
        return None
    try:
        con = sqlite3.connect(OFFLINE_DB_PATH)
        con.row_factory = sqlite3.Row
        row = None
        for col, val in (("anilist", anilist_id), ("mal", mal_id)):
            if val:
                row = con.execute(
                    f"SELECT * FROM offline_anime WHERE {col}=?", (int(val),)).fetchone()
                if row:
                    break
        if row is None and title:
            cands = con.execute(
                "SELECT a.* FROM offline_names n JOIN offline_anime a "
                "ON a.id=n.anime_id WHERE n.name=?", (_norm(title),)).fetchall()
            if year:
                cands = [c for c in cands if c["year"] and abs(c["year"] - year) <= 1]
            row = cands[0] if cands else None
        if row is None and anidb_id:
            row = con.execute(
                "SELECT * FROM offline_anime WHERE anidb=?", (int(anidb_id),)).fetchone()
        con.close()
        if row is None:
            return None
        if year and row["year"] and abs(row["year"] - year) > 1:
            return None   # wrong entry (id drift / homonym) — refuse
        return {
            "anidb_id": row["anidb"], "anilist_id": row["anilist"],
            "mal_id": row["mal"], "title": row["title"], "year": row["year"],
            "episodes": row["episodes"], "score_median": row["score_median"],
            "studios": json.loads(row["studios"] or "[]"),
            "tags": json.loads(row["tags"] or "[]"),
        }
    except Exception as e:
        logger.debug("[anime-offline] lookup failed: %s", e)
        return None
