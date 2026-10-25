"""Netflix viewing-history import for household members (2026-10-25).

A Netflix play counts like a Plex play: the same watch_history row, with
``source="netflix"``. Two exports exist, both are read:

* the full data export ("Download your personal information"): a zip whose
  ``CONTENT_INTERACTION/ViewingActivity.csv`` holds every profile's
  sessions - profile, start time (UTC), how long was watched, the
  bookmark, the device, and trailers / previews marked as such;
* the profile page's "Download all" (``NetflixViewingHistory.csv``): one
  profile, title and date only, in the account's locale. Its rows say a
  title was watched that day - not when, not for how long
  (``date_only``).

A Netflix title is one string: "Show: Season 1: Episode (Episode 3)" for
an episode, the plain title for a film. It is split here; the library
matcher (services/netflix_match) then puts the play on the Plex item when
the title is in the library.
"""
import csv
import hashlib
import io
import logging
import re
import zipfile
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Optional

from src.paths import ROOT

logger = logging.getLogger(__name__)

IMPORT_DIR = ROOT / "data" / "imports" / "netflix"
MAX_MEMBER_BYTES = 100 * 1024 * 1024
BATCH_SIZE = 5000
# Shorter than this is a trailer that played on its own, a preview while
# browsing, or a start that was abandoned at once - not a viewing.
MIN_WATCH_MS = 120_000
# Without the title's runtime (not in the library): a session counts as
# watched through when its bookmark got this far.
_DONE_EST_MS = {"movie": 75 * 60_000, "show": 18 * 60_000}

FULL = "full"
SIMPLE = "simple"
_FULL_MEMBER = re.compile(r"(^|/)ViewingActivity\.csv$", re.I)

# ── the title ───────────────────────────────────────────────────────────────

_EP_NO = re.compile(r"\s*\((?:Episode|Folge|Épisode|Episodio|Aflevering|Afsnit|Odcinek)\s+(\d+)\)\s*$", re.I)
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
              "nine": 9, "ten": 10, "eins": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5}
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}
_SEASON = re.compile(
    r"^(?:Season|Staffel|Saison|Temporada|Stagione|Seizoen|Sæson|Series|Serie|Part|Teil|Volume|Vol\.|"
    r"Book|Buch|Collection)\s+(\d+|[IVX]+|[A-Za-zü]+)$", re.I)
_LIMITED = re.compile(r"^(?:Limited Series|Miniseries|Mini-Series|Miniserie|Mini-Serie)$", re.I)


def _season_no(word: str) -> Optional[int]:
    w = word.lower()
    if w.isdigit():
        return int(w)
    return _ROMAN.get(w) or _NUM_WORDS.get(w)


def parse_title(raw: str) -> dict:
    """{"media_type", "series_title", "season", "episode", "title"} from one
    Netflix title. A film keeps its whole title, colons and all; a title is
    an episode when a part names a season ("Season 2", "Staffel 2",
    "Limited Series") or the episode number is given, or when it has three
    parts or more ("Show: Custom Season Name: Episode")."""
    raw = (raw or "").strip()
    ep = None
    m = _EP_NO.search(raw)
    if m:
        ep = int(m.group(1))
        raw_core = raw[:m.start()].rstrip()
    else:
        raw_core = raw
    parts = [p.strip() for p in raw_core.split(": ")]
    for i in range(1, len(parts)):
        sm = _SEASON.match(parts[i])
        season = _season_no(sm.group(1)) if sm else (1 if _LIMITED.match(parts[i]) else None)
        if season is not None:
            return {"media_type": "show", "series_title": ": ".join(parts[:i]), "season": season,
                    "episode": ep, "title": ": ".join(parts[i + 1:]) or raw_core}
    if ep is not None and len(parts) >= 2:
        return {"media_type": "show", "series_title": parts[0], "season": None, "episode": ep,
                "title": ": ".join(parts[1:])}
    if len(parts) >= 3:
        return {"media_type": "show", "series_title": parts[0], "season": None, "episode": ep,
                "title": parts[-1]}
    return {"media_type": "movie", "series_title": None, "season": None, "episode": None, "title": raw_core}


def title_ref(raw: str) -> str:
    """The Netflix title's own id here: stable across re-imports, the
    placeholder ``plex_item_id`` until the matcher finds the library item."""
    return "netflix:" + hashlib.sha1((raw or "").strip().casefold().encode("utf-8"), usedforsecurity=False).hexdigest()[:24]


# ── the files ───────────────────────────────────────────────────────────────

