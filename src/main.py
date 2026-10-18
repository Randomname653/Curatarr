"""
Curatarr - Main Application Entry Point
"""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from src.config import settings
from src.log_setup import init_logging
from src.paths import DATA_DIR, frontend_root

# File + (when a console exists) stderr logging, incl. the apscheduler/httpx/
# watchfiles noise suppression — see src/log_setup.py. Idempotent: the tray
# launcher calls it before importing this module so preflight is logged too.
init_logging(settings.LOG_LEVEL)
logger = logging.getLogger("curatarr")

# ── STARTUP / SHUTDOWN ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pass 100: stop Syncthing from touching the live SQLite DB. Syncing an
    # open WAL-mode database causes "database is locked" errors and risks
    # corruption. The guard excludes data/ in the enclosing Syncthing
    # folder's .stignore — PERMANENTLY: it is never removed on shutdown, a
    # stopped DB is no safer to file-sync (no-op when Syncthing isn't
    # present). Done first, before we open the DB.
    try:
        from src.services.sync_guard import enable as _sync_guard_enable
        _sync_guard_enable()
    except Exception as e:
        # enable() handles its own errors; reaching here is a bug (an import
        # failure) and leaves the live DB unguarded — not a DEBUG matter.
        logger.warning("[sync-guard] enable failed — data/ may be synced while open: %s", e)

    # Which Python this is, on record for every start: start.bat once picked
    # a different interpreter depending on how its window was opened
    # (2026-09-25 / 09-27) and nothing in the log said so.
    try:
        import platform as _platform
        import sys as _sys
        logger.info("[python] %s (%s)", _sys.executable, _platform.python_version())
    except Exception:  # noqa: BLE001 - startup banner line only; nothing depends on it
        pass

    # Pinned requirements vs. this interpreter: reported, never installed from
    # inside the server — the launchers do that before the first import.
    # Settings → Maintenance shows the same report to admins.
    try:
        from src.deps_check import check as _deps_check
        _deps = _deps_check()
        (logger.info if _deps.clean else logger.warning)("[deps] %s", _deps.summary())
    except Exception as e:  # noqa: BLE001 - reported, the server still starts
        # A broken check would hide the drift it exists to report.
        logger.warning("[deps] check failed: %s", e)
    try:
        from src.deps_lock import check as _lock_check
        _lock = _lock_check()
        (logger.info if _lock.clean else logger.warning)("[lock] %s", _lock.summary())
    except Exception as e:  # noqa: BLE001 - reported, the server still starts
        logger.warning("[lock] check failed: %s", e)

    (DATA_DIR / "chromadb").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "cache").mkdir(parents=True, exist_ok=True)

    from src.database import loop_guard
    loop_guard.serving(False)
    from src.database.connection import init_db
    init_db()

    from src.services.scheduler import start_scheduler
    start_scheduler()

    # Reset any stuck flags from previous crash. This is the ONLY recovery
    # for a lock a crash left at "1", so each key is cleared on its own (one
    # failure used to skip every reset after it) and a failure is an ERROR:
    # the jobs behind a stuck flag wait for it until the next restart.
    from src.services.app_state import get_state, set_state
    # The music flag can be held by ANOTHER OS process (scripts/
    # music_enricher.py survives server restarts by design): a blind "0"
    # here handed its lock to the custodian and two passes ran side by side.
    # The lease (state_lease) clears it only when its holder is gone.
    _music_free = False
    try:
        from src.services import state_lease
        _music_free = state_lease.reclaim_if_stale("music_pipeline_running")
        if not _music_free:
            logger.info("[startup] music pipeline held by a live process (pid %s) — left alone",
                        state_lease.holder_pid("music_pipeline_running"))
    except Exception as e:
        logger.error("Boot: the music pipeline lease could not be checked - its "
                     "flag is left as it is until a restart: %s", e)
    _flags = ["enrichment_running", "deletion_run", "plex_sync_running"]
    if _music_free:
        _flags.append("music_pipeline_stop_requested")
    for _flag in _flags:
        try:
            set_state(_flag, "0")
            if get_state(_flag) not in (None, "0"):
                raise RuntimeError("the database stayed locked")
        except Exception as e:
            logger.error("Boot: stale flag %s could not be cleared - the jobs "
                         "that take it stay blocked until a restart: %s", _flag, e)
    # Pass 75/76: seed the game flag with the ACTUAL current state, not
    # a blind "0". A blind reset left a ~30 s window (until the watcher's
    # first tick) where game_active was wrong — long enough for the
    # startup catch-ups to load the LLM and OOM a running game. A direct
    # is_game_running() check here makes the flag correct from t=0, and a
    # probe that fails counts as a game until the watcher knows better.
    try:
        from src.services.process_monitor import is_game_running as _igr
        _game = _igr()
    except Exception as e:
        logger.warning("Boot: game probe failed - assuming a game until the "
                       "watcher's first tick: %s", e)
        _game = True
    try:
        set_state("game_active", "1" if _game else "0")
    except Exception as e:
        logger.error("Boot: game_active could not be seeded: %s", e)
    # A proposal claimed "deleting" when the process died mid-delete: the
    # outcome is unknown, so park it in limbo (listed, retryable) rather
    # than leave it invisible forever.
    try:
        from src.database.connection import get_db_session as _gds
        from src.database.models import DeletionProposal as _DP
        with _gds() as _db:
            _n = (_db.query(_DP).filter(_DP.status == "deleting")
                  .update({"status": "limbo"}, synchronize_session=False))
            _db.commit()
        if _n:
            logger.warning("Boot: %d interrupted deletion(s) parked in limbo", _n)
    except Exception as e:
        logger.error("Boot: interrupted deletions could not be parked in limbo - "
                     "they stay hidden as 'deleting': %s", e)

    # Pass 41: all startup background tasks now go through ``track_task``
    # which retains a strong reference until the task completes. Without
    # that, the GC could collect the task mid-run and silently drop the
    # work (anime-mapping load, library prewarm, startup sync).
    from src.services.bg_tasks import track_task

    # Pre-load anime mapping in background
    async def _load_anime_mapping():
        try:
            from src.services.anime_mapping import get_anime_mapping
            m = await get_anime_mapping()
            logger.info("Anime mapping ready: %d entries", m.total_entries)
        except Exception as e:
            from src.services.enrichment_state import log_topup_failure
            log_topup_failure(logger, "[boot] anime mapping", "anime-lists", e)
    track_task(_load_anime_mapping(), name="anime_mapping_load")

    # Pass 16k: pre-warm Library Manager arr caches.
    #   1. Load any persisted L2 (DB) caches into L1 (in-process) so the
    #      first user click after a restart serves instantly from cache.
    #   2. Fire a background refresh for each configured arr so the L1
    #      cache is fresh by the time someone opens the page.
    # Best-effort — failures are logged, on-demand fetch always works as
    # a fallback.
    async def _prewarm_library():
        try:
            from src.routers.library import prewarm_arr_caches
            await prewarm_arr_caches()
        except Exception as e:
            from src.services.enrichment_state import log_topup_failure
            log_topup_failure(logger, "[boot] library prewarm", "arr caches", e)
    track_task(_prewarm_library(), name="library_prewarm")

    if not settings.is_configured:
        logger.warning("Curatarr is not configured yet. Open http://localhost:%d to run setup.", settings.PORT)
    try:
        from src.database.connection import get_db_session as _gds
        from src.routers.auth import _no_admin_exists, SETUP_CODE
        with _gds() as _db:
            if _no_admin_exists(_db):
                logger.warning(
                    "No admin account yet. Setting up from ANOTHER device on the "
                    "LAN (or through a reverse proxy) needs this one-time code: %s  "
                    "(a browser on this machine at http://localhost:%d does not).",
                    SETUP_CODE, settings.PORT)
    except Exception as _e:  # noqa: BLE001 - reported, the server still starts
        # Without this line a first run cannot be finished from another device.
        logger.warning("setup-code notice skipped: %s", _e)
    else:
        from src.database.connection import get_db_session
        from src.database.models import User
        with get_db_session() as db:
            # ⚡ Bolt: Fast existence check avoiding full table counts
            has_users = db.query(User.id).first() is not None
        if not has_users:
            logger.info("No users yet — first login via Plex will create the admin account.")

        if settings.SYNC_ON_STARTUP:
            track_task(_startup_sync_if_needed(), name="startup_sync_if_needed")

    loop_guard.serving(True)
    yield

    # Pass 61: flush any pending debounced memory extractions before we go
    # down — otherwise a restart inside the 90s debounce window drops the
    # last conversation's memories entirely.
    try:
        from src.services.episodic_memory import flush_all_pending_extractions
        await flush_all_pending_extractions()
    except Exception as e:
        # Pending extractions are lost on this path — worth seeing.
        logger.warning("[shutdown] memory flush failed: %s", e, exc_info=True)

    from src.services.scheduler import stop_scheduler
    stop_scheduler()

    # Running work (custodian queues, enrichment, music pipeline) is cancelled
    # and awaited NOW, while the loop is alive: its finally-blocks roll back
    # and release locks before the flags below are cleared and the DB is
    # closed. It used to keep writing through the rest of the teardown.
    try:
        from src.services.bg_tasks import drain
        await drain(timeout=15)
    except Exception as e:
        logger.warning("[shutdown] draining background tasks failed: %s", e)
    loop_guard.serving(False)

    try:
        from src.services.app_state import set_state
        set_state("enrichment_running", "0")
        # Only OUR music lease: an out-of-process enricher keeps its own.
        try:
            from src.services import state_lease
            if state_lease.release("music_pipeline_running"):
                set_state("music_pipeline_stop_requested", "0")
        except Exception as e:  # noqa: BLE001
            logger.warning("[shutdown] music lease release failed: %s", e)
        set_state("deletion_run", "0")
        set_state("plex_sync_running", "0")
    except Exception as e:
        # A flag left at "1" reads as a run in progress after the restart.
        logger.warning("[shutdown] clearing run flags failed: %s", e)

    # Cancel the idle-evict timer and free the VRAM on the way out — the
    # most common reason this app gets closed is that the GPU is wanted for
    # a game, and leaving gemma sitting in 20 GB of VRAM until Ollama's own
    # keep_alive expires defeats exactly that.
    try:
        from src.services.llm_priority import shutdown_evict
        await shutdown_evict()
    except Exception as e:
        # shutdown_evict swallows Ollama outages itself; this is a bug.
        logger.warning("[shutdown] evict pass failed: %s", e)

    # Release the database: finish queued writes, close every connection and
    # checkpoint the WAL into the main file (TRUNCATE), so data/ holds one
    # self-consistent .db before Syncthing may touch it again.
    try:
        from src.vector_store.chromadb_wrapper import shutdown_chroma_pool
        shutdown_chroma_pool()
    except Exception as e:
        logger.warning("[shutdown] Chroma thread did not stop cleanly: %s", e)
    try:
        from src.database.runner import shutdown_writer
        shutdown_writer()
    except Exception as e:
        logger.warning("[shutdown] DB writer did not stop cleanly: %s", e)
    try:
        from src.database.connection import checkpoint_and_dispose
        checkpoint_and_dispose()
    except Exception as e:
        logger.warning("[shutdown] DB release failed: %s", e)

    # Pass 100: the sync-guard's shutdown hook. It deliberately leaves the
    # data/ exclusion in place (sync_guard.disable is a no-op): a stopped
    # SQLite/ChromaDB store is no safer to file-sync than a running one.
    try:
        from src.services.sync_guard import disable as _sync_guard_disable
        _sync_guard_disable()
    except Exception as e:
        logger.warning("[sync-guard] disable failed: %s", e)

    logger.info("Curatarr shutting down.")


