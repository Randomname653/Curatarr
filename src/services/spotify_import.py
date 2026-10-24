"""Spotify listening-history import — the engine behind the GUI upload.

The importer began life as a root-level script with two constants to edit by
hand. The logic now lives here so the setup wizard and the admin view can do
the same thing through a drop zone: the user requests their *extended
streaming history* from Spotify, drags the files (or the whole zip) into
Curatarr, picks whose history it is, and the app does the rest.
``import_spotify.py`` remains as a thin CLI wrapper over this module.

Two Spotify export formats exist and only one is usable:

* the EXTENDED streaming history (``Streaming_History_Audio_*.json`` /
  ``endsong_*.json``) carries ``ms_played``, the track URI and skip flags —
  everything the completion rule needs;
* the basic account-data export (``StreamingHistory*.json``) lacks all of
  that. It is rejected with an explanation rather than half-imported,
  because rows without a completion signal would poison replay counting.
"""
import json
import logging
import re
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from src.paths import ROOT  # noqa: E402

# Anchored to the project root like every other data path (src/paths.py);
# a relative path silently followed the process CWD, which differs between
# the console launcher, the tray and a frozen build.
IMPORT_DIR = ROOT / "data" / "imports" / "spotify"

# A real Spotify extended-history JSON is a few MB; the whole export a few
# tens. Members are read through a bounded stream — the zip's own headers
# are not trusted for the size (a deflate stream can expand far past what
# the upload cap allowed in).
MAX_MEMBER_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 400 * 1024 * 1024

BATCH_SIZE = 5000
MIN_MS_PLAYED = 30_000   # shorter than 30 s: a skip (music_skips), not a play

# usable: extended-history files. unusable: the basic export, whose entries
# have no ms_played/URI/skip data.
_USABLE = re.compile(r"^(Streaming_History_Audio_.*|endsong_?\d*)\.json$", re.I)
_BASIC = re.compile(r"^StreamingHistory\d*\.json$", re.I)


def _parse_ts(ts_str: str) -> Optional[datetime]:
    """The play's timestamp, or None when it cannot be read. This used to
    fall back to utcnow(): the play then looked recent, and since dedup keys
    on (track, timestamp) a re-import inserted it again."""
    try:
        return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None


def _classify(filename: str) -> str:
    """'usable' | 'basic' | 'other' for one file name."""
    name = Path(filename).name
    if _USABLE.match(name):
        return "usable"
    if _BASIC.match(name):
        return "basic"
    return "other"


_BASIC_MSG = ("this is the BASIC account export, which lacks play durations "
              "and skip flags — request the EXTENDED streaming history from "
              "Spotify's privacy page instead")


def save_upload(filename: str, content: bytes, import_dir: Path = None, *,
                max_member_bytes: int = MAX_MEMBER_BYTES,
                max_total_bytes: int = MAX_TOTAL_BYTES) -> dict:
    """Store one uploaded file (or unpack a zip) into the pending directory.

    Returns ``{"saved": [names], "rejected": [(name, reason)]}``. Only files
    the importer can actually use are kept — storing the rest would show a
    pending count that an import can never clear. Zip members are read
    through a bounded stream: one over ``max_member_bytes`` is rejected,
    and extraction stops once ``max_total_bytes`` have been written.
    """
    import_dir = import_dir or IMPORT_DIR
    import_dir.mkdir(parents=True, exist_ok=True)
    saved, rejected = [], []
    total = 0

    def _keep(name: str, data: bytes):
        kind = _classify(name)
        if kind == "usable":
            target = import_dir / Path(name).name
            target.write_bytes(data)
            saved.append(target.name)
        elif kind == "basic":
            rejected.append((Path(name).name, _BASIC_MSG))
        else:
            rejected.append((Path(name).name,
                             "not a Spotify extended-history file"))

    if filename.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(BytesIO(content)) as zf:
                members = [m for m in zf.namelist() if not m.endswith("/")]
                for member in members:
                    if _classify(member) != "usable":
                        _keep(member, b"")          # classified + rejected, never read
                        continue
                    with zf.open(member) as fh:
                        data = fh.read(max_member_bytes + 1)
                    if len(data) > max_member_bytes:
                        rejected.append((Path(member).name, "file too large for a Spotify export"))
                        continue
                    if total + len(data) > max_total_bytes:
                        rejected.append((Path(member).name, "archive exceeds the import size limit"))
                        break
                    total += len(data)
                    _keep(member, data)
            if not saved and not rejected:
                rejected.append((filename, "zip contains no files"))
        except zipfile.BadZipFile:
            rejected.append((filename, "not a readable zip archive"))
    else:
        _keep(filename, content)
    return {"saved": saved, "rejected": rejected}


