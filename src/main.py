"""
Curatarr - Main Application Entry Point
"""

import asyncio
import logging
import os
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
    # Pass 100: stop Syncthing from touching the live SQLite DB while we run.
    # Syncing an open WAL-mode database causes "database is locked" errors and
    # risks corruption. The guard excludes data/ from the enclosing Syncthing
    # folder's .stignore now and restores it on clean shutdown (no-op when
    # Syncthing isn't present). Done first, before we open the DB.
    try:
        from src.services.sync_guard import enable as _sync_guard_enable
        _sync_guard_enable()
    except Exception as e:
        logger.debug("[sync-guard] enable failed: %s", e)

    (DATA_DIR / "chromadb").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "cache").mkdir(parents=True, exist_ok=True)

    from src.database.connection import init_db
    init_db()

    from src.services.scheduler import start_scheduler
    start_scheduler()

    # Reset any stuck flags from previous crash
    try:
        from src.services.app_state import set_state
        set_state("enrichment_running", "0")
        set_state("music_pipeline_running", "0")
        set_state("music_pipeline_stop_requested", "0")
        set_state("deletion_run", "0")
        # Pass 75/76: seed the game flag with the ACTUAL current state, not
        # a blind "0". A blind reset left a ~30 s window (until the watcher's
        # first tick) where game_active was wrong — long enough for the
        # startup catch-ups to load the LLM and OOM a running game. A direct
        # is_game_running() check here makes the flag correct from t=0.
        from src.services.process_monitor import is_game_running as _igr
        set_state("game_active", "1" if _igr() else "0")
    except Exception:
        pass

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
            logger.debug("Anime mapping load failed: %s", e)
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
            logger.debug("Library prewarm failed: %s", e)
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
                    "LAN needs this one-time code: %s  (a browser on this machine "
                    "does not).", SETUP_CODE)
    except Exception as _e:  # noqa: BLE001
        logger.debug("setup-code notice skipped: %s", _e)
    else:
        from src.database.connection import get_db_session
        from src.database.models import User
        with get_db_session() as db:
            user_count = db.query(User).count()
        if user_count == 0:
            logger.info("No users yet — first login via Plex will create the admin account.")

        if settings.SYNC_ON_STARTUP:
            track_task(_startup_sync_if_needed(), name="startup_sync_if_needed")

    yield

    # Pass 61: flush any pending debounced memory extractions before we go
    # down — otherwise a restart inside the 90s debounce window drops the
    # last conversation's memories entirely.
    try:
        from src.services.episodic_memory import flush_all_pending_extractions
        await flush_all_pending_extractions()
    except Exception as e:
        logger.debug("Shutdown memory flush failed: %s", e)

    from src.services.scheduler import stop_scheduler
    stop_scheduler()
    try:
        from src.services.app_state import set_state
        set_state("enrichment_running", "0")
        set_state("music_pipeline_running", "0")
        set_state("music_pipeline_stop_requested", "0")
        set_state("deletion_run", "0")
    except Exception:
        pass

    # Cancel the idle-evict timer and free the VRAM on the way out — the
    # most common reason this app gets closed is that the GPU is wanted for
    # a game, and leaving gemma sitting in 20 GB of VRAM until Ollama's own
    # keep_alive expires defeats exactly that.
    try:
        from src.services.llm_priority import shutdown_evict
        await shutdown_evict()
    except Exception as e:
        logger.debug("[shutdown] evict pass failed: %s", e)

    # Pass 100: re-enable Syncthing for data/ now that the DB is released, so
    # the database can sync between machines while Curatarr is stopped. Done
    # last, after all shutdown DB writes above have completed.
    try:
        from src.services.sync_guard import disable as _sync_guard_disable
        _sync_guard_disable()
    except Exception as e:
        logger.debug("[sync-guard] disable failed: %s", e)

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
    except Exception:
        pass
    try:
        from pathlib import Path
        from src.database.connection import get_db_session
        from src.database.models import User

        with get_db_session() as db:
            if db.query(User).filter(User.is_active == True).count() == 0:
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

# ── SYSTEM ────────────────────────────────────────────────────────────────────

@app.post("/api/system/shutdown", dependencies=_ADMIN_ONLY)
async def shutdown_server():
    """Graceful shutdown from the web UI (admin only, confirmed client-side).

    Replies first, then raises SIGINT in the event loop — uvicorn then runs
    the exact Ctrl+C path: in-flight requests finish and the lifespan
    teardown executes (pending memory extractions flushed, scheduler
    stopped, app-state flags cleared, sync-guard released). No console
    access needed."""
    import signal

    async def _raise_sigint():
        await asyncio.sleep(0.6)   # let the HTTP response leave first
        # End the long-lived SSE streams FIRST (every open tab, including
        # phones) — they otherwise pin "Waiting for connections to close"
        # until each browser tab is closed by hand.
        try:
            from src.services.task_monitor import shutdown_event
            shutdown_event.set()
        except Exception:
            pass
        await asyncio.sleep(0.4)   # let the streams drain
        # Tray mode: the launcher registered a stop callback (sets
        # server.should_exit — the graceful path for an embedded server).
        # Dev/CLI mode: nothing registered → the SIGINT path, unchanged.
        from src.services.shutdown_bridge import request_app_stop
        if request_app_stop():
            logger.info("Shutdown requested from the web UI — host stop callback engaged.")
        else:
            logger.info("Shutdown requested from the web UI — raising SIGINT.")
            signal.raise_signal(signal.SIGINT)

    asyncio.create_task(_raise_sigint())
    return {"status": "shutting_down"}


# ── FRONTEND ──────────────────────────────────────────────────────────────────

_FRONTEND_ROOT = frontend_root()


@app.get("/")
async def serve_frontend():
    return FileResponse(_FRONTEND_ROOT / "index.html")


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
        return FileResponse(_FRONTEND_ROOT / "index.html")

    if candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(_FRONTEND_ROOT / "index.html")
