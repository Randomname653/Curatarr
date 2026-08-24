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

logger = logging.getLogger(__name__)

IMPORT_DIR = Path("data/imports/spotify")

BATCH_SIZE = 5000
MIN_MS_PLAYED = 30_000   # skip plays shorter than 30 s (accidental taps / skips)

# usable: extended-history files. unusable: the basic export, whose entries
# have no ms_played/URI/skip data.
_USABLE = re.compile(r"^(Streaming_History_Audio_.*|endsong_?\d*)\.json$", re.I)
_BASIC = re.compile(r"^StreamingHistory\d*\.json$", re.I)


def _parse_ts(ts_str: str) -> datetime:
    try:
        return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return datetime.utcnow()


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


def save_upload(filename: str, content: bytes, import_dir: Path = None) -> dict:
    """Store one uploaded file (or unpack a zip) into the pending directory.

    Returns ``{"saved": [names], "rejected": [(name, reason)]}``. Only files
    the importer can actually use are kept — storing the rest would show a
    pending count that an import can never clear.
    """
    import_dir = import_dir or IMPORT_DIR
    import_dir.mkdir(parents=True, exist_ok=True)
    saved, rejected = [], []

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
                    _keep(member, zf.read(member))
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


def run_import(user_id: int, import_dir: Path = None, task=None) -> dict:
    """Import every pending file for ``user_id``. Returns the stats dict.

    Duplicate-safe against both the database and re-uploaded files; imported
    files move to an ``imported/`` subfolder so the pending count clears and
    a crashed run can simply be started again.
    """
    from src.database.connection import get_db_session
    from src.database.models import User, WatchHistoryEntry

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
                            message="loading existing history for dedup")

    with get_db_session() as db:
        existing = {
            f"{r.plex_item_id}|{r.viewed_at.strftime('%Y-%m-%dT%H:%M:%S')}"
            for r in db.query(WatchHistoryEntry.plex_item_id,
                              WatchHistoryEntry.viewed_at)
            .filter(WatchHistoryEntry.user_id == user_id,
                    WatchHistoryEntry.media_type == "music").all()
        }

    imported = skipped = dupes = 0
    buffer: list = []
    done_dir = import_dir / "imported"

    with get_db_session() as db:
        for i, file_path in enumerate(files, 1):
            try:
                data = json.loads(file_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("[spotify-import] unreadable %s: %s",
                               file_path.name, e)
                continue
            for item in data:
                ms_played = item.get("ms_played", 0)
                if item.get("episode_name") or item.get("audiobook_title"):
                    skipped += 1
                    continue
                if ms_played < MIN_MS_PLAYED:
                    skipped += 1
                    continue
                track_name = item.get("master_metadata_track_name")
                artist_name = item.get("master_metadata_album_artist_name")
                track_uri = item.get("spotify_track_uri")
                if not track_name or not artist_name:
                    skipped += 1
                    continue
                viewed_at = _parse_ts(item.get("ts", ""))
                dedup_key = (f"{track_uri}|"
                             f"{viewed_at.strftime('%Y-%m-%dT%H:%M:%S')}")
                if dedup_key in existing:
                    dupes += 1
                    continue
                existing.add(dedup_key)
                # "trackdone" = played to the end; an unskipped 2-minute play
                # counts too — the same completion rule the Plex path applies.
                is_completed = (
                    item.get("reason_end") == "trackdone"
                    or (not item.get("skipped") and ms_played >= 120_000)
                )
                buffer.append({
                    "user_id": user_id,
                    "plex_user_id": plex_user_id,
                    # placeholder until music_matcher resolves the ratingKey
                    "plex_item_id": (track_uri or "")[:64],
                    "title": track_name[:512],
                    "series_title": artist_name[:512],   # artist rides here
                    "media_type": "music",
                    "season": None, "episode": None,
                    "viewed_at": viewed_at,
                    "duration_ms": None,
                    "view_offset_ms": ms_played,
                    "completed": is_completed,
                    "genres": None, "tmdb_id": None,
                    "spotify_uri": (track_uri or "")[:100],
                    "source": "spotify",
                })
                if len(buffer) >= BATCH_SIZE:
                    db.bulk_insert_mappings(WatchHistoryEntry, buffer)
                    db.commit()
                    imported += len(buffer)
                    buffer = []
            # Commit BEFORE moving: the move is the statement "everything in
            # this file is in the database". Moving with rows still buffered
            # would lose up to a batch of plays on a crash — silently, since
            # the file would no longer be pending.
            if buffer:
                db.bulk_insert_mappings(WatchHistoryEntry, buffer)
                db.commit()
                imported += len(buffer)
                buffer = []
            done_dir.mkdir(exist_ok=True)
            file_path.rename(done_dir / file_path.name)
            if task is not None:
                from src.services.task_monitor import task_monitor
                task_monitor.update(task, processed=i, total=len(files),
                                    message=f"{imported + len(buffer)} plays so far")
        if buffer:
            db.bulk_insert_mappings(WatchHistoryEntry, buffer)
            db.commit()
            imported += len(buffer)

    stats = {"imported": imported, "skipped": skipped, "duplicates": dupes,
             "files": len(files), "user_id": user_id}
    logger.info("[spotify-import] %s", stats)
    return stats
