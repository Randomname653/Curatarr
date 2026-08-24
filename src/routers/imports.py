"""Data imports through the GUI — no scripts, no shell.

The upload half is open during first-run setup as well (the wizard's Import
step), because that is exactly when people have their Spotify export at hand.
Actually RUNNING an import needs a user to attach the plays to, and users only
exist after the first Plex sync — so the run half is admin-only, and the
pending files simply wait until then.
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from src.routers.auth import require_admin, require_admin_or_first_run
from src.services import spotify_import

logger = logging.getLogger(__name__)

router = APIRouter()

# One import at a time, and the last outcome survives for the status card.
_running: dict = {}
_last_result: dict = {}

# A whole extended history is a few dozen MB; a single member beyond this is
# not a Spotify export, it is a mistake (or mischief on the LAN).
_MAX_FILE_MB = 200


@router.get("/spotify/status")
async def spotify_status(_gate=Depends(require_admin_or_first_run)):
    return {
        "pending": spotify_import.pending_files(),
        "running": bool(_running),
        "last_result": _last_result or None,
    }


@router.post("/spotify/files")
async def spotify_upload(files: list[UploadFile] = File(...),
                         _gate=Depends(require_admin_or_first_run)):
    saved, rejected = [], []
    for up in files:
        content = await up.read()
        if len(content) > _MAX_FILE_MB * 1024 * 1024:
            rejected.append((up.filename, f"larger than {_MAX_FILE_MB} MB"))
            continue
        result = spotify_import.save_upload(up.filename or "upload", content)
        saved += result["saved"]
        rejected += result["rejected"]
    return {"saved": saved,
            "rejected": [{"name": n, "reason": r} for n, r in rejected],
            "pending": spotify_import.pending_files()}


@router.delete("/spotify/files")
async def spotify_clear(_admin=Depends(require_admin)):
    return {"removed": spotify_import.clear_pending()}


@router.post("/spotify/run")
async def spotify_run(payload: dict, _admin=Depends(require_admin)):
    if _running:
        raise HTTPException(409, "an import is already running")
    user_id = payload.get("user_id")
    if not isinstance(user_id, int):
        raise HTTPException(422, "user_id (int) is required")
    if not spotify_import.pending_files():
        raise HTTPException(400, "no pending files — upload first")

    from src.services.task_monitor import task_monitor
    card = task_monitor.create(name="Spotify history import",
                               category="import", task_id="spotify-import")
    task_monitor.start(card)
    _running["task"] = True

    async def _job():
        try:
            # The importer is synchronous SQLAlchemy work; keep it off the
            # event loop so the UI stays responsive during a 100k-row insert.
            stats = await asyncio.to_thread(
                spotify_import.run_import, user_id, None, card)
            _last_result.clear()
            _last_result.update(stats)
            if stats.get("error"):
                task_monitor.error(card, stats["error"])
            else:
                task_monitor.done(card, f"{stats['imported']} plays imported, "
                                        f"{stats['duplicates']} duplicates skipped")
        except Exception as e:
            logger.warning("[spotify-import] failed: %s", e)
            _last_result.clear()
            _last_result.update({"error": str(e)})
            task_monitor.error(card, str(e))
        finally:
            _running.clear()

    asyncio.create_task(_job())
    return {"started": True}