def _hms(s: str) -> Optional[int]:
    """'01:02:03' -> ms; None when it is not a duration ("Not latest view")."""
    m = re.fullmatch(r"\s*(\d+):(\d{2}):(\d{2})\s*", s or "")
    if not m:
        return None
    h, mi, se = map(int, m.groups())
    return ((h * 60 + mi) * 60 + se) * 1000


def kind_of(text: str) -> Optional[str]:
    """FULL / SIMPLE from a CSV's header line, else None."""
    head = (text.lstrip("﻿").splitlines() or [""])[0].lower()
    if "profile name" in head and "start time" in head and "title" in head:
        return FULL
    if head.replace('"', "").replace(" ", "").startswith("title,date"):
        return SIMPLE
    return None


_DATE_FORMATS = {".": ("%d.%m.%y", "%d.%m.%Y"), "-": ("%Y-%m-%d", "%d-%m-%Y", "%d-%m-%y"),
                 "/": ("%m/%d/%y", "%d/%m/%y", "%m/%d/%Y", "%d/%m/%Y")}


def _parse_with(fmt: str, dates: list) -> Optional[list]:
    out = []
    for d in dates:
        try:
            out.append(datetime.strptime(d.strip(), fmt))
        except ValueError:
            return None
    return out


def date_format(dates: list) -> Optional[str]:
    """The strptime format of a simple export's dates. Several may read
    every date (10/09/26); the file lists the newest first, so the one with
    the fewest steps forward in time wins."""
    dates = [d for d in dates if d and d.strip()]
    if not dates:
        return None
    sep = next((c for c in "./-" if c in dates[0]), None)
    best, best_bad = None, None
    for fmt in _DATE_FORMATS.get(sep, ()):
        parsed = _parse_with(fmt, dates)
        if parsed is None:
            continue
        bad = sum(1 for a, b in zip(parsed, parsed[1:]) if b > a)
        if best is None or bad < best_bad:
            best, best_bad = fmt, bad
    return best


def read_rows(text: str, name: str = "") -> dict:
    """{"kind", "profiles": {name: count}, "rows": [play dicts], "dropped":
    {why: n}} from one CSV's text. Trailers, previews and sessions under
    MIN_WATCH_MS are dropped (counted). A simple export is one profile
    with no name in it: the file's ``name`` stands for it."""
    kind = kind_of(text)
    out = {"kind": kind, "profiles": {}, "rows": [], "dropped": {}}
    if kind is None:
        return out
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))

    def _drop(why):
        out["dropped"][why] = out["dropped"].get(why, 0) + 1

    if kind == SIMPLE:
        items = [(r.get("Title") or "", r.get("Date") or "") for r in reader]
        fmt = date_format([d for _, d in items])
        if fmt is None:
            out["error"] = "the dates could not be read"
            return out
        for raw, d in items:
            if not raw.strip():
                _drop("no title")
                continue
            at = datetime.strptime(d.strip(), fmt).replace(hour=12)
            out["profiles"][name] = out["profiles"].get(name, 0) + 1
            out["rows"].append({"profile": name, "raw_title": raw.strip(), **parse_title(raw),
                                "viewed_at": at, "watched_ms": None, "bookmark_ms": None,
                                "device": None, "date_only": True})
        return out
    for r in reader:
        raw = (r.get("Title") or "").strip()
        if (r.get("Supplemental Video Type") or "").strip():
            _drop("trailer or preview")
            continue
        try:
            at = datetime.strptime((r.get("Start Time") or "").strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            _drop("unreadable time")
            continue
        watched = _hms(r.get("Duration") or "")
        if not raw or watched is None:
            _drop("no title or duration")
            continue
        if watched < MIN_WATCH_MS:
            _drop("under two minutes")
            continue
        # Plex stamps a play when it ends: so does this, from Netflix's start
        at = at + timedelta(milliseconds=watched)
        profile = (r.get("Profile Name") or "").strip() or "(no profile)"
        out["profiles"][profile] = out["profiles"].get(profile, 0) + 1
        out["rows"].append({"profile": profile, "raw_title": raw, **parse_title(raw), "viewed_at": at,
                            "watched_ms": watched, "bookmark_ms": _hms(r.get("Bookmark") or ""),
                            "device": (r.get("Device Type") or "").strip()[:64] or None,
                            "date_only": False})
    return out


def completed(row: dict, runtime_ms: Optional[int] = None) -> bool:
    """Watched through: the bookmark reached 90 % of the runtime when it is
    known (the title is in the library), else the estimate for its kind.
    A simple-export row has no bookmark: Netflix lists what was watched."""
    if row.get("date_only"):
        return True
    pos = row.get("bookmark_ms") or row.get("watched_ms") or 0
    if runtime_ms:
        return pos >= 0.9 * runtime_ms
    return pos >= _DONE_EST_MS.get(row.get("media_type") or "movie", _DONE_EST_MS["movie"])


# ── the upload ──────────────────────────────────────────────────────────────

def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name)[:120] or "upload.csv"


