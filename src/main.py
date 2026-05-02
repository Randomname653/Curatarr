"""
Curatarr 1.0 - Main Application Entry Point
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.config import settings

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger("curatarr")

# ── STARTUP / SHUTDOWN ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    Path("data/chromadb").mkdir(parents=True, exist_ok=True)
    Path("data/cache").mkdir(parents=True, exist_ok=True)

    from src.database.connection import init_db
    init_db()

    from src.services.scheduler import start_scheduler
    start_scheduler()

    # Reset any stuck flags from previous crash
    try:
        from src.services.app_state import set_state
        set_state("enrichment_running", "0")
    except Exception:
        pass

    # Pre-load anime mapping in background
    async def _load_anime_mapping():
        try:
            from src.services.anime_mapping import get_anime_mapping
            m = await get_anime_mapping()
            logger.info("Anime mapping ready: %d entries", m.total_entries)
        except Exception as e:
            logger.debug("Anime mapping load failed: %s", e)
    asyncio.create_task(_load_anime_mapping())

    if not settings.is_configured:
        logger.warning("Curatarr is not configured yet. Open http://localhost:%d to run setup.", settings.PORT)
    else:
        from src.database.connection import get_db_session
        from src.database.models import User
        with get_db_session() as db:
            user_count = db.query(User).count()
        if user_count == 0:
            logger.info("No users yet — first login via Plex will create the admin account.")

        if settings.SYNC_ON_STARTUP:
            asyncio.create_task(_startup_sync_if_needed())

    yield

    from src.services.scheduler import stop_scheduler
    stop_scheduler()
    try:
        from src.services.app_state import set_state
        set_state("enrichment_running", "0")
    except Exception:
        pass
    logger.info("Curatarr shutting down.")


async def _startup_sync_if_needed():
    """Only sync if last sync was more than SYNC_INTERVAL_HOURS ago."""
    await asyncio.sleep(6)
    try:
        from pathlib import Path
        from src.database.connection import get_db_session
        from src.database.models import User

        with get_db_session() as db:
            if db.query(User).filter(User.is_active == True).count() == 0:
                logger.info("No users yet — skipping startup sync")
                return

        marker = Path("data/.last_sync")
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
        Path("data/.last_sync").touch()

        with get_db_session() as db:
            user_ids = [u.id for u in db.query(User).filter(User.is_active == True).all()]
        for uid in user_ids:
            await check_and_generate_messages(uid)

    except Exception as e:
        logger.error("Sync failed: %s", e)


# ── APP ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Curatarr",
    description="Personal AI media curator for Plex",
    version=settings.VERSION,
    docs_url="/api/docs" if settings.DEBUG else None,
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── ROUTERS ───────────────────────────────────────────────────────────────────

from src.routers import (
    setup, auth, users, chat, history, libraries,
    recommendations, messages, memories, enrichment, tasks,
)

app.include_router(setup.router,           prefix="/api/setup",           tags=["setup"])
app.include_router(auth.router,            prefix="/api/auth",            tags=["auth"])
app.include_router(users.router,           prefix="/api/users",           tags=["users"])
app.include_router(chat.router,            prefix="/api/chat",            tags=["chat"])
app.include_router(history.router,         prefix="/api/history",         tags=["history"])
app.include_router(libraries.router,       prefix="/api/libraries",       tags=["libraries"])
app.include_router(recommendations.router, prefix="/api/recommendations",  tags=["recommendations"])
app.include_router(messages.router,        prefix="/api/messages",        tags=["messages"])
app.include_router(memories.router,        prefix="/api/memories",        tags=["memories"])
app.include_router(enrichment.router,      prefix="/api/enrichment",      tags=["enrichment"])
app.include_router(tasks.router,           prefix="/api/tasks",           tags=["tasks"])

# ── FRONTEND ──────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_frontend():
    return FileResponse("frontend/index.html")

@app.get("/{full_path:path}")
async def catch_all(full_path: str):
    """Serve frontend for all non-API routes (SPA routing)."""
    static = Path("frontend") / full_path
    if static.exists() and static.is_file():
        return FileResponse(static)
    return FileResponse("frontend/index.html")
