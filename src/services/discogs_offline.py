"""
Curatarr — Discogs styles from the monthly CC0 data dump (no API, no token).

Discogs' API requires an auth token and is rate-limited, but the full
database ships monthly as CC0 dumps (data.discogs.com). We only need the
MASTERS file (~590 MB gz — the werk-level records carrying the precise
STYLE vocabulary: Frenchcore, Uptempo Hardcore, Speedcore… where Last.fm
tags stay coarse) — never the 10.4 GB releases file.

Strategy: stream the gzip, regex-scan <master> blocks incrementally (fixed,
years-stable schema), keep ONLY masters whose artist is in the owner's
music world (Lidarr artists ∪ listening-history artists, dash-folded), and
persist a compact SQLite (per-master rows + per-artist aggregated styles).
One run a month via the custodian; the download endpoint itself 429s eager
clients, so this module never retries aggressively.
"""
from __future__ import annotations

import gzip
import json
import logging
import re
import sqlite3
import time
import zlib
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DATA_INDEX = "https://data.discogs.com/"
UA = {"User-Agent": "Curatarr/1.0 (https://github.com/Randomname653/curatarr; "
                    "personal media curator) python-httpx"}
STYLES_DB_PATH = Path("data/cache/discogs_styles.db")
MAX_AGE_DAYS = 30

_MASTER_RE = re.compile(rb"<master id=\"(\d+)\">(.*?)</master>", re.S)
_TITLE_RE = re.compile(rb"<title>([^<]*)</title>")
_YEAR_RE = re.compile(rb"<year>(\d{4})</year>")
_ARTIST_NAME_RE = re.compile(rb"<artist><id>\d+</id><name>([^<]*)</name>")
_GENRE_RE = re.compile(rb"<genre>([^<]*)</genre>")
_STYLE_RE = re.compile(rb"<style>([^<]*)</style>")

_DASHES = str.maketrans({c: "-" for c in "‐‑‒–—−"})


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower().translate(_DASHES)).strip()