def save_upload(filename: str, content: bytes, import_dir: Path = None) -> dict:
    """Keep the usable CSVs of one upload (a CSV, or the export zip, of
    which only ViewingActivity.csv is read). {"saved": [...], "rejected":
    [(name, reason)]}."""
    import_dir = import_dir or IMPORT_DIR
    import_dir.mkdir(parents=True, exist_ok=True)
    saved, rejected = [], []

    def _keep(name: str, data: bytes):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            rejected.append((Path(name).name, "not a UTF-8 CSV"))
            return
        kind = kind_of(text)
        if kind is None:
            rejected.append((Path(name).name, "not a Netflix viewing-history CSV"))
            return
        stem = "ViewingActivity" if kind == FULL else Path(_safe_name(name)).stem
        target = import_dir / f"{stem}-{hashlib.sha1(data, usedforsecurity=False).hexdigest()[:8]}.csv"
        target.write_bytes(data)
        saved.append(target.name)

    if filename.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(BytesIO(content)) as zf:
                members = [m for m in zf.namelist() if _FULL_MEMBER.search(m)]
                if not members:
                    rejected.append((filename, "the zip has no CONTENT_INTERACTION/ViewingActivity.csv"))
                for m in members:
                    with zf.open(m) as fh:
                        data = fh.read(MAX_MEMBER_BYTES + 1)
                    if len(data) > MAX_MEMBER_BYTES:
                        rejected.append((Path(m).name, "file too large for a Netflix export"))
                        continue
                    _keep(m, data)
        except zipfile.BadZipFile:
            rejected.append((filename, "not a readable zip archive"))
    else:
        _keep(filename, content)
    return {"saved": saved, "rejected": rejected}


def pending_files(import_dir: Path = None) -> list[dict]:
    """The waiting CSVs, each with its kind and, for the full export, the
    profiles in it (the admin assigns each to a household member)."""
    import_dir = import_dir or IMPORT_DIR
    if not import_dir.exists():
        return []
    out = []
    for f in sorted(import_dir.glob("*.csv")):
        text = f.read_text(encoding="utf-8-sig", errors="replace")
        r = read_rows(text, f.name)
        if r["kind"] is None:
            continue
        out.append({"name": f.name, "size": f.stat().st_size, "kind": r["kind"],
                    "profiles": r["profiles"], "plays": len(r["rows"]), "dropped": r["dropped"]})
    return out


def clear_pending(import_dir: Path = None) -> int:
    import_dir = import_dir or IMPORT_DIR
    removed = 0
    for f in list(import_dir.glob("*.csv")) if import_dir.exists() else []:
        f.unlink()
        removed += 1
    return removed


# ── the import ──────────────────────────────────────────────────────────────

def _existing(db, user_id: int, rows: list) -> set:
    from src.database.models import WatchHistoryEntry as W
    lo, hi = min(r["viewed_at"] for r in rows), max(r["viewed_at"] for r in rows)
    return {(ref, at) for ref, at in db.query(W.source_ref, W.viewed_at).filter(
        W.user_id == user_id, W.source == "netflix", W.viewed_at >= lo, W.viewed_at <= hi)}


def to_entry(row: dict, user_id: int, plex_user_id: str) -> dict:
    """One parsed (and matched) play as a watch_history row."""
    ref = title_ref(row["raw_title"])
    return {
        "user_id": user_id, "plex_user_id": plex_user_id,
        "plex_item_id": row.get("plex_item_id") or ref, "source_ref": ref, "source": "netflix",
        "title": (row["title"] or row["raw_title"])[:512], "media_type": row["media_type"],
        "series_title": (row.get("series_title") or None) and row["series_title"][:512],
        "season": row.get("season"), "episode": row.get("episode"),
        "viewed_at": row["viewed_at"], "duration_ms": None,
        # the position reached, like a Plex play's offset; the time actually
        # watched is the play's length (wrapup_facts.played_ms)
        "view_offset_ms": row.get("bookmark_ms"), "listened_ms": row.get("watched_ms"),
        "completed": completed(row), "tmdb_id": row.get("tmdb_id"),
        "player": row.get("device"), "date_only": bool(row.get("date_only")),
    }