def pending_files(import_dir: Path = None) -> list[dict]:
    import_dir = import_dir or IMPORT_DIR
    if not import_dir.exists():
        return []
    return [{"name": f.name, "size": f.stat().st_size}
            for f in sorted(import_dir.glob("*.json"))
            if _classify(f.name) == "usable"]


def clear_pending(import_dir: Path = None) -> int:
    import_dir = import_dir or IMPORT_DIR
    removed = 0
    for f in list(import_dir.glob("*.json")) if import_dir.exists() else []:
        f.unlink()
        removed += 1
    return removed


def _entry(item: dict) -> tuple:
    """One export entry → ("play", fields) | ("skip", fields) | (reason, None).

    A play under ``MIN_MS_PLAYED`` is a skip: kept in ``music_skips``, never
    in watch history (a skip is not a play). Podcasts and audiobooks, entries
    without a track or artist, and unreadable timestamps are left out."""
    if item.get("episode_name") or item.get("audiobook_title"):
        return "skipped", None
    track_name = item.get("master_metadata_track_name")
    artist_name = item.get("master_metadata_album_artist_name")
    if not track_name or not artist_name:
        return "skipped", None
    viewed_at = _parse_ts(item.get("ts", ""))
    if viewed_at is None:
        return "bad_ts", None
    ms_played = int(item.get("ms_played") or 0)
    shuffle = item.get("shuffle")
    ctx = {
        "title": track_name[:512],
        "album": (item.get("master_metadata_album_album_name") or "")[:512] or None,
        "spotify_uri": (item.get("spotify_track_uri") or "")[:100],
        "platform": (item.get("platform") or "")[:64] or None,
        "reason_end": (item.get("reason_end") or "")[:32] or None,
        "shuffle": bool(shuffle) if shuffle is not None else None,
    }
    if ms_played < MIN_MS_PLAYED:
        return "skip", {**ctx, "artist": artist_name[:512], "at": viewed_at, "ms_played": ms_played}
    # "trackdone" = played to the end; an unskipped 2-minute play counts
    # too — the same completion rule the Plex path applies.
    is_completed = (item.get("reason_end") == "trackdone"
                    or (not item.get("skipped") and ms_played >= 120_000))
    return "play", {
        **ctx,
        # placeholder until music_matcher resolves the ratingKey
        "plex_item_id": ctx["spotify_uri"][:64],
        "series_title": artist_name[:512],   # artist rides here
        "media_type": "music",
        "season": None, "episode": None,
        "viewed_at": viewed_at,
        "duration_ms": None,
        "view_offset_ms": ms_played,
        "listened_ms": ms_played,
        "completed": is_completed,
        "genres": None, "tmdb_id": None,
        "source": "spotify",
    }


def _play_key(uri, artist, title, at) -> tuple:
    """A play's identity across re-imports. Not plex_item_id: the matcher
    rewrites it to the Plex ratingKey, after which a re-uploaded file read
    every play as new."""
    return (uri or f"{artist}|{title}", at.strftime("%Y-%m-%dT%H:%M:%S"))


def _existing_plays(db, user_id: int, rows: list) -> set:
    from src.database.models import WatchHistoryEntry as W
    lo, hi = min(r["viewed_at"] for r in rows), max(r["viewed_at"] for r in rows)
    return {_play_key(u, a, t, v) for u, a, t, v in db.query(
        W.spotify_uri, W.series_title, W.title, W.viewed_at).filter(
        W.user_id == user_id, W.media_type == "music", W.source == "spotify",
        W.viewed_at >= lo, W.viewed_at <= hi)}