def _unescape(b: bytes) -> str:
    s = b.decode("utf-8", "replace")
    return (s.replace("&amp;", "&").replace("&lt;", "<")
             .replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'"))


def _library_artist_norms() -> set[str]:
    """The owner's music world: Lidarr artists ∪ listening-history artists."""
    names: set[str] = set()
    try:
        from src.config import settings
        base = (settings.LIDARR_URL or "").rstrip("/")
        if base and settings.LIDARR_API_KEY:
            r = httpx.get(f"{base}/api/v1/artist",
                          params={"apikey": settings.LIDARR_API_KEY},
                          timeout=30)
            if r.status_code == 200:
                names.update(a.get("artistName") or "" for a in r.json())
    except Exception as e:
        logger.debug("[discogs] lidarr artist list failed: %s", e)
    try:
        from src.database.connection import get_db_session
        from src.database.models import WatchHistoryEntry as W
        from sqlalchemy import distinct
        with get_db_session() as db:
            rows = (db.query(distinct(W.series_title))
                    .filter(W.media_type == "music").all())
            names.update(r[0] or "" for r in rows)
    except Exception as e:
        logger.debug("[discogs] history artist list failed: %s", e)
    return {n for n in (_norm(x) for x in names) if n}


def _latest_masters_key() -> Optional[str]:
    """Newest masters key via the index page — with a DETERMINISTIC fallback
    (dumps are always data/YYYY/discogs_YYYYMM01_masters.xml.gz), because the
    index endpoint 429s eager IPs for hours and we don't actually need it."""
    try:
        r = httpx.get(DATA_INDEX, params={"prefix": f"data/{datetime.now().year}/"},
                      timeout=30, headers=UA)
        if r.status_code == 200:
            keys = re.findall(r'\?download=([^"]*masters\.xml\.gz)', r.text)
            if keys:
                return keys[-1]
    except Exception as e:
        logger.debug("[discogs] index fetch failed: %s", e)
    now = datetime.now()
    first = now.replace(day=1)
    prev = (first - timedelta(days=1)).replace(day=1)
    # current month's dump when it exists yet, else last month's
    return (f"data%2F{first.year}%2Fdiscogs_{first:%Y%m%d}_masters.xml.gz"
            if now.day > 3 else
            f"data%2F{prev.year}%2Fdiscogs_{prev:%Y%m%d}_masters.xml.gz")


def _fresh() -> bool:
    if not STYLES_DB_PATH.exists():
        return False
    age = datetime.utcnow() - datetime.fromtimestamp(STYLES_DB_PATH.stat().st_mtime)
    if age >= timedelta(days=MAX_AGE_DAYS):
        return False
    try:
        con = sqlite3.connect(STYLES_DB_PATH)
        n = con.execute("SELECT COUNT(*) FROM artist_styles").fetchone()[0]
        con.close()
        return n > 0
    except Exception:
        return False


def _gaming() -> bool:
    try:
        from src.services.app_state import get_state
        return get_state("game_active") == "1"
    except Exception:
        return False


async def refresh_discogs_styles(max_bytes: int = None, force: bool = False) -> dict:
    """Stream the newest masters dump, filter to the owner's artists, and
    rebuild the styles DB. ``max_bytes`` caps the compressed read (test
    windows); production runs read the whole ~590 MB once a month."""
    if _fresh() and not force and max_bytes is None:
        return {"skipped": "fresh"}
    key = _latest_masters_key()
    if not key:
        return {"error": "no dump key"}
    wanted = _library_artist_norms()
    if not wanted:
        return {"error": "no library artists"}
    url = f"{DATA_INDEX}?download={key}"
    logger.info("[discogs] streaming %s for %d library artists", key, len(wanted))

    decomp = zlib.decompressobj(wbits=31)
    buf = b""
    read_c = 0
    masters: list[tuple] = []
    t0 = time.time()
    stopped = None
    async with httpx.AsyncClient(timeout=120, headers=UA,
                                 follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            if resp.status_code != 200:
                return {"error": f"HTTP {resp.status_code} (endpoint rate-limits; retry next tick)"}
            async for chunk in resp.aiter_bytes(1 << 19):
                read_c += len(chunk)
                if _gaming():
                    stopped = "game"
                    break
                try:
                    buf += decomp.decompress(chunk)
                except zlib.error:
                    stopped = "zlib"
                    break
                # scan complete <master> blocks; carry the tail over
                last_end = 0
                for m in _MASTER_RE.finditer(buf):
                    last_end = m.end()
                    block = m.group(2)
                    am = _ARTIST_NAME_RE.search(block)
                    if not am:
                        continue
                    a_norm = _norm(_unescape(am.group(1)))
                    if a_norm not in wanted:
                        continue
                    tm = _TITLE_RE.search(block)
                    ym = _YEAR_RE.search(block)
                    styles = [_unescape(x) for x in _STYLE_RE.findall(block)]
                    genres = [_unescape(x) for x in _GENRE_RE.findall(block)]
                    masters.append((
                        a_norm, _unescape(am.group(1)),
                        _unescape(tm.group(1)) if tm else "",
                        int(ym.group(1)) if ym else None,
                        json.dumps(genres), json.dumps(styles)))
                if last_end:
                    buf = buf[last_end:]
                if len(buf) > 8 << 20:      # runaway guard
                    buf = buf[-(4 << 20):]
                if max_bytes and read_c >= max_bytes:
                    stopped = "test window"
                    break

    STYLES_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(STYLES_DB_PATH)
    try:
        cur = con.cursor()
        cur.executescript("""
            DROP TABLE IF EXISTS master_styles;
            DROP TABLE IF EXISTS artist_styles;
            CREATE TABLE master_styles (
                artist_norm TEXT, artist TEXT, title TEXT, year INTEGER,
                genres TEXT, styles TEXT);
            CREATE TABLE artist_styles (artist_norm TEXT PRIMARY KEY, styles TEXT);
        """)
        cur.executemany("INSERT INTO master_styles VALUES (?,?,?,?,?,?)", masters)
        agg: dict[str, Counter] = {}
        for a_norm, _a, _t, _y, _g, styles_json in masters:
            agg.setdefault(a_norm, Counter()).update(json.loads(styles_json))
        cur.executemany(
            "INSERT INTO artist_styles VALUES (?,?)",
            [(a, json.dumps([s for s, _ in c.most_common(8)]))
             for a, c in agg.items()])
        cur.executescript("""
            CREATE INDEX idx_ms_artist ON master_styles(artist_norm);
        """)
        con.commit()
    finally:
        con.close()
    result = {"read_mb": round(read_c / 1e6, 1),
              "matched_masters": len(masters),
              "artists_with_styles": len({m[0] for m in masters}),
              "seconds": round(time.time() - t0, 1)}
    if stopped:
        result["stopped"] = stopped
    logger.info("[discogs] styles rebuilt: %s", result)
    return result


def artist_styles(artist: str) -> list[str]:
    """Sync read: the aggregated Discogs styles for one artist ([] if none)."""
    if not artist or not STYLES_DB_PATH.exists():
        return []
    try:
        con = sqlite3.connect(STYLES_DB_PATH)
        row = con.execute("SELECT styles FROM artist_styles WHERE artist_norm=?",
                          (_norm(artist),)).fetchone()
        con.close()
        return json.loads(row[0]) if row else []
    except Exception:
        return []