def insert(entries_by_user: dict) -> dict:
    """Write the plays, skipping the ones already there (same Netflix title,
    same start). Blocking: call via run_write."""
    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry
    imported = dupes = 0
    with get_db_session() as db:
        for user_id, entries in entries_by_user.items():
            for i in range(0, len(entries), BATCH_SIZE):
                chunk = entries[i:i + BATCH_SIZE]
                seen = _existing(db, user_id, chunk)
                new = []
                for e in chunk:
                    k = (e["source_ref"], e["viewed_at"])
                    if k in seen:
                        dupes += 1
                        continue
                    seen.add(k)
                    new.append(e)
                if new:
                    db.bulk_insert_mappings(WatchHistoryEntry, new)
                    imported += len(new)
                db.commit()
    return {"imported": imported, "duplicates": dupes}


async def run_import(assign: dict, import_dir: Path = None, task=None, find=None,
                     episodes=None, seasons=None) -> dict:
    """Import the pending files. ``assign``: {profile: user_id} - a full
    export's profile names, a simple export's file name; profiles left out
    are not imported. Titles are matched onto the library first
    (services/netflix_match)."""
    import asyncio
    from src.database.connection import get_db_session
    from src.database.models import User
    from src.database.runner import run_read, run_write
    from src.services import netflix_match as nm

    import_dir = import_dir or IMPORT_DIR
    files = sorted(import_dir.glob("*.csv")) if import_dir.exists() else []
    if not files:
        return {"error": "no pending files"}

    def _users():
        with get_db_session() as db:
            ids = {int(v) for v in assign.values() if v is not None}
            return {u.id: u.plex_user_id for u in db.query(User).filter(User.id.in_(ids))}

    users = await run_read(_users)

    def _parse():
        rows, dropped, skipped = [], {}, 0
        for f in files:
            r = read_rows(f.read_text(encoding="utf-8-sig", errors="replace"), f.name)
            for k, v in r["dropped"].items():
                dropped[k] = dropped.get(k, 0) + v
            for row in r["rows"]:
                uid = assign.get(row["profile"])
                if uid is None or int(uid) not in users:
                    skipped += 1
                    continue
                rows.append({**row, "user_id": int(uid)})
        return rows, dropped, skipped

    rows, dropped, skipped = await asyncio.to_thread(_parse)

    def _report(i, n):
        if task is None:
            return
        try:
            from src.services.task_monitor import task_monitor
            task_monitor.update(task, processed=i, total=n, message="matching titles to the library")
        except Exception:  # noqa: BLE001 - progress card only
            pass

    def _cat():
        with get_db_session() as db:
            return nm.catalog(db)

    cat = await run_read(_cat)
    rows = [nm.fix_kind(r, cat) for r in rows]
    matches = await nm.resolve(rows, cat, find=find, task_report=_report)
    episodes = episodes or nm.sonarr_episodes
    seasons = seasons or nm.tmdb_season_episodes
    ep_lists: dict = {}
    season_lists: dict = {}
    by_user: dict = {}
    in_library = 0
    for r in rows:
        entry, found = matches[nm.name_of(r)]
        in_library += bool(entry)
        r = nm.apply(r, entry, found)
        if (entry and entry.get("arr_id") and r["media_type"] != "movie"
                and (r.get("episode") is None or r.get("season") is None)):
            if entry["arr_id"] not in ep_lists:
                ep_lists[entry["arr_id"]] = await episodes(entry["arr_id"])
            r = nm.number_episode(r, ep_lists[entry["arr_id"]])
        # still no number: TMDB's list of the season, in English and German
        if (r["media_type"] != "movie" and r.get("episode") is None and r.get("season") is not None
                and r.get("tmdb_id")):
            sk = (r["tmdb_id"], r["season"])
            if sk not in season_lists:
                season_lists[sk] = await seasons(*sk)
            r = nm.number_episode(r, season_lists[sk])
        by_user.setdefault(r["user_id"], []).append(to_entry(r, r["user_id"], users[r["user_id"]]))
    stats = await run_write(insert, by_user)
    done = import_dir / "imported"
    done.mkdir(exist_ok=True)
    for f in files:
        f.replace(done / f.name)
    titles = {nm.name_of(r) for r in rows}
    stats.update({"files": len(files), "plays": len(rows), "not_assigned": skipped, "dropped": dropped,
                  "titles": len(titles), "titles_in_library": sum(1 for t in titles if matches[t][0]),
                  "plays_in_library": in_library})
    logger.info("[netflix-import] %s", stats)
    return stats
