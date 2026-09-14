"""
Curatarr — lyrics from Plex (the SoulSync sidecars), step 1: collect.

Owner decision 2026-09-14: the curator judges music with the lyrics in
hand, but raw lyrics never reach a prompt or the UI. SoulSync drops .lrc /
.txt sidecars next to the tracks and trickles more in over time; Plex
indexes them as lyric streams (streamType 4, codec lrc / txt) and serves the
text at the stream's key. Probed on the owner's server 2026-09-14: 121 of
160 sampled tracks carried one (76 %), 141 lrc + 20 txt streams; a track
listing with ``includeElements=Stream`` returns the streams inline, so one
walk is ~47 page calls for 18,500 tracks plus one text fetch per new stream.

This module owns the FIRST walker (no LLM): list every track of every
music section, note its lyric stream, fetch each text once per
(track, stream key), keep the plain lines in the cache's own SQLite
(data/cache/lyrics.db — the main DB and its backups stay lean), and re-check
every track each run so the trickle lands. Tracks Plex no longer lists are
marked gone, but only after a COMPLETE listing (a broken page must never
look like a purge — same rule as the tech-profile prune).

The profile walker (step 2) reads this cache through ``artist_index`` /
``artist_tracks``; ``lyrics_coverage`` feeds the Knowledge Base line and the
custodian report.

Custodian contract: ``run_lyrics_sync(task=)`` returns True when the walk
finished within the fetch budget (the task then waits for its cadence) and
False when the budget ran out mid-walk (stays due, continues next tick).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Optional

from src.config import settings

logger = logging.getLogger(__name__)

LYRICS_DB_PATH = Path(str(settings.ENRICHMENT_CACHE)).parent / "lyrics.db"
_PAGE = 400                  # Plex container size per listing page
_FETCH_BUDGET = 4000         # stream texts per run (~100 s on the LAN); the rest continues next tick
_CONCURRENCY = 4             # parallel text fetches against the LAN server
_TIMEOUT = 30.0
_MAX_CHARS = 20_000          # per track; nothing sensible is longer
_RETRY_DAYS = 7              # a stream Plex 404s (sidecar moved by SoulSync, index not yet rescanned)
                             # is retried after this long, or at once when its key changes

ListTracks = Callable[[str], Awaitable[Optional[list]]]     # section key -> tracks, None = listing broke
FetchText = Callable[[str], Awaitable[Optional[bytes]]]     # stream key -> bytes, None = fetch failed


# ── storage ──────────────────────────────────────────────────────────────────

def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    p = Path(db_path or LYRICS_DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    con.executescript("""
        CREATE TABLE IF NOT EXISTS track_lyrics (
            plex_rating_key TEXT PRIMARY KEY,
            artist_key      TEXT,
            album_key       TEXT,
            artist          TEXT,
            album           TEXT,
            title           TEXT,
            duration_ms     INTEGER,
            stream_key      TEXT,               -- /library/streams/<id>, NULL = no lyric stream
            fmt             TEXT,               -- lrc / txt
            text            TEXT,               -- plain lines joined by newlines
            lines           INTEGER NOT NULL DEFAULT 0,
            checked_at      TEXT NOT NULL,      -- last listing that saw the track
            fetched_at      TEXT,
            failed_at       TEXT,               -- last fetch Plex refused (404: sidecar moved)
            gone            INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS ix_track_lyrics_artist ON track_lyrics(artist_key);
        CREATE TABLE IF NOT EXISTS lyrics_meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    return con


# ── LRC / txt parsing ────────────────────────────────────────────────────────

_TS = re.compile(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]")       # [mm:ss.xx] — several per line happen
_WORD_TS = re.compile(r"<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>")    # enhanced LRC word timing
_META = re.compile(r"^\[(?:ar|ti|al|au|by|re|ve|la|length|offset|tool|#)[^\]]*\]\s*$", re.I)


def _decode(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


def parse_lyrics(raw, fmt: Optional[str] = None) -> list:
    """Plain lines out of an LRC or txt payload: timestamps and header tags
    gone, whitespace trimmed, empty lines dropped. ``fmt`` is a hint only —
    a txt stream may still carry LRC tags and vice versa, so both are
    always handled."""
    text = _decode(raw) if isinstance(raw, bytes) else (raw or "")
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line or _META.match(line):
            continue
        line = _WORD_TS.sub("", _TS.sub("", line)).strip()
        if line:
            out.append(line)
    return out


def _lyric_stream(item: dict) -> tuple:
    """(stream key, format) of the track's lyric stream, lrc preferred over
    txt when both exist; (None, None) without one."""
    found = []
    for media in item.get("Media") or []:
        for part in media.get("Part") or []:
            for st in part.get("Stream") or []:
                if st.get("streamType") == 4 and st.get("key"):
                    found.append((st["key"], (st.get("codec") or st.get("format") or "").lower() or None))
    if not found:
        return None, None
    return next((f for f in found if f[1] == "lrc"), found[0])


# ── the walk ─────────────────────────────────────────────────────────────────

async def sync_lyrics(sections: list, list_tracks: ListTracks, fetch_text: FetchText, *,
                      db_path: Optional[Path] = None, budget: Optional[int] = None,
                      task=None, now: Optional[datetime] = None) -> dict:
    """One run over ``sections`` ([(key, title), ...]). Lists every track,
    upserts its identity and stream, fetches the text of every new or
    changed stream up to ``budget``, drops text whose stream vanished, and
    marks tracks a COMPLETE listing no longer contains as gone. A stream
    Plex refused is left alone for ``_RETRY_DAYS`` unless its key changes.
    Returns the run's numbers; ``done`` is False when the budget ran out."""
    now = now or datetime.utcnow()
    stamp = now.isoformat()
    budget = _FETCH_BUDGET if budget is None else budget
    retry_before = (now - timedelta(days=_RETRY_DAYS)).isoformat()
    con = _connect(db_path)
    try:
        seen = 0
        to_fetch = []
        complete = bool(sections)
        for sec_key, sec_title in sections:
            items = await list_tracks(sec_key)
            if items is None:
                complete = False
                logger.warning("[lyrics] listing of section %r broke — nothing is marked gone this run", sec_title)
                continue
            for it in items:
                key = str(it.get("ratingKey") or "")
                if not key:
                    continue
                seen += 1
                skey, fmt = _lyric_stream(it)
                row = con.execute("SELECT stream_key, text, failed_at FROM track_lyrics WHERE plex_rating_key=?",
                                  (key,)).fetchone()
                changed = row is not None and (row["stream_key"] or None) != skey
                con.execute(
                    """INSERT INTO track_lyrics (plex_rating_key, artist_key, album_key, artist, album, title,
                                                 duration_ms, stream_key, fmt, checked_at, gone)
                       VALUES (?,?,?,?,?,?,?,?,?,?,0)
                       ON CONFLICT(plex_rating_key) DO UPDATE SET
                           artist_key=excluded.artist_key, album_key=excluded.album_key,
                           artist=excluded.artist, album=excluded.album, title=excluded.title,
                           duration_ms=excluded.duration_ms, stream_key=excluded.stream_key,
                           fmt=excluded.fmt, checked_at=excluded.checked_at, gone=0""",
                    (key, str(it.get("grandparentRatingKey") or ""), str(it.get("parentRatingKey") or ""),
                     it.get("grandparentTitle"), it.get("parentTitle"), it.get("title"),
                     it.get("duration"), skey, fmt, stamp))
                if changed:
                    # a new sidecar replaced the old one, or it went away:
                    # never serve the previous text as if it were current
                    con.execute("UPDATE track_lyrics SET text=NULL, lines=0, fetched_at=NULL, failed_at=NULL "
                                "WHERE plex_rating_key=?", (key,))
                if skey and (row is None or changed
                             or (row["text"] is None
                                 and (row["failed_at"] is None or row["failed_at"] < retry_before))):
                    to_fetch.append((key, skey, fmt))
        con.commit()

        batch = to_fetch[:budget]
        fetched = failed = 0
        sem = asyncio.Semaphore(_CONCURRENCY)

        def _report(msg: str):
            if task is None:
                return
            try:
                from src.services.task_monitor import task_monitor
                task_monitor.update(task, processed=fetched + failed, total=len(batch), message=msg)
            except Exception:
                pass

        async def _one(key, skey, fmt):
            nonlocal fetched, failed
            async with sem:
                raw = await fetch_text(skey)
            if raw is None:
                failed += 1
                con.execute("UPDATE track_lyrics SET failed_at=? WHERE plex_rating_key=?", (stamp, key))
                return
            lines = parse_lyrics(raw, fmt)
            text = "\n".join(lines)[:_MAX_CHARS]
            con.execute("UPDATE track_lyrics SET text=?, lines=?, fetched_at=?, failed_at=NULL "
                        "WHERE plex_rating_key=?", (text, len(lines), stamp, key))
            fetched += 1

        _report(f"{seen:,} tracks listed, {len(to_fetch):,} lyric streams to fetch")
        for i in range(0, len(batch), 50):
            await asyncio.gather(*(_one(*t) for t in batch[i:i + 50]))
            con.commit()
            _report(f"{fetched + failed:,}/{len(batch):,} lyric streams fetched this run")

        gone = 0
        if complete:
            gone = con.execute("UPDATE track_lyrics SET gone=1 WHERE checked_at <> ? AND gone=0", (stamp,)).rowcount
        result = {
            "listed": seen, "with_stream": seen and con.execute(
                "SELECT COUNT(*) FROM track_lyrics WHERE gone=0 AND stream_key IS NOT NULL").fetchone()[0],
            "fetched": fetched, "failed": failed, "pending": len(to_fetch) - len(batch),
            "gone_marked": gone, "complete": complete, "done": len(to_fetch) <= budget,
        }
        con.execute("INSERT OR REPLACE INTO lyrics_meta (key, value) VALUES ('last_run', ?)", (stamp,))
        con.execute("INSERT OR REPLACE INTO lyrics_meta (key, value) VALUES ('last_result', ?)",
                    (json.dumps(result),))
        con.commit()
        return result
    finally:
        con.close()


# ── reads for the profile walker, the KB line and the custodian report ──────

def lyrics_coverage(db_path: Optional[Path] = None) -> dict:
    con = _connect(db_path)
    try:
        r = con.execute("""
            SELECT COUNT(*) AS tracks,
                   SUM(CASE WHEN stream_key IS NOT NULL THEN 1 ELSE 0 END) AS with_stream,
                   SUM(CASE WHEN lines > 0 THEN 1 ELSE 0 END) AS with_text,
                   SUM(CASE WHEN stream_key IS NOT NULL AND text IS NULL AND failed_at IS NOT NULL
                            THEN 1 ELSE 0 END) AS unreachable,
                   COUNT(DISTINCT artist_key) AS artists,
                   COUNT(DISTINCT CASE WHEN lines > 0 THEN artist_key END) AS artists_with_text
            FROM track_lyrics WHERE gone=0""").fetchone()
        meta = {m["key"]: m["value"] for m in con.execute("SELECT key, value FROM lyrics_meta")}
        return {
            "tracks": r["tracks"] or 0, "with_stream": r["with_stream"] or 0, "with_text": r["with_text"] or 0,
            "unreachable": r["unreachable"] or 0,
            "artists": r["artists"] or 0, "artists_with_text": r["artists_with_text"] or 0,
            "last_run": meta.get("last_run"),
            "last_result": json.loads(meta["last_result"]) if meta.get("last_result") else None,
        }
    finally:
        con.close()


def artist_index(db_path: Optional[Path] = None) -> list:
    """Per artist: key, name, tracks on file, tracks with text — the profile
    walker's eligibility input."""
    con = _connect(db_path)
    try:
        return [dict(r) for r in con.execute("""
            SELECT artist_key, MAX(artist) AS artist, COUNT(*) AS tracks,
                   SUM(CASE WHEN lines > 0 THEN 1 ELSE 0 END) AS with_text
            FROM track_lyrics WHERE gone=0 AND artist_key <> ''
            GROUP BY artist_key ORDER BY artist""")]
    finally:
        con.close()


def artist_tracks(artist_key: str, db_path: Optional[Path] = None) -> list:
    """The artist's tracks that have text, album by album."""
    con = _connect(db_path)
    try:
        return [dict(r) for r in con.execute("""
            SELECT plex_rating_key, album_key, album, title, lines, text, duration_ms
            FROM track_lyrics WHERE gone=0 AND artist_key=? AND lines > 0
            ORDER BY album, title""", (artist_key,))]
    finally:
        con.close()


# ── the custodian runner (live Plex) ────────────────────────────────────────

def _music_sections() -> list:
    """Music sections the owner mapped in Libraries (media_category music)."""
    from src.database.connection import get_db_session
    from src.database.models import LibraryConfig
    with get_db_session() as db:
        return [(c.plex_section_key, c.plex_section_title)
                for c in db.query(LibraryConfig).filter(LibraryConfig.media_category == "music").all()]


async def _discover_music_sections(client, base: str, headers: dict) -> list:
    """Every Plex section of type artist. The sidecars live in Plex whether
    or not the owner mapped the section in Libraries (the owner's own
    instance maps movies, anime and shows only — 2026-09-14), so the
    collector must not depend on that mapping."""
    try:
        r = await client.get(f"{base}/library/sections", headers=headers)
    except Exception as e:
        logger.warning("[lyrics] section discovery failed: %s", e)
        return []
    if r.status_code != 200:
        logger.warning("[lyrics] section discovery: HTTP %s", r.status_code)
        return []
    dirs = r.json().get("MediaContainer", {}).get("Directory", []) or []
    return [(str(d.get("key")), d.get("title") or "") for d in dirs
            if d.get("type") == "artist" and d.get("key")]


async def run_lyrics_sync(task=None) -> bool:
    """Collect from the Plex music sections — the mapped ones, else every
    artist-type section Plex has. True = the walk finished (or there is
    nothing to walk); False = the fetch budget ran out."""
    import httpx

    base, token = settings.effective_plex_url, settings.effective_plex_token
    if not base or not token:
        logger.info("[lyrics] no Plex configured — nothing to collect")
        return True
    headers = {"Accept": "application/json", "X-Plex-Token": token,
               "X-Plex-Client-Identifier": settings.PLEX_CLIENT_ID}

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        sections = _music_sections()
        if not sections:
            sections = await _discover_music_sections(client, base, headers)
            if sections:
                logger.info("[lyrics] no music section mapped in Libraries — using Plex's own: %s",
                            ", ".join(t for _, t in sections))
        if not sections:
            logger.info("[lyrics] Plex has no music section — nothing to collect")
            return True

        async def list_tracks(sec_key: str):
            out, start = [], 0
            while True:
                try:
                    r = await client.get(
                        f"{base}/library/sections/{sec_key}/all",
                        headers={**headers, "X-Plex-Container-Start": str(start),
                                 "X-Plex-Container-Size": str(_PAGE)},
                        params={"type": 10, "includeElements": "Stream"})
                except Exception as e:
                    logger.warning("[lyrics] listing %s at %d failed: %s", sec_key, start, e)
                    return None
                if r.status_code != 200:
                    logger.warning("[lyrics] listing %s at %d: HTTP %s", sec_key, start, r.status_code)
                    return None
                items = r.json().get("MediaContainer", {}).get("Metadata", []) or []
                out.extend(items)
                if len(items) < _PAGE:
                    return out
                start += _PAGE

        async def fetch_text(stream_key: str):
            try:
                r = await client.get(f"{base}{stream_key}", headers={"X-Plex-Token": token})
            except Exception as e:
                logger.debug("[lyrics] stream %s failed: %s", stream_key, e)
                return None
            return r.content if r.status_code == 200 else None

        res = await sync_lyrics(sections, list_tracks, fetch_text, task=task)
    logger.info("[lyrics] run: %s", res)
    return bool(res.get("done"))
