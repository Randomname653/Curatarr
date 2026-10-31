"""Moving this install to the other media server from the web page
(Settings -> Media server), both ways, with the curation kept whole.

The rebind (rebind.py) does the work; this runs it inside the live
process. The script asks for Curatarr to be stopped; here the process
holds off everything that writes server keys instead:

1. ``start_check`` - the dry run, in the background: both servers walked,
   the plan kept here, its report for the page. Users and libraries
   without a match get a dropdown each there.
2. ``start_switch`` - with the admin's choices the plan is built again
   (both libraries may have changed since the check). If nothing blocks:
   the custodian paused, the Jellyfin session poller stopped, the Plex
   sync's lock and the Jellyfin sync's lock held; then the backup, the
   re-key and ``MEDIA_SERVER_TYPE`` flipped, the settings reloaded in
   place, the poller started on a move to Jellyfin. Last, what was watched
   is marked played on the new server, so its own "unwatched" agrees with
   the curation (rebind.sync_watched_state). The way back to Plex is the
   same switch, and carries the Jellyfin period's plays along.
3. ``start_undo`` - the emergency exit: the newest switch replayed
   backwards (rebind.undo), back to where it started.

Refused while an enrichment, the music pipeline or a deletion run holds
its lock: those write server keys from lists they read before.

One switch at a time; its progress is one Activity task.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.services.media_server import rebind

logger = logging.getLogger(__name__)

BUSY_FLAGS = ("enrichment_running", "music_pipeline_running", "deletion_run")
SYNC_WAIT_S = 10 * 60
# The switch reuses the check's library walks this long: a walk of a large
# library takes minutes, and only the picks changed in between.
WALKS_FRESH_S = 60 * 60

_lock = asyncio.Lock()
_state: dict = {"phase": "idle"}     # idle | checking | ready | switching | done | failed
_plan: Optional[rebind.Plan] = None


def status() -> dict:
    from src.config import settings
    from src.services.media_server import active_kind
    out = {k: v for k, v in _state.items()}
    out["active"] = active_kind()
    out["configured"] = {
        "plex": bool(settings.effective_plex_url and settings.effective_plex_token),
        "jellyfin": bool(settings.JELLYFIN_URL and settings.JELLYFIN_API_KEY),
    }
    out["undo"] = _undo_target()
    return out


def _undo_target() -> Optional[dict]:
    """The newest completed switch, which ``start_undo`` would replay."""
    best = None
    for m in sorted(rebind.default_backup_root().glob("rebind-*/manifest.json")):
        try:
            man = json.loads(m.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if man.get("status") == "completed":
            best = {"manifest": str(m), "from": man.get("from"), "to": man.get("to"),
                    "finished": man.get("finished")}
    return best


def _options(raw: Optional[dict]) -> dict:
    raw = raw or {}
    norm = rebind.normalize_flag
    return {
        "map_user": {norm(k): norm(v) for k, v in (raw.get("map_user") or {}).items() if k and v},
        "skip_users": [norm(s) for s in raw.get("skip_users") or [] if s],
        "map_library": {str(k): str(v) for k, v in (raw.get("map_library") or {}).items() if k and v},
        "accept_suggestions": bool(raw.get("accept_suggestions")),
        "sync_state": raw.get("sync_state", True) is not False,
    }


async def _build(target: str, opts: dict, *, walks: Optional[dict] = None) -> rebind.Plan:
    from src.services.media_server import active_kind
    source = active_kind()
    if source == target:
        raise RuntimeError(f"this install already runs on {target}")
    src_adapter, tgt_adapter = rebind.server_adapters(source, target)
    return await rebind.build_plan(source, target, tgt_adapter, source_adapter=src_adapter,
                                   map_user=opts["map_user"], skip_users=opts["skip_users"],
                                   accept_suggestions=opts["accept_suggestions"],
                                   map_library=opts["map_library"], walks=walks)


_walks: dict = {}     # {"target", "at", "walks"} of the last fresh walk


def _fresh_walks(target: str) -> Optional[dict]:
    """The last walks towards ``target``, while fresh: a second check with
    other picks and the switch after it take seconds instead of minutes."""
    if _walks.get("target") != target or not _walks.get("walks"):
        return None
    if (datetime.utcnow() - _walks["at"]).total_seconds() >= WALKS_FRESH_S:
        _walks.clear()
        return None
    return _walks["walks"]


def _keep_walks(target: str, plan: rebind.Plan, reused: bool) -> None:
    if not reused:
        _walks.update({"target": target, "at": datetime.utcnow(), "walks": plan.walks})


def _busy() -> list:
    from src.services.app_state import get_state
    return [f for f in BUSY_FLAGS if get_state(f) not in (None, "", "0")]


def _begin(phase: str, target: Optional[str], name: str):
    from src.services.task_monitor import task_monitor
    if _lock.locked() or _state.get("phase") in ("checking", "switching"):
        raise RuntimeError("a media server check or switch is already running")
    task = task_monitor.create(name, "media_server", task_id=f"media-switch-{datetime.utcnow():%H%M%S}")
    task_monitor.start(task)
    _state.clear()
    _state.update({"phase": phase, "target": target, "started": datetime.utcnow().isoformat(),
                   "task_id": task.id})
    return task


async def start_check(target: str, options: Optional[dict] = None) -> dict:
    """The dry run in the background; ``status()`` carries its report."""
    from src.services.bg_tasks import track_task
    opts = _options(options)
    task = _begin("checking", target, f"Media server check: to {target.title()}")
    track_task(_check(target, opts, task), name="media_server_check")
    return status()


async def _check(target: str, opts: dict, task) -> None:
    global _plan
    from src.services.task_monitor import task_monitor
    async with _lock:
        try:
            task_monitor.update(task, message=f"Reading both servers' libraries for the move to {target}")
            walks = _fresh_walks(target)
            _plan = await _build(target, opts, walks=walks)
            _keep_walks(target, _plan, walks is not None)
            rep = _plan.report()
            _state.update({"phase": "ready", "report": rep, "options": opts})
            task_monitor.done(task, f"{rep['items']['matched']} titles matched, "
                                    f"{rep['items']['unmatched']} without a match")
        except Exception as e:  # noqa: BLE001 - the page shows the reason; logged with the stack
            logger.exception("[switch] check towards %s failed", target)
            _state.update({"phase": "failed", "error": str(e)})
            task_monitor.error(task, str(e))


@contextlib.asynccontextmanager
async def _held(task):
    """Nothing that writes server keys runs while the re-key does."""
    from src.database.runner import run_read, run_write
    from src.services import scheduler
    from src.services.app_state import acquire_state_lock, release_state_lock
    from src.services.media_server import jellyfin_history as jh
    from src.services.task_monitor import task_monitor
    busy = await run_read(_busy)
    if busy:
        raise RuntimeError("wait until this is done, or stop it: " + ", ".join(
            b.replace("_running", "").replace("_", " ") for b in busy))
    paused = False
    if scheduler.scheduler.running:
        scheduler.scheduler.pause()
        paused = True
    await jh.stop_session_poller()
    holds_plex = False
    try:
        for i in range(SYNC_WAIT_S):
            if await run_write(acquire_state_lock, "plex_sync_running"):
                holds_plex = True
                break
            if i == 0:
                task_monitor.update(task, message="Waiting for the running Plex sync to finish")
            await asyncio.sleep(1)
        if not holds_plex:
            raise RuntimeError("a Plex sync is still running - try again when it is done")
        async with jh._sync_lock:
            yield
    finally:
        if holds_plex:
            await run_write(release_state_lock, "plex_sync_running")
        from src.services.media_server import active_kind
        if active_kind() == "jellyfin":
            jh.start_session_poller()
        if paused:
            scheduler.scheduler.resume()


def _settings_writer(target):
    """Flip ``MEDIA_SERVER_TYPE`` and reload the settings in this process."""
    from src.config import settings
    old = rebind._write_settings(target)
    settings.__init__()
    return old


async def start_switch(target: str, options: Optional[dict] = None) -> dict:
    from src.services.bg_tasks import track_task
    if _state.get("phase") != "ready" or _state.get("target") != target:
        raise RuntimeError(f"run the check towards {target} first")
    opts = _options(options)
    task = _begin("switching", target, f"Media server switch: to {target.title()}")
    track_task(_switch(target, opts, task), name="media_server_switch")
    return status()


async def _switch(target: str, opts: dict, task) -> None:
    global _plan
    from src.database.runner import run_read, run_write
    from src.services.task_monitor import task_monitor
    async with _lock:
        try:
            task_monitor.update(task, message="Matching again with your choices")
            plan = await _build(target, opts, walks=_fresh_walks(target))
            if plan.blocking:
                _plan = plan
                _state.update({"phase": "ready", "report": plan.report(), "options": opts,
                               "error": "; ".join(plan.blocking)})
                task_monitor.error(task, "; ".join(plan.blocking))
                return
            _, tgt_adapter = rebind.server_adapters(plan.source, target)
            async with _held(task):
                task_monitor.update(task, message="Backing up the database, then moving every key")
                mpath = await rebind.apply_plan(plan, target_adapter=tgt_adapter,
                                                settings_writer=_settings_writer)
                if target == "jellyfin":
                    # The delta poll starts over: an earlier Jellyfin period's
                    # snapshot would read the marks below as new plays.
                    await run_write(_reset_jellyfin_poll, [u for u in plan.users])
            synced = None
            if opts["sync_state"]:
                task_monitor.update(task, message=f"Marking what was watched as played on {target.title()}")
                synced = await rebind.sync_watched_state(plan, tgt_adapter)
                man = json.loads(Path(mpath).read_text(encoding="utf-8"))
                man["sync_state"] = synced
                rebind._save(Path(mpath), man)
            man = json.loads(Path(mpath).read_text(encoding="utf-8"))
            stuck = sum(t.get("collisions", 0) for t in man["tables"].values())
            result = {"manifest": str(mpath), "vectors": len(man.get("chroma") or []),
                      "proposals": len(man.get("proposals") or []), "collisions": stuck,
                      "sync_state": synced, "items": len(plan.items)}
            _plan = None
            _walks.clear()                  # every key changed: the next check walks afresh
            _state.update({"phase": "done", "result": result, "report": plan.report(),
                           "finished": datetime.utcnow().isoformat()})
            logger.info("[switch] now on %s: %s", target, result)
            task_monitor.done(task, f"Curatarr now runs on {target.title()}: {len(plan.items)} titles moved")
            await _after_switch()
        except Exception as e:  # noqa: BLE001 - the page shows the reason; logged with the stack
            logger.exception("[switch] switch towards %s failed", target)
            _state.update({"phase": "failed", "error": str(e)})
            task_monitor.error(task, str(e))


def _reset_jellyfin_poll(user_ids: list) -> int:
    from src.services.media_server.jellyfin_history import reset_poll
    return reset_poll(user_ids)


async def _after_switch() -> None:
    """The first sync on the new server right away, so the page shows its
    users and plays without waiting for the custodian's next tick."""
    from src.services.media_server import active_kind
    try:
        if active_kind() == "jellyfin":
            from src.services.media_server.jellyfin_history import sync_history
            res = await sync_history()
        else:
            from src.services.plex_sync import sync_plex_history
            res = await sync_plex_history(force=True)
        logger.info("[switch] first sync on the new server: %s", res)
    except Exception as e:  # noqa: BLE001 - the custodian's next tick syncs anyway; logged
        logger.warning("[switch] first sync on the new server failed: %s", e)