async def _startup_sync_if_needed():
    """Only sync if last sync was more than SYNC_INTERVAL_HOURS ago."""
    await asyncio.sleep(6)
    # Pass 76: skip startup sync while a game is running — the proactive-
    # message generation it triggers (check_and_generate_messages) loads the
    # curator model and can OOM the GPU mid-game. The next scheduled sync (or
    # the one after the game exits) covers it.
    try:
        from src.services.process_monitor import is_game_running
        if is_game_running():
            logger.info("[startup] Game running — skipping startup sync")
            return
    except Exception as e:
        logger.warning("[startup] game check failed - skipping startup sync; "
                       "the schedule covers it: %s", e)
        return
    try:
        from pathlib import Path
        from src.database.connection import get_db_session
        from src.database.models import User

        with get_db_session() as db:
            # ⚡ Bolt: Fast existence check avoiding full table count and ORM instantiation
            if db.query(User.id).filter(User.is_active == True).first() is None:
                logger.info("No users yet — skipping startup sync")
                return

        marker = DATA_DIR / ".last_sync"
        if marker.exists():
            import time
            age_h = (time.time() - marker.stat().st_mtime) / 3600
            if age_h < settings.SYNC_INTERVAL_HOURS:
                logger.info("Last sync %.1fh ago — skipping (interval: %dh)",
                            age_h, settings.SYNC_INTERVAL_HOURS)
                return
            logger.info("Last sync %.1fh ago — running update sync", age_h)
        else:
            logger.info("No sync marker — running initial sync")

        await _startup_sync()

    except Exception as e:
        logger.error("Startup sync check failed: %s", e)


