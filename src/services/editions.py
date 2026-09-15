"""
Curatarr — series editions: do we own the uncensored cut, does one exist?

Owner question 2026-09-14: "kann der Curatarr herausfinden von welchen Serien
wir eine uncensored Fassung besitzen und/oder für welche eine existiert?"
Measured before building: the prose the enrichment collects (reception,
significance, overview) mentions censorship for 13 of 2,443 anime and half
of those are plot ("Library War"); the AniDB tags from the offline snapshot
carry it as a fact for 37 — "censored uncensored version" (30) means the TV
airing was censored and an uncensored version exists, "excessive censoring"
(7) and "censored" say the airing was cut. Ownership is in Sonarr: every
episode file carries its release name, quality and the Custom Formats the
owner defined ("Uncensored" in the TRaSH style tags the files already).

Two sides, one row per series (series_editions):
  owned   from Sonarr's episode files: names (release / path) that say
          uncensored, uncut or unrated; names that say censored; the custom
          format names seen; one sample release name.
  exists  from the AniDB tags (anime only): uncensored version exists;
          the airing was censored.

The walker (custodian task editions_sync, weekly, no LLM, one API call per
series, resumable) keeps the table; readers feed the curator's verified
block ("Edition: …"), the Curation view's upgrade list (owned = the TV cut,
AniDB says an uncensored version exists) and an on-demand release search
through Sonarr's own indexers (season by season — Sonarr's release endpoint
searches per season).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from src.config import settings

logger = logging.getLogger(__name__)

_UNCENSORED = re.compile(r"\b(uncensored|uncut|unrated)\b", re.I)
_CENSORED = re.compile(r"(?<![a-z])(?<!un)censored\b", re.I)
_RECHECK_DAYS = 7
_BUDGET = 400                # series per run: one episodefile call each
_CURSOR_KEY = "editions_cursor"
TAG_UNCENSORED_EXISTS = "censored uncensored version"
TAGS_CENSORED = ("censored", "excessive censoring", "censored uncensored version")


# ── classification ───────────────────────────────────────────────────────────

def _file_names(f: dict) -> list:
    return [str(f.get(k) or "") for k in ("sceneName", "relativePath", "originalFilePath", "path")]


def _cf_names(f: dict) -> list:
    return [str(c.get("name") or "") for c in (f.get("customFormats") or []) if isinstance(c, dict)]


def classify_files(files: list) -> dict:
    """Count the episode files whose release name or custom formats say
    uncensored (uncut, unrated), and those that say censored; keep the custom
    format names seen and one sample release name."""
    n = unc = cen = 0
    cfs, sample = set(), ""
    for f in files or []:
        if not isinstance(f, dict):
            continue
        n += 1
        names = _file_names(f)
        cf = _cf_names(f)
        cfs.update(c for c in cf if c)
        text = " ".join(names + cf)
        if _UNCENSORED.search(text):
            unc += 1
        elif _CENSORED.search(text):
            cen += 1
        if not sample:
            sample = next((x for x in names if x), "")
    return {"episode_files": n, "files_uncensored": unc, "files_censored": cen,
            "custom_formats": ", ".join(sorted(cfs))[:300], "sample_release": sample[:300]}


def anidb_flags(title: str, year: Optional[int] = None, *, lookup=None) -> tuple:
    """(uncensored version exists, the airing was censored) from the AniDB
    tags of the offline snapshot. ``lookup`` is injectable for tests."""
    try:
        if lookup is None:
            from src.services import anime_offline
            lookup = lambda **kw: anime_offline.lookup(**kw)  # noqa: E731
        off = lookup(title=title, year=year) or {}
    except Exception as e:
        logger.debug("[editions] anidb lookup failed for %r: %s", title, e)
        return False, False
    tags = {str(t).strip().lower() for t in (off.get("tags") or [])}
    return TAG_UNCENSORED_EXISTS in tags, any(t in tags for t in TAGS_CENSORED)


# ── the walker ───────────────────────────────────────────────────────────────

def _category(series: dict) -> str:
    try:
        from src.services.arr_client import classify_sonarr_category
        return classify_sonarr_category(series)
    except Exception:
        return "anime" if (series.get("seriesType") == "anime") else "show"


async def sync_editions(client, *, budget: Optional[int] = None, now: Optional[datetime] = None,
                        task=None, lookup=None, get_state=None, set_state=None) -> dict:
    """One run: every Sonarr series not checked within a week, in id order
    from the cursor, up to ``budget`` — one episodefile call each, the AniDB
    flags for anime, one upsert. Returns the numbers; done=False when the
    budget ran out (the cursor continues next run)."""
    from src.database.connection import get_db_session
    from src.database.models import SeriesEdition
    now = now or datetime.utcnow()
    budget = _BUDGET if budget is None else budget
    if get_state is None or set_state is None:
        from src.services.app_state import get_state as _gs, set_state as _ss
        get_state, set_state = get_state or _gs, set_state or _ss
    series = await client.get_series()
    series = sorted((s for s in series or [] if isinstance(s, dict) and s.get("id")), key=lambda s: int(s["id"]))
    try:
        cursor = int(get_state(_CURSOR_KEY) or 0)
    except Exception:
        cursor = 0
    fresh_before = now - timedelta(days=_RECHECK_DAYS)
    with get_db_session() as db:
        fresh = {r.arr_id for r in db.query(SeriesEdition).filter(
            SeriesEdition.service == "sonarr", SeriesEdition.checked_at >= fresh_before).all()}
    todo = [s for s in series if int(s["id"]) > cursor and int(s["id"]) not in fresh]
    batch, rest = todo[:budget], todo[budget:]
    checked = failed = 0
    last_id = cursor

    def _report(msg):
        if task is None:
            return
        try:
            from src.services.task_monitor import task_monitor
            task_monitor.update(task, processed=checked + failed, total=len(batch), message=msg)
        except Exception:
            pass

    _report(f"{len(series):,} series, {len(todo):,} to check")
    for s in batch:
        sid = int(s["id"])
        try:
            files = await client.get_episode_files(sid)
        except Exception as e:
            failed += 1
            logger.debug("[editions] episodefile %s failed: %s", sid, e)
            last_id = sid
            continue
        row = classify_files(files)
        cat = _category(s)
        exists = censored = False
        if cat == "anime":
            exists, censored = anidb_flags(s.get("title") or "", s.get("year"), lookup=lookup)
        with get_db_session() as db:
            ed = db.query(SeriesEdition).filter(SeriesEdition.service == "sonarr", SeriesEdition.arr_id == sid).first()
            if ed is None:
                ed = SeriesEdition(service="sonarr", arr_id=sid)
                db.add(ed)
            ed.title = s.get("title")
            ed.category = cat
            ed.tvdb_id = s.get("tvdbId")
            ed.title_slug = s.get("titleSlug")
            ed.year = s.get("year")
            for k, v in row.items():
                setattr(ed, k, v)
            ed.anidb_uncensored = bool(exists)
            ed.anidb_censored = bool(censored)
            ed.checked_at = now
        checked += 1
        last_id = sid
        if checked % 25 == 0:
            _report(f"{checked}/{len(batch)} series checked")
    done = not rest
    set_state(_CURSOR_KEY, "0" if done else str(last_id))
    _report(f"{checked} checked, {failed} failed, {len(rest)} left")
    return {"series": len(series), "todo": len(todo), "checked": checked, "failed": failed,
            "left": len(rest), "done": done}


async def run_editions_sync(task=None) -> bool:
    if not (settings.SONARR_URL and settings.SONARR_API_KEY):
        logger.info("[editions] Sonarr not configured — nothing to check")
        return True
    from src.services.arr_client import SonarrClient
    client = SonarrClient(settings.SONARR_URL, settings.SONARR_API_KEY)
    async with client:
        res = await sync_editions(client, task=task)
    logger.info("[editions] run: %s", res)
    return bool(res.get("done"))


# ── readers ──────────────────────────────────────────────────────────────────

def _row(ed) -> dict:
    return {"service": ed.service, "arr_id": ed.arr_id, "title": ed.title, "category": ed.category,
            "tvdb_id": ed.tvdb_id, "title_slug": ed.title_slug, "year": ed.year,
            "episode_files": ed.episode_files or 0, "files_uncensored": ed.files_uncensored or 0,
            "files_censored": ed.files_censored or 0, "custom_formats": ed.custom_formats or "",
            "sample_release": ed.sample_release or "", "anidb_uncensored": bool(ed.anidb_uncensored),
            "anidb_censored": bool(ed.anidb_censored),
            "checked_at": ed.checked_at.isoformat() if ed.checked_at else None}


def edition_for(arr_id: int, service: str = "sonarr") -> Optional[dict]:
    from src.database.connection import get_db_session
    from src.database.models import SeriesEdition
    with get_db_session() as db:
        ed = db.query(SeriesEdition).filter(SeriesEdition.service == service, SeriesEdition.arr_id == int(arr_id)).first()
        return _row(ed) if ed else None


def edition_by_title(title: str, category: Optional[str] = None) -> Optional[dict]:
    from sqlalchemy import func
    from src.database.connection import get_db_session
    from src.database.models import SeriesEdition
    if not title:
        return None
    with get_db_session() as db:
        q = db.query(SeriesEdition).filter(func.lower(SeriesEdition.title) == title.strip().lower())
        if category in ("show", "anime"):
            q = q.filter(SeriesEdition.category == category)
        ed = q.first()
        return _row(ed) if ed else None


def edition_line(ed: Optional[dict]) -> Optional[str]:
    """One evidence line, or None when there is nothing to say: no files and
    no AniDB flag."""
    if not ed:
        return None
    n = ed["episode_files"]
    parts = []
    if n:
        if ed["files_uncensored"]:
            parts.append(f"{ed['files_uncensored']} of {n} episode files named uncensored")
        elif ed["files_censored"]:
            parts.append(f"{ed['files_censored']} of {n} episode files named censored (the TV cut)")
        else:
            parts.append(f"{n} episode files on disk, none named uncensored")
        if ed["custom_formats"]:
            parts.append(f"custom formats: {ed['custom_formats']}")
    if ed["anidb_uncensored"]:
        parts.append("AniDB: the TV airing was censored and an uncensored version exists")
    elif ed["anidb_censored"]:
        parts.append("AniDB: the airing was censored")
    if not parts:
        return None
    return "; ".join(parts)


def upgrade_rows() -> list:
    """Series in the Curation upgrade list's shape: AniDB says an uncensored
    version exists and none of the files on disk is named uncensored."""
    from src.database.connection import get_db_session
    from src.database.models import SeriesEdition
    base = str(settings.SONARR_URL or "").rstrip("/")
    out = []
    with get_db_session() as db:
        rows = (db.query(SeriesEdition).filter(SeriesEdition.anidb_uncensored == True,  # noqa: E712
                                               SeriesEdition.episode_files > 0,
                                               SeriesEdition.files_uncensored == 0)
                .order_by(SeriesEdition.title).all())
        for ed in rows:
            e = _row(ed)
            why = (f"AniDB tags it '{TAG_UNCENSORED_EXISTS}': the TV airing was censored and an uncensored "
                   f"version exists; on disk {e['episode_files']} episode files, none named uncensored"
                   + (f" ({e['files_censored']} named censored)" if e["files_censored"] else "")
                   + (f"; sample: {e['sample_release']}" if e["sample_release"] else ""))
            out.append({"kind": "edition", "title": e["title"], "category": e["category"], "arr_id": e["arr_id"],
                        "size_gb": None, "weakness": "TV cut — uncensored version exists",
                        "love_reason": why,
                        "arr_url": f"{base}/series/{e['title_slug']}" if base and e["title_slug"] else ""})
    return out


async def check_releases(client, series_id: int, seasons=(1, 2)) -> list:
    """What the owner's indexers offer: releases for the given seasons whose
    title or custom formats say uncensored. Sonarr searches live, per
    season — keep the season list short."""
    out, seen = [], set()
    for season in seasons:
        try:
            rels = await client.search_releases(int(series_id), int(season))
        except Exception as e:
            logger.debug("[editions] release search %s s%s failed: %s", series_id, season, e)
            continue
        for r in rels or []:
            if not isinstance(r, dict):
                continue
            title = str(r.get("title") or "")
            cfs = [str(c.get("name") or "") for c in (r.get("customFormats") or []) if isinstance(c, dict)]
            if not _UNCENSORED.search(" ".join([title] + cfs)):
                continue
            key = (title, r.get("indexer"))
            if key in seen:
                continue
            seen.add(key)
            out.append({"title": title, "indexer": r.get("indexer"), "season": season,
                        "size_gb": round((r.get("size") or 0) / 1e9, 2), "seeders": r.get("seeders"),
                        "custom_formats": cfs})
    return out