async def start_undo() -> dict:
    from src.services.bg_tasks import track_task
    target = _undo_target()
    if target is None:
        raise RuntimeError("no completed switch on record")
    task = _begin("switching", target["from"], f"Media server undo: back to {str(target['from']).title()}")
    track_task(_undo(Path(target["manifest"]), task), name="media_server_undo")
    return status()


async def _undo(mpath: Path, task) -> None:
    from src.services.task_monitor import task_monitor
    _walks.clear()
    async with _lock:
        try:
            async with _held(task):
                task_monitor.update(task, message="Replaying the switch backwards")
                res = await rebind.undo(mpath, settings_writer=_settings_writer)
            _state.update({"phase": "done", "result": {"undone": str(mpath), **res},
                           "finished": datetime.utcnow().isoformat()})
            task_monitor.done(task, "Switch undone")
            await _after_switch()
        except Exception as e:  # noqa: BLE001 - the page shows the reason; logged with the stack
            logger.exception("[switch] undo failed")
            _state.update({"phase": "failed", "error": str(e)})
            task_monitor.error(task, str(e))


def forget() -> dict:
    """Back to idle after the page has shown a result or an error."""
    global _plan
    if _state.get("phase") in ("checking", "switching"):
        raise RuntimeError("a media server check or switch is still running")
    _plan = None
    _walks.clear()
    _state.clear()
    _state["phase"] = "idle"
    return status()
