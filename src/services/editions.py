"""
Curatarr — series editions: do we own the uncensored cut, does one exist?

Owner question 2026-09-14: "kann der Curatarr herausfinden von welchen Serien
wir eine uncensored Fassung besitzen und/oder für welche eine existiert?"
Measured before building: the prose the enrichment collects (reception,
significance, overview) mentions censorship for 13 of 2,443 anime and half
of those are plot ("Library War"); the AniDB tags of the offline snapshot
carry it as a fact. The tags, by AniDB's own definitions (checked on
2026-09-15 after the first walk had built on the wrong one):

  "tv censoring"                  the TV version is censored, the later
                                  DVD/BD releases are uncensored — the disc
                                  IS the uncensored cut (147 of the owner's
                                  series, 313 in the snapshot)
  "uncensored version available"  an uncensored version exists (rare: 10)
  "censored uncensored version"   even the DVD/BD release keeps censoring —
                                  no nipples (KissXSis), the steam stays
                                  (Kanamemo), sometimes more than the TV cut
                                  (32 of the owner's series, 57 in the snapshot)
  "excessive censoring", "violence censoring", "governmental censorship",
  "censored", "tv censored version"    the airing was censored

So "is the Blu-ray the uncensored cut?" is answered by AniDB per title, and
the cut on disk is read from Sonarr's episode files: the quality *source*
of every file (bluray / dvd against web / television), plus release names
and the owner's custom formats that say uncensored, uncut, unrated or
censored. Web rips carry the word (AT-X and HIDIVE streams), disc rips
rarely bother (63 of 493 Blu-ray files in the owner's library), so the
missing word on a Blu-ray rip means nothing.

Two sides, one row per series (series_editions):
  owned   episode files: how many come from a disc source, how many are
          named uncensored / censored, the custom format names, a sample.
  exists  the AniDB flags (anime only): an uncensored version exists, the
          airing was censored, the disc release still keeps censoring, and
          the censoring tags seen.

The walker (custodian task editions_sync, weekly, no LLM, one API call per
series, resumable) keeps the table; readers feed the curator's verified
block ("Edition: …"), the Curation view's upgrade list (broadcast or web
files on disk while AniDB says the disc release is uncensored) and an
on-demand release search through Sonarr's own indexers (season by season —
Sonarr's release endpoint searches per season; uncensored by name or from a
Blu-ray/DVD source).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from src.config import settings

logger = logging.getLogger(__name__)

# Release names join words with dots, spaces, brackets or underscores
# ("[UNCENSORED_BD_1080p]"); \b treats "_" as a word character, so the
# boundaries are spelled out as "no letter or digit next to it".
_UNCENSORED = re.compile(r"(?<![a-z0-9])(uncensored|uncut|unrated)(?![a-z0-9])", re.I)
_CENSORED = re.compile(r"(?<![a-z0-9])censored(?![a-z0-9])", re.I)
_DISC_NAME = re.compile(r"(?<![a-z0-9])(bd|bdrip|bdmv|bd-?remux|blu-?ray)(?![a-z0-9])", re.I)
_DISC_SOURCES = {"bluray", "blurayraw", "dvd"}     # Sonarr quality.quality.source, lowercased
_RECHECK_DAYS = 7
_BUDGET = 200                # series per run, one episodefile call each. Sonarr itself answers in
                             # milliseconds (178 calls in 7 s on the owner's LAN); the pace is our
                             # client's rate limiter — the walker asks for 60 requests a minute, so
                             # a tick spends ~3.5 min here, full coverage in ~15 ticks
_WALK_RPM = 60
_CURSOR_KEY = "editions_cursor"
TAG_TV_CENSORING = "tv censoring"
TAG_UNCENSORED_AVAILABLE = "uncensored version available"
TAG_DISC_CENSORED = "censored uncensored version"
TAGS_UNCENSORED_EXISTS = (TAG_TV_CENSORING, TAG_UNCENSORED_AVAILABLE)
TAGS_CENSORED = (TAG_TV_CENSORING, TAG_DISC_CENSORED, "censored", "excessive censoring",
                 "violence censoring", "governmental censorship", "tv censored version")
_NO_FLAGS = {"uncensored": False, "censored": False, "disc_censored": False, "tags": ""}


# ── classification ───────────────────────────────────────────────────────────

def _file_names(f: dict) -> list:
    return [str(f.get(k) or "") for k in ("sceneName", "relativePath", "originalFilePath", "path")]


def _cf_names(f: dict) -> list:
    return [str(c.get("name") or "") for c in (f.get("customFormats") or []) if isinstance(c, dict)]


def _source(f: dict) -> str:
    """Sonarr's parsed quality source of an episode file or a release:
    bluray, blurayRaw, dvd, web, webRip, television, unknown."""
    q = f.get("quality") or {}
    if isinstance(q, dict) and isinstance(q.get("quality"), dict):
        q = q["quality"]
    return str(q.get("source") or "").strip().lower() if isinstance(q, dict) else ""


def _is_disc(f: dict, text: str) -> bool:
    """A disc source by Sonarr's parsed quality; the name decides only when
    nothing was parsed."""
    src = _source(f)
    if src:
        return src in _DISC_SOURCES
    return bool(_DISC_NAME.search(text))


def classify_files(files: list) -> dict:
    """Count the episode files from a disc source (Blu-ray / DVD), those
    whose release name or custom formats say uncensored (uncut, unrated),
    and those that say censored; keep the custom format names seen and one
    sample release name."""
    n = unc = cen = disc = 0
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
        if _is_disc(f, " ".join(names)):
            disc += 1
        if not sample:
            sample = next((x for x in names if x), "")
    return {"episode_files": n, "files_disc": disc, "files_uncensored": unc, "files_censored": cen,
            "custom_formats": ", ".join(sorted(cfs))[:300], "sample_release": sample[:300]}


def anidb_flags(title: str, year: Optional[int] = None, *, lookup=None) -> dict:
    """AniDB's verdict from the tags of the offline snapshot: ``uncensored``
    (an uncensored version exists — for "tv censoring" that is the DVD/BD
    release), ``censored`` (the airing was censored), ``disc_censored``
    (even the disc release keeps censoring), ``tags`` (the censoring tags
    seen, comma-joined). ``lookup`` is injectable for tests."""
    try:
        if lookup is None:
            from src.services import anime_offline
            lookup = lambda **kw: anime_offline.lookup(**kw)  # noqa: E731
        off = lookup(title=title, year=year) or {}
    except Exception as e:
        logger.debug("[editions] anidb lookup failed for %r: %s", title, e)
        return dict(_NO_FLAGS)
    tags = {str(t).strip().lower() for t in (off.get("tags") or [])}
    seen = sorted(t for t in TAGS_CENSORED + (TAG_UNCENSORED_AVAILABLE,) if t in tags)
    return {"uncensored": any(t in tags for t in TAGS_UNCENSORED_EXISTS),
            "censored": any(t in tags for t in TAGS_CENSORED),
            "disc_censored": TAG_DISC_CENSORED in tags,
            "tags": ", ".join(seen)[:200]}


# ── the walker ───────────────────────────────────────────────────────────────

def _category(series: dict) -> str:
    try:
        from src.services.arr_client import classify_sonarr_category
        return classify_sonarr_category(series)
    except Exception:
        return "anime" if (series.get("seriesType") == "anime") else "show"


async def sync_editions(client, *, budget: Optional[int] = None, now: Optional[datetime] = None,
                        task=None, lookup=None, get_state=None, set_state=None) -> dict:
    """One run: every Sonarr series not checked within a week (rows from
    before the source count existed are stale too), in id order from the
    cursor, up to ``budget`` — one episodefile call each, the AniDB flags for
    anime, one upsert. Returns the numbers; done=False when the budget ran
    out (the cursor continues next run)."""
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
            SeriesEdition.service == "sonarr", SeriesEdition.checked_at >= fresh_before,
            SeriesEdition.files_disc.isnot(None)).all()}
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
        flags = anidb_flags(s.get("title") or "", s.get("year"), lookup=lookup) if cat == "anime" else dict(_NO_FLAGS)
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
            ed.anidb_uncensored = bool(flags["uncensored"])
            ed.anidb_censored = bool(flags["censored"])
            ed.anidb_disc_censored = bool(flags["disc_censored"])
            ed.anidb_tags = flags["tags"] or None
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
    client = SonarrClient(settings.SONARR_URL, settings.SONARR_API_KEY, rate_limit_rpm=_WALK_RPM)
    async with client:
        res = await sync_editions(client, task=task)
    logger.info("[editions] run: %s", res)
    return bool(res.get("done"))


# ── readers ──────────────────────────────────────────────────────────────────

def _row(ed) -> dict:
    return {"service": ed.service, "arr_id": ed.arr_id, "title": ed.title, "category": ed.category,
            "tvdb_id": ed.tvdb_id, "title_slug": ed.title_slug, "year": ed.year,
            "episode_files": ed.episode_files or 0, "files_disc": ed.files_disc,
            "files_uncensored": ed.files_uncensored or 0, "files_censored": ed.files_censored or 0,
            "custom_formats": ed.custom_formats or "", "sample_release": ed.sample_release or "",
            "anidb_uncensored": bool(ed.anidb_uncensored), "anidb_censored": bool(ed.anidb_censored),
            "anidb_disc_censored": bool(ed.anidb_disc_censored), "anidb_tags": ed.anidb_tags or "",
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


def _tags(ed: dict) -> set:
    return {t.strip() for t in str(ed.get("anidb_tags") or "").split(",") if t.strip()}


def _files_part(ed: dict) -> str:
    n, disc = ed["episode_files"], ed.get("files_disc")
    unit = "episode file" if n == 1 else "episode files"
    if disc is None:
        head = f"{n} {unit} on disk"
    elif n == 1:
        head = f"1 episode file from {'Blu-ray/DVD' if disc else 'broadcast or web'}"
    elif disc >= n:
        head = f"{n} {unit}, all from Blu-ray/DVD"
    elif disc == 0:
        head = f"{n} {unit}, all from broadcast or web"
    else:
        head = f"{n} {unit}, {disc} from Blu-ray/DVD and {n - disc} from broadcast or web"
    if ed["files_uncensored"]:
        return head + f", {ed['files_uncensored']} named uncensored"
    if ed["files_censored"]:
        return head + f", {ed['files_censored']} named censored"
    return head + ", none named uncensored"


def _anidb_part(ed: dict) -> Optional[str]:
    tags = _tags(ed)
    if ed["anidb_uncensored"] and (TAG_TV_CENSORING in tags or not tags):
        s = "AniDB: the TV airing was censored, the Blu-ray/DVD release is the uncensored cut"
        return s + " though it keeps some censoring" if ed.get("anidb_disc_censored") else s
    if ed["anidb_uncensored"]:
        return "AniDB: an uncensored version exists"
    if ed.get("anidb_disc_censored"):
        return "AniDB: even the Blu-ray/DVD release keeps some censoring; no uncensored version known"
    if ed["anidb_censored"]:
        return "AniDB: the airing was censored" + (f" ({', '.join(sorted(tags))})" if tags else "")
    return None


def edition_line(ed: Optional[dict]) -> Optional[str]:
    """One evidence line, or None when there is nothing to say: no files and
    no AniDB flag."""
    if not ed:
        return None
    parts = []
    if ed["episode_files"]:
        parts.append(_files_part(ed))
        cfs = [c for c in ed["custom_formats"].split(", ") if c and (_UNCENSORED.search(c) or _CENSORED.search(c))]
        if cfs:
            parts.append(f"custom formats: {', '.join(cfs)}")
    a = _anidb_part(ed)
    if a:
        parts.append(a)
    return "; ".join(parts) if parts else None


def upgrade_rows() -> list:
    """Series in the Curation upgrade list's shape: AniDB says the disc
    release is the uncensored cut, and broadcast or web files sit on disk
    with none of them named uncensored. Rows the walker has not re-read
    since the source count exists are left out. Largest TV-cut share first."""
    from src.database.connection import get_db_session
    from src.database.models import SeriesEdition
    base = str(settings.SONARR_URL or "").rstrip("/")
    out = []
    with get_db_session() as db:
        rows = (db.query(SeriesEdition).filter(SeriesEdition.anidb_uncensored == True,  # noqa: E712
                                               SeriesEdition.episode_files > 0,
                                               SeriesEdition.files_uncensored == 0,
                                               SeriesEdition.files_disc.isnot(None),
                                               SeriesEdition.files_disc < SeriesEdition.episode_files)
                .all())
        for ed in rows:
            e = _row(ed)
            n, disc = e["episode_files"], e["files_disc"] or 0
            tv = n - disc
            tag = TAG_TV_CENSORING if (TAG_TV_CENSORING in _tags(e) or not e["anidb_tags"]) else TAG_UNCENSORED_AVAILABLE
            claim = ("the TV airing was censored and the DVD/Blu-ray release is uncensored" if tag == TAG_TV_CENSORING
                     else "an uncensored version exists")
            why = (f"AniDB tags it '{tag}': {claim}; on disk {n} episode files, {tv} from broadcast or web, "
                   f"{disc} from Blu-ray/DVD, none named uncensored"
                   + (f" ({e['files_censored']} named censored)" if e["files_censored"] else "")
                   + ("; AniDB: the disc release keeps some censoring too" if e["anidb_disc_censored"] else "")
                   + (f"; sample: {e['sample_release']}" if e["sample_release"] else ""))
            weakness = ("TV cut — uncensored disc release exists" if disc == 0
                        else f"TV cut in {tv} of {n} files — uncensored disc release exists")
            out.append({"kind": "edition", "title": e["title"], "category": e["category"], "arr_id": e["arr_id"],
                        "size_gb": None, "weakness": weakness, "love_reason": why,
                        "arr_url": f"{base}/series/{e['title_slug']}" if base and e["title_slug"] else "",
                        "_share": tv / n if n else 0.0})
    out.sort(key=lambda r: (-r["_share"], str(r["title"] or "").lower()))
    for r in out:
        r.pop("_share", None)
    return out


def _release_reason(r: dict) -> Optional[str]:
    """Why a release counts as the uncensored cut: named so, or from a
    Blu-ray/DVD source (Sonarr's parsed quality, else the title). Releases
    named censored never count."""
    title = str(r.get("title") or "")
    cfs = [str(c.get("name") or "") for c in (r.get("customFormats") or []) if isinstance(c, dict)]
    text = " ".join([title] + cfs)
    if _UNCENSORED.search(text):
        return "named uncensored"
    if _CENSORED.search(text):
        return None
    if _is_disc(r, title):
        return "Blu-ray/DVD source"
    return None


async def check_releases(client, series_id: int, seasons=(1, 2)) -> list:
    """What the owner's indexers offer: releases for the given seasons that
    are uncensored by name or come from a Blu-ray/DVD source. Sonarr searches
    live, per season — keep the season list short."""
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
            why = _release_reason(r)
            if not why:
                continue
            title = str(r.get("title") or "")
            key = (title, r.get("indexer"))
            if key in seen:
                continue
            seen.add(key)
            cfs = [str(c.get("name") or "") for c in (r.get("customFormats") or []) if isinstance(c, dict)]
            out.append({"title": title, "indexer": r.get("indexer"), "season": season, "why": why,
                        "size_gb": round((r.get("size") or 0) / 1e9, 2), "seeders": r.get("seeders"),
                        "custom_formats": cfs})
    out.sort(key=lambda o: (o["why"] != "named uncensored", o["season"]))
    return out