async def _startup_sync():
    """Run Plex sync, write marker, check proactive messages."""
    try:
        from pathlib import Path
        from src.services.plex_sync import sync_plex_history
        from src.database.connection import get_db_session
        from src.database.models import User
        from src.services.proactive_messages import check_and_generate_messages

        result = await sync_plex_history()
        logger.info("Sync: %d new, %d skipped", result.get("synced", 0), result.get("skipped", 0))
        (DATA_DIR / ".last_sync").touch()

        with get_db_session() as db:
            user_ids = [u_id for (u_id,) in db.query(User.id).filter(User.is_active == True).all()]
        for uid in user_ids:
            await check_and_generate_messages(uid)

    except Exception as e:
        logger.error("Sync failed: %s", e)


# ── APP ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Curatarr",
    description="Personal AI media curator for Plex",
    version=settings.VERSION,
    # Pass 97: Swagger gated behind ENABLE_DOCS (default False). The
    # server binds to 0.0.0.0 by default, so a LAN-exposed instance
    # was handing out a free map of every endpoint at /api/docs. Set
    # ENABLE_DOCS=true in .env when you need the API explorer.
    docs_url="/api/docs" if settings.ENABLE_DOCS else None,
    # The gate only hid the Swagger UI: FastAPI kept serving the spec at its
    # default /openapi.json - the actual endpoint map, for anyone on the LAN
    # (caught by the 2026-09 first-run probe). Gate the spec with the UI.
    openapi_url="/api/openapi.json" if settings.ENABLE_DOCS else None,
    redoc_url=None,
    lifespan=lifespan,
)