def _existing_skips(db, user_id: int, rows: list) -> set:
    from src.database.models import MusicSkip as S
    lo, hi = min(r["at"] for r in rows), max(r["at"] for r in rows)
    return {_play_key(u, a, t, v) for u, a, t, v in db.query(
        S.spotify_uri, S.artist, S.title, S.at).filter(
        S.user_id == user_id, S.at >= lo, S.at <= hi)}


def run_import(user_id: int, import_dir: Path = None, task=None) -> dict:
    """Import every pending file for ``user_id``. Returns the stats dict.

    Duplicate-safe against both the database and re-uploaded files; imported
    files move to an ``imported/`` subfolder so the pending count clears and
    a crashed run can simply be started again.
    """
    from src.database.connection import get_db_session
    from src.database.models import MusicSkip, User, WatchHistoryEntry

    import_dir = import_dir or IMPORT_DIR
    files = [import_dir / p["name"] for p in pending_files(import_dir)]
    if not files:
        return {"error": "no pending files"}

    with get_db_session() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return {"error": f"user {user_id} does not exist"}
        plex_user_id = user.plex_user_id

    if task is not None:
        from src.services.task_monitor import task_monitor
        task_monitor.update(task, processed=0, total=len(files),
                            message="importing listening history")

    imported = skipped = dupes = bad_ts = skips = 0
    buffer: list = []
    skip_buffer: list = []
    done_dir = import_dir / "imported"

    def _flush_buffer(db):
        nonlocal imported, dupes, skips
        if buffer:
            seen = _existing_plays(db, user_id, buffer)
            to_insert = []
            for item in buffer:
                k = _play_key(item["spotify_uri"], item["series_title"], item["title"],
                              item["viewed_at"])
                if k in seen:
                    dupes += 1
                else:
                    to_insert.append({**item, "user_id": user_id, "plex_user_id": plex_user_id})
                    seen.add(k)
            if to_insert:
                db.bulk_insert_mappings(WatchHistoryEntry, to_insert)
                imported += len(to_insert)
        if skip_buffer:
            seen = _existing_skips(db, user_id, skip_buffer)
            new = []
            for item in skip_buffer:
                k = _play_key(item["spotify_uri"], item["artist"], item["title"], item["at"])
                if k not in seen:
                    new.append({**item, "user_id": user_id, "source": "spotify"})
                    seen.add(k)
            if new:
                db.bulk_insert_mappings(MusicSkip, new)
                skips += len(new)
        db.commit()
        buffer.clear()
        skip_buffer.clear()

    with get_db_session() as db:
        for i, file_path in enumerate(files, 1):
            try:
                data = json.loads(file_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("[spotify-import] unreadable %s: %s",
                               file_path.name, e)
                continue
            for item in data:
                kind, fields = _entry(item)
                if kind == "play":
                    buffer.append(fields)
                elif kind == "skip":
                    skip_buffer.append(fields)
                elif kind == "bad_ts":
                    bad_ts += 1
                else:
                    skipped += 1
                if len(buffer) + len(skip_buffer) >= BATCH_SIZE:
                    _flush_buffer(db)

            # Commit BEFORE moving: the move is the statement "everything in
            # this file is in the database". Moving with rows still buffered
            # would lose up to a batch of plays on a crash — silently, since
            # the file would no longer be pending.
            _flush_buffer(db)

            done_dir.mkdir(exist_ok=True)
            # replace, not rename: on Windows rename refuses an existing
            # target (the same export uploaded twice), on Linux both overwrite.
            file_path.replace(done_dir / file_path.name)
            if task is not None:
                from src.services.task_monitor import task_monitor
                task_monitor.update(task, processed=i, total=len(files),
                                    message=f"{imported} plays so far")
        _flush_buffer(db)

    stats = {"imported": imported, "skipped": skipped, "duplicates": dupes, "skips": skips,
             "bad_timestamp": bad_ts, "files": len(files), "user_id": user_id}
    if bad_ts:
        logger.warning("[spotify-import] %d plays skipped: unreadable timestamp", bad_ts)
    logger.info("[spotify-import] %s", stats)
    return stats


# ── what earlier imports dropped ────────────────────────────────────────────
#
# Imports before 2026-10-25 kept neither skips nor the album, platform, end
# reason or shuffle state of a play. The files they read are still there in
# ``imported/``. Read once more, they fill those columns on the stored plays
# (only where empty) and bring the skips in. A file does not say whose
# history it is: its plays found in the database do, and its skips go to
# the user most of them belong to.
REREAD_KEY = "spotify_reread_v1"


def imported_files(import_dir: Path = None) -> list:
    done_dir = (import_dir or IMPORT_DIR) / "imported"
    if not done_dir.exists():
        return []
    return [f for f in sorted(done_dir.glob("*.json")) if _classify(f.name) == "usable"]


def reread_file(path: Path) -> dict:
    """Fill one already imported file's missing columns and skips. Blocking
    — one write transaction; call via ``run_write``."""
    from collections import Counter
    from src.database.connection import get_db_session
    from src.database.models import MusicSkip, WatchHistoryEntry as W
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - one unreadable file is reported, the rest go on
        logger.warning("[spotify-reread] unreadable %s: %s", path.name, e)
        return {"file": path.name, "error": "unreadable"}
    plays, skip_rows = [], []
    for item in data if isinstance(data, list) else []:
        kind, fields = _entry(item)
        if kind == "play":
            plays.append(fields)
        elif kind == "skip":
            skip_rows.append(fields)
    out = {"file": path.name, "filled": 0, "skips": 0, "unattributed_skips": 0}
    with get_db_session() as db:
        owners = Counter()
        if plays:
            lo, hi = min(p["viewed_at"] for p in plays), max(p["viewed_at"] for p in plays)
            stored: dict = {}
            for rid, uid, u, a, t, v, album in db.query(
                    W.id, W.user_id, W.spotify_uri, W.series_title, W.title, W.viewed_at, W.album
            ).filter(W.media_type == "music", W.source == "spotify",
                     W.viewed_at >= lo, W.viewed_at <= hi):
                stored[_play_key(u, a, t, v)] = (rid, uid, album)
            updates = []
            for p in plays:
                hit = stored.get(_play_key(p["spotify_uri"], p["series_title"], p["title"],
                                           p["viewed_at"]))
                if hit is None:
                    continue
                owners[hit[1]] += 1
                if hit[2] is None:
                    updates.append({"id": hit[0], "album": p["album"], "listened_ms": p["listened_ms"],
                                    "platform": p["platform"], "reason_end": p["reason_end"],
                                    "shuffle": p["shuffle"]})
            if updates:
                db.bulk_update_mappings(W, updates)
                out["filled"] = len(updates)
        if skip_rows:
            if not owners:
                out["unattributed_skips"] = len(skip_rows)
            else:
                uid = owners.most_common(1)[0][0]
                seen = _existing_skips(db, uid, skip_rows)
                new = []
                for sk in skip_rows:
                    k = _play_key(sk["spotify_uri"], sk["artist"], sk["title"], sk["at"])
                    if k not in seen:
                        new.append({**sk, "user_id": uid, "source": "spotify"})
                        seen.add(k)
                if new:
                    db.bulk_insert_mappings(MusicSkip, new)
                    out["skips"] = len(new)
        db.commit()
    return out


async def reread_imported(task=None) -> bool:
    """The custodian job: once, every already imported file, one write each."""
    from src.database.runner import run_read, run_write
    from src.services.app_state import get_state, set_state
    if (await run_read(get_state, REREAD_KEY)) == "1":
        return True
    files = imported_files()
    total = {"filled": 0, "skips": 0, "unattributed_skips": 0}
    for i, f in enumerate(files, 1):
        got = await run_write(reread_file, f)
        for k in total:
            total[k] += got.get(k, 0)
        if task is not None:
            from src.services.task_monitor import task_monitor
            task_monitor.update(task, processed=i, total=len(files),
                                message=f"{total['filled']} plays filled, {total['skips']} skips")
    await run_write(set_state, REREAD_KEY, "1")
    if files:
        logger.info("[spotify-reread] %d file(s): %s", len(files), total)
    return True