# A hand-edited "*" origin combined with credentials would reflect ANY
# origin with cookies allowed; Starlette does not stop that, so we do.
_cors_wildcard = "*" in (settings.CORS_ORIGINS or [])
if _cors_wildcard:
    logger.warning("CORS_ORIGINS contains '*' - credentials disabled for CORS")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=not _cors_wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Slow-request telemetry: logs any endpoint taking >300ms to response
# start, so "the page feels slow" can be answered from the log instead of
# guessed at. Time-to-first-byte, so SSE streams don't count as slow.
from src.middleware import (SecurityHeadersMiddleware, SlowRequestLogMiddleware,
                            TokenRefreshMiddleware)
app.add_middleware(SlowRequestLogMiddleware)

# Sliding sessions: a used session never expires mid-click; an idle one
# dies after 7 days. See TokenRefreshMiddleware.
app.add_middleware(TokenRefreshMiddleware)

# Outermost (added last): stamps security headers on every response,
# CORS preflights included. Pure ASGI — SSE-safe (see src/middleware.py).
app.add_middleware(SecurityHeadersMiddleware)

# ── ROUTERS ───────────────────────────────────────────────────────────────────

from src.routers import (
    setup, auth, users, chat, history, libraries,
    recommendations, messages, enrichment, tasks, imports,
)
from src.routers import process_monitor
from src.routers import music
from src.routers import library
from src.routers import image_proxy   # Pass 97
from src.routers import stats
from src.routers import model_updates
from src.routers.auth import require_admin

# Admin-only routers: enrichment runs heavy LLM jobs that block the GPU
# and rewrites server-wide caches; process_monitor mutates Ollama VRAM
# state. Both must be gated server-wide, not per-endpoint.
_ADMIN_ONLY = [Depends(require_admin)]

app.include_router(setup.router,           prefix="/api/setup",           tags=["setup"])
app.include_router(auth.router,            prefix="/api/auth",            tags=["auth"])
app.include_router(users.router,           prefix="/api/users",           tags=["users"])
app.include_router(chat.router,            prefix="/api/chat",            tags=["chat"])
app.include_router(history.router,         prefix="/api/history",         tags=["history"])
app.include_router(libraries.router,       prefix="/api/libraries",       tags=["libraries"])
app.include_router(recommendations.router, prefix="/api/recommendations",  tags=["recommendations"])
app.include_router(messages.router,        prefix="/api/messages",        tags=["messages"])
app.include_router(enrichment.router,      prefix="/api/enrichment",      tags=["enrichment"],
                   dependencies=_ADMIN_ONLY)
app.include_router(tasks.router,           prefix="/api/tasks",           tags=["tasks"])
app.include_router(process_monitor.router, prefix="/api/processes",       tags=["processes"],
                   dependencies=_ADMIN_ONLY)
app.include_router(music.router,           prefix="/api/music",           tags=["music"])
app.include_router(library.router,         prefix="/api/library",         tags=["library"])
app.include_router(image_proxy.router,     prefix="/api/image",           tags=["image"])
app.include_router(stats.router,           prefix="/api/stats",           tags=["stats"])
app.include_router(imports.router,         prefix="/api/import",          tags=["import"])
app.include_router(model_updates.router,   prefix="/api/system/model-updates", tags=["models"],
                   dependencies=_ADMIN_ONLY)

# ── SYSTEM ────────────────────────────────────────────────────────────────────

from src.routers.auth import get_current_user  # noqa: E402


@app.get("/api/system/notifications")
async def system_notifications_route(user=Depends(get_current_user)):
    """The bell's system section (services/notifications.py). Every signed-in
    user may ask; admin-only kinds are filtered by the providers."""
    from src.services.notifications import system_notifications
    return system_notifications(user)


@app.get("/api/system/dependencies", dependencies=_ADMIN_ONLY)
async def system_dependencies():
    """Pinned requirements vs. the interpreter that serves this request —
    the report the lifespan logged, for Settings → Maintenance. Read-only:
    installing is the launchers' job, before the first import."""
    from src.deps_check import check
    from src.deps_lock import check as lock_check
    out = check().as_dict()
    out["lock"] = lock_check().as_dict()   # the tested install vs. this interpreter
    return out


@app.get("/api/system/loop-guard", dependencies=_ADMIN_ONLY)
def system_loop_guard():
    """Database statements that ran on the event-loop thread since start,
    by call site (src/database/loop_guard.py). Each one stalled every
    request for as long as it took."""
    from src.database import loop_guard
    return loop_guard.snapshot()


@app.post("/api/system/shutdown", dependencies=_ADMIN_ONLY)
async def shutdown_server():
    """Graceful shutdown from the web UI (admin only, confirmed client-side).

    Replies first, then raises SIGINT in the event loop — uvicorn then runs
    the exact Ctrl+C path: in-flight requests finish and the lifespan
    teardown executes (pending memory extractions flushed, scheduler
    stopped, app-state flags cleared, sync-guard released). No console
    access needed. Under ``uvicorn --reload`` the reloader is stopped too
    (request_dev_server_stop)."""

    async def _raise_sigint():
        await asyncio.sleep(0.6)   # let the HTTP response leave first
        # End the long-lived SSE streams FIRST (every open tab, including
        # phones) — they otherwise pin "Waiting for connections to close"
        # until each browser tab is closed by hand.
        try:
            from src.services.task_monitor import shutdown_event
            shutdown_event.set()
        except Exception as e:
            # Open SSE tabs would then pin the shutdown until closed by hand.
            logger.warning("[shutdown] could not end SSE streams: %s", e)
        await asyncio.sleep(0.4)   # let the streams drain
        # Tray mode: the launcher registered a stop callback (sets
        # server.should_exit — the graceful path for an embedded server).
        # Dev/CLI mode: nothing registered → the SIGINT path, unchanged.
        from src.services.shutdown_bridge import request_app_stop
        if request_app_stop():
            logger.info("Shutdown requested from the web UI — host stop callback engaged.")
        else:
            path = request_dev_server_stop()
            logger.info("Shutdown requested from the web UI — SIGINT via %s.", path)

    asyncio.create_task(_raise_sigint())
    return {"status": "shutting_down"}


def request_dev_server_stop(*, parent=None, platform=None, kill=None,
                            raise_signal=None, ppid=None) -> str:
    """Stop the server the way Ctrl+C does, also under ``uvicorn --reload``
    (start.bat). Returns which path it took. The keyword arguments are for
    tests.

    Without --reload this process IS the server: SIGINT to itself. Under
    --reload it is a worker the reloader spawned, and the reloader never
    notices a worker that exits (uvicorn's BaseReload only waits for file
    changes): it kept the port and the console, the tray then saw the port
    "in use" by a dead server, and the next save in src/ resurrected it. So
    the reloader is asked instead, ONCE — a second SIGINT makes uvicorn
    force-exit, which skips the lifespan teardown:
      * POSIX: SIGINT to the reloader, which SIGTERMs this worker.
      * Windows: one Ctrl+C to the console the two share (the reloader's own
        restart path uses the same event); each gets it once. cmd.exe may
        then ask "Terminate batch job (Y/N)?" as after a typed Ctrl+C.
    """
    import multiprocessing as _mp
    import signal as _signal
    parent = _mp.parent_process() if parent is None else parent
    platform = sys.platform if platform is None else platform
    kill = os.kill if kill is None else kill
    raise_signal = _signal.raise_signal if raise_signal is None else raise_signal
    if not parent:
        raise_signal(_signal.SIGINT)
        return "self"
    if platform == "win32":
        kill(0, getattr(_signal, "CTRL_C_EVENT", 0))
        return "console"
    kill(os.getppid() if ppid is None else ppid, _signal.SIGINT)
    return "reloader"


# ── FRONTEND ──────────────────────────────────────────────────────────────────

# Resolved once: the catch-all compares candidate.resolve() against this,
# and an unresolved root (symlink, 8.3 short path) can only make that
# comparison fail spuriously — never pass wrongly, but the fallback would
# then serve index.html for a legitimate asset.
_FRONTEND_ROOT = frontend_root().resolve()


@app.get("/")
async def serve_frontend():
    return FileResponse(
        _FRONTEND_ROOT / "index.html",
        headers={"Cache-Control": "no-cache"}
    )


@app.get("/{full_path:path}")
async def catch_all(full_path: str):
    # audit 11g: unknown /api/... paths returned index.html with 200 —
    # a typo in an API call got HTML instead of a 404.
    if full_path.startswith("api"):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Unknown API path")
    """Serve frontend for all non-API routes (SPA routing).

    Resolves the requested path under frontend/ and verifies it stays inside
    the frontend root — guards against ``../`` traversal attempts.
    """
    candidate = (_FRONTEND_ROOT / full_path).resolve()
    try:
        candidate.relative_to(_FRONTEND_ROOT)
    except ValueError:
        # Path escaped the frontend root — fall back to SPA index.
        return FileResponse(
            _FRONTEND_ROOT / "index.html",
            headers={"Cache-Control": "no-cache"}
        )

    if candidate.is_file():
        headers = {}
        if full_path.startswith("js/") or full_path.startswith("css/"):
            headers["Cache-Control"] = "no-cache"
        return FileResponse(candidate, headers=headers)
    return FileResponse(
        _FRONTEND_ROOT / "index.html",
        headers={"Cache-Control": "no-cache"}
    )
