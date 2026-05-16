"""
Curatarr 1.0 - Background Scheduler

All scheduled jobs in one place. Started at app startup.

Schedule:
  Every 24h  — Plex sync (if new items found: recompute taste + cache recs)
  Every 24h  — ARR incremental sync
  Every 6h   — Check for proactive messages (binge/marathon/completion detection)
  Every 7d   — Memory decay (reduce importance of old memories)
  Every 7d   — Orphaned section check (notify admin if Plex sections are unmapped)
  02:30      — ARR pre-enrichment batch (fills rating/genre cache before deletion sync)
  03:30      — Enrichment TTL refresh (queues stale items for re-enrichment)
  04:00      — Music pipeline (Plex match + Last.fm genre enrichment)
  On demand  — Triggered after enrichment completes: recompute taste + cache recs
"""

import asyncio
import functools
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from src.config import settings

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
_started = False


# ── Pass 15a: missed-job replay infrastructure ──────────────────────────────
#
# Curatarr is a self-hosted app, not a 24/7 service. APScheduler skips
# missed firings by default — daily-sync, music-pipeline and weekly jobs
# silently lose their slots when the laptop is off. We persist the last
# successful run timestamp per job in AppState and check on startup
# whether each job is overdue. If yes: fire ONCE (catch-up consolidates
# multiple missed intervals into a single run — running 5 daily syncs
# back-to-back wouldn't produce 5× the value).

def _record_job_run(job_id: str) -> None:
    """Persist last successful run timestamp for a scheduled job."""
    from src.services.app_state import set_state
    set_state(f"job_last_run:{job_id}", datetime.utcnow().isoformat())


def _last_job_run(job_id: str) -> datetime | None:
    """Return last successful run timestamp for a job, or None."""
    from src.services.app_state import get_state
    raw = get_state(f"job_last_run:{job_id}")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def _job_overdue(job_id: str, interval_hours: float, grace_factor: float = 1.1) -> bool:
    """True when the job hasn't run within its interval (× grace factor).

    Grace factor of 1.1 means a 24h job is only "overdue" after ~26.4h —
    avoids firing catch-ups for jobs that were just slightly delayed by a
    normal restart.
    """
    last = _last_job_run(job_id)
    if not last:
        return True   # never ran → fire once
    return (datetime.utcnow() - last) > timedelta(hours=interval_hours * grace_factor)


def _tracked(job_id: str):
    """Decorator: record successful run timestamp after the job completes.

    Failed runs DON'T record — that way the next startup catch-up still
    triggers and we get another chance.
    """
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            result = await fn(*args, **kwargs)
            _record_job_run(job_id)
            return result
        return wrapped
    return decorator

# Module-level set of fire-and-forget background tasks. Without retaining a
# reference, asyncio's GC may cancel a still-running task at any time. Tasks
# add themselves on creation and remove themselves on completion via
# add_done_callback, so the set never grows unbounded.
_background_tasks: set[asyncio.Task] = set()


def _track_task(task: asyncio.Task) -> asyncio.Task:
    """Retain a reference to *task* until it completes, log any exception."""
    _background_tasks.add(task)
    def _on_done(t: asyncio.Task):
        _background_tasks.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc:
                logger.error("[scheduler] background task raised: %s", exc, exc_info=exc)
    task.add_done_callback(_on_done)
    return task


def _gaming() -> bool:
    """True when the game watcher has flagged a game as running. LLM-heavy
    scheduler jobs check this and skip — no point fighting the game for VRAM
    (Pass 75)."""
    try:
        from src.services.app_state import get_state
        return get_state("game_active") == "1"
    except Exception:
        return False


async def job_game_watcher():
    """Pass 75: server-side game-mode enforcement — frontend-independent.

    Runs every ~30 s. The old design only unloaded models on the
    not-running→running transition detected by the FRONTEND polling
    ``/api/processes/status`` — so if the web UI wasn't open, or the
    ``game_was_running`` flag had gone stale (Curatarr restart mid-game, a
    running→stopped transition the UI never observed), the unload never
    fired; and even when it did it was a one-shot that any later LLM call
    reloaded past.

    This watcher owns the truth: it sets the ``game_active`` AppState flag
    the LLM-heavy jobs check, and while a game runs it re-unloads every tick.
    ``unload_llm_models`` is /api/ps-aware, so a tick with VRAM already clear
    is just one cheap GET.
    """
    try:
        from src.services.process_monitor import is_game_running, unload_llm_models
        from src.services.app_state import set_state
        running = is_game_running()
        set_state("game_active", "1" if running else "0")
        if running:
            unloaded = await unload_llm_models()
            if unloaded:
                logger.info("[game watcher] game active — evicted from VRAM: %s",
                            ", ".join(unloaded))
    except Exception as e:
        logger.debug("[game watcher] tick failed: %s", e)


def start_scheduler():
    global _started
    if _started:
        return
    _started = True

    scheduler.add_job(
        _tracked("plex_sync")(job_plex_sync),
        IntervalTrigger(hours=24),
        id="plex_sync",
        name="Daily Plex sync",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        _tracked("arr_sync")(job_arr_sync),
        IntervalTrigger(hours=24),
        id="arr_sync",
        name="Daily ARR sync",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        _tracked("proactive_messages")(job_proactive_messages),
        IntervalTrigger(minutes=30),
        id="proactive_messages",
        name="Proactive message cache fill",
        replace_existing=True,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        _tracked("memory_decay")(job_memory_decay),
        CronTrigger(day_of_week="sun", hour=3),
        id="memory_decay",
        name="Weekly memory decay",
        replace_existing=True,
        misfire_grace_time=7200,
    )
    scheduler.add_job(
        _tracked("orphan_check")(job_orphan_check),
        CronTrigger(day_of_week="sun", hour=4),
        id="orphan_check",
        name="Weekly orphaned section check",
        replace_existing=True,
        misfire_grace_time=7200,
    )
    scheduler.add_job(
        _tracked("arr_pre_enrich")(job_arr_pre_enrich),
        CronTrigger(hour=2, minute=30),
        id="arr_pre_enrich",
        name="Daily ARR pre-enrichment batch",
        replace_existing=True,
        misfire_grace_time=7200,
    )
    scheduler.add_job(
        _tracked("enrichment_ttl_refresh")(job_enrichment_ttl_refresh),
        CronTrigger(hour=3, minute=30),
        id="enrichment_ttl_refresh",
        name="Daily enrichment TTL refresh",
        replace_existing=True,
        misfire_grace_time=7200,
    )
    scheduler.add_job(
        _tracked("music_pipeline")(job_music_pipeline),
        CronTrigger(hour=4, minute=0),
        id="music_pipeline",
        name="Daily music match + Last.fm enrichment",
        replace_existing=True,
        misfire_grace_time=7200,
    )
    # Pass 15b: weekly DB vacuum off-hours. Reclaims free SQLite pages
    # without touching enrichment data.
    scheduler.add_job(
        _tracked("db_vacuum")(job_db_vacuum),
        CronTrigger(day_of_week="sun", hour=4, minute=30),
        id="db_vacuum",
        name="Weekly DB vacuum",
        replace_existing=True,
        misfire_grace_time=14400,
    )
    # Pass 16o: keep Library Manager arr cache warm. Catches recovery
    # windows for a flaky Lidarr without the user having to click.
    scheduler.add_job(
        _tracked("arr_cache_refresh")(job_arr_cache_refresh),
        IntervalTrigger(minutes=30),
        id="arr_cache_refresh",
        name="Arr library cache refresh",
        replace_existing=True,
        misfire_grace_time=900,
    )
    # Pass 75: server-side game-mode watcher. Frequent + lightweight (one
    # psutil scan + one Ollama /api/ps GET). NOT _tracked — catch-up makes no
    # sense for a 30 s heartbeat. This is what actually keeps VRAM clear for
    # the game; /api/processes/status is now just a UI read.
    scheduler.add_job(
        job_game_watcher,
        IntervalTrigger(seconds=30),
        id="game_watcher",
        name="Game-mode VRAM watcher",
        replace_existing=True,
        misfire_grace_time=30,
    )

    scheduler.start()
    logger.info("Scheduler started: plex_sync(24h), arr_sync(24h), arr_pre_enrich(02:30), "
                "proactive(30min), memory_decay(weekly), orphan_check(weekly), "
                "db_vacuum(weekly), arr_cache_refresh(30min), game_watcher(30s)")

    # Run startup check — cache recommendations if missing, sync if overdue.
    # Retained via _track_task so asyncio's GC can't cancel it mid-flight.
    _track_task(asyncio.create_task(_startup_check(), name="startup_check"))


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)


# ── JOBS ──────────────────────────────────────────────────────────────────────

async def _startup_check():
    """On startup: cache recommendations if empty, sync if overdue."""
    import asyncio
    await asyncio.sleep(5)  # wait for DB to be ready

    # Pass 76: do NOT do startup LLM work while a game is running. Both the
    # rec-cache generation below and the overdue catch-ups (job_arr_sync ->
    # deletion-proposal pitches, job_plex_sync -> proactive messages) load
    # the curator model — on top of a game's VRAM use that can OOM the GPU
    # and crash the game. Direct is_game_running() check, NOT the game_active
    # flag: at this point the flag may still be "0" (lifespan just seeded it,
    # the watcher's first tick is ~30 s out). The normal schedule + the
    # watcher pick all of this up once the game exits.
    try:
        from src.services.process_monitor import is_game_running
        if is_game_running():
            logger.info("[startup] Game running — skipping rec-cache + overdue catch-ups")
            return
    except Exception as _e:
        logger.debug("[startup] game check failed: %s", _e)

    try:
        from src.database.connection import get_db_session
        from src.database.models import User, CachedRecommendation
        from src.services.app_state import get_state
        from datetime import datetime, timedelta

        with get_db_session() as db:
            admin = db.query(User).filter_by(is_admin=True).first()
            if not admin:
                return
            user_id = admin.id
            rec_count = db.query(CachedRecommendation).filter(
                CachedRecommendation.user_id == user_id
            ).count()

        # Cache recommendations if none exist
        if rec_count == 0:
            logger.info("[startup] No cached recommendations — generating now")
            await _cache_recommendations(user_id)

        # Pass 15a: missed-job catch-up. Each scheduled job persists its
        # last successful run timestamp via _tracked(). On startup we fire
        # any job whose interval has elapsed. Catch-ups are CONSOLIDATED:
        # a job that missed 5 daily windows still only runs once.
        catchup_jobs = [
            ("plex_sync",             24.0,  job_plex_sync),
            ("arr_sync",              24.0,  job_arr_sync),
            ("memory_decay",          168.0, job_memory_decay),
            ("orphan_check",          168.0, job_orphan_check),
            ("arr_pre_enrich",        24.0,  job_arr_pre_enrich),
            ("enrichment_ttl_refresh", 24.0, job_enrichment_ttl_refresh),
            ("music_pipeline",        24.0,  job_music_pipeline),
            ("db_vacuum",             168.0, job_db_vacuum),
            # proactive_messages NOT included — 30min interval, catch-up
            # would be noise.
        ]
        for job_id, interval_h, fn in catchup_jobs:
            if _job_overdue(job_id, interval_h):
                logger.info("[startup] catch-up: %s overdue — firing once", job_id)
                try:
                    await fn()
                    _record_job_run(job_id)
                except Exception as e:
                    logger.warning("[startup] catch-up %s failed: %s — will retry next startup",
                                   job_id, e)

        # Resume enrichment if there are unfinished items (stopped mid-run or
        # game-mode items waiting for LLM). Runs in background after a short delay
        # so the API is fully ready before the first LLM call.
        await _resume_enrichment_if_needed(user_id)

        # Resume music pipeline if it was interrupted before completing
        await _resume_music_pipeline_if_needed(user_id)

    except Exception as e:
        logger.debug("[startup] Startup check failed: %s", e)


async def _resume_music_pipeline_if_needed(user_id: int):
    """Resume music pipeline if it was interrupted (e.g. server shutdown mid-run)."""
    import asyncio
    try:
        from src.services.app_state import get_state, set_state
        if get_state("music_pipeline_interrupted") != "1":
            return
        logger.info("[startup] Music pipeline was interrupted — resuming automatically")
        await asyncio.sleep(15)  # give the API a moment to be fully ready

        from src.services.app_state import get_state as _gs
        if _gs("music_pipeline_running") == "1":
            return  # already running (shouldn't happen after flag reset on startup)

        set_state("music_pipeline_stop_requested", "0")
        _track_task(asyncio.create_task(
            _run_music_pipeline_bg(user_id),
            name=f"music_pipeline_resume_user_{user_id}",
        ))
    except Exception as e:
        logger.debug("[startup] Music pipeline resume check failed: %s", e)


async def _run_music_pipeline_bg(user_id: int):
    """Thin wrapper so the scheduler can fire _run_music_pipeline without importing router."""
    try:
        from src.routers.music import _run_music_pipeline
        await _run_music_pipeline(user_id, batch=300)
    except Exception as e:
        logger.error("[music_pipeline] Background run failed: %s", e)


async def _resume_enrichment_if_needed(user_id: int):
    """Auto-resume enrichment only when a previous run was interrupted (not completed).
    Uses the exact same categories/source/limit as the interrupted run."""
    import asyncio
    import json as _json
    try:
        from src.services.app_state import get_state

        if get_state("enrichment_interrupted") != "1":
            return  # last run finished normally — nothing to resume

        raw_settings = get_state("enrichment_last_settings")
        if not raw_settings:
            return

        saved = _json.loads(raw_settings)
        categories = saved.get("categories") or ["music", "movie", "show", "anime"]
        source     = saved.get("source") or "both"
        limit      = saved.get("limit")   # None = no limit

        logger.info(
            "[startup] Enrichment was interrupted — resuming with categories=%s source=%s limit=%s",
            categories, source, limit,
        )
        await asyncio.sleep(10)  # let the API finish initialising

        from src.routers.enrichment import _run_enrichment
        # Atomic compare-and-set: lose the race, return; win it, schedule.
        from src.services.app_state import acquire_state_lock, release_state_lock
        if not acquire_state_lock("enrichment_running"):
            return  # already started by something else (route or another resume)

        # Pass 46 (Bug 3): release the lock if we crash BEFORE handing it
        # off to ``_run_enrichment``'s own finally-block. Once the task is
        # successfully created the ownership transfers — releasing here
        # would pull the lock out from under the running task. Hence the
        # ``handed_off`` flag.
        handed_off = False
        try:
            _track_task(asyncio.create_task(
                _run_enrichment(user_id, categories, source, limit, False),
                name=f"enrichment_resume_user_{user_id}",
            ))
            handed_off = True
        finally:
            if not handed_off:
                release_state_lock("enrichment_running")
    except Exception as e:
        logger.debug("[startup] Enrichment resume check failed: %s", e)


async def job_plex_sync():
    """Daily Plex sync. If new items found, chains taste recompute + recs cache."""
    logger.info("[scheduler] Starting daily Plex sync")
    try:
        from src.services.plex_sync import sync_plex_history
        from src.services.task_monitor import task_monitor

        result = await sync_plex_history(force=False)
        synced = result.get("synced", 0)
        logger.info("[scheduler] Plex sync complete: %d new entries", synced)

        if synced > 0:
            # Chain: taste recompute → recommendations cache
            await _recompute_and_cache_recs()
    except Exception as e:
        logger.error("[scheduler] Plex sync failed: %s", e)


async def job_arr_sync():
    """Daily ARR sync — refreshes deletion proposal candidates."""
    logger.info("[scheduler] Starting daily ARR sync")
    from src.services.task_monitor import task_monitor
    task = task_monitor.create(name="ARR Sync", category="arr_sync")
    task_monitor.start(task)
    if _gaming():
        task_monitor.skip(task, "Game running — skipping to leave VRAM for the game")
        return
    try:
        from src.database.connection import get_db_session
        from src.database.models import User, DeletionProposal
        from src.routers.recommendations import _fetch_arr_candidates
        from src.services.recommendations_engine import generate_deletion_proposals
        from src.services.app_state import set_state

        with get_db_session() as db:
            admin = db.query(User).filter_by(is_admin=True).first()
            if not admin:
                task_monitor.skip(task, "No admin user")
                return
            user_id = admin.id

        task_monitor.update(task, message="Fetching ARR library")
        arr_items = await _fetch_arr_candidates()
        if not arr_items:
            task_monitor.skip(task, "No ARR items or ARR not configured")
            logger.info("[scheduler] ARR sync: no items or no ARR configured")
            return

        task_monitor.update(task, total=len(arr_items), message=f"Analysing {len(arr_items)} items")
        all_proposals = []
        for cat in ["movie", "show", "anime", "music"]:
            cat_items = [i for i in arr_items if i.get("category") == cat]
            if not cat_items:
                continue
            cat_proposals = await generate_deletion_proposals(user_id, cat_items, cat)
            all_proposals.extend(cat_proposals)

        if not all_proposals:
            task_monitor.done(task, "No deletion proposals generated")
            return

        # Group proposals by category and replace them wholesale — same logic as
        # the manual refresh in the recommendations router.  This ensures:
        #   • old proposals (including NULL-category legacy rows) are wiped
        #   • every new row gets the correct category column set
        #   • no stale duplicates survive across scheduler runs
        from sqlalchemy import or_, and_
        _CAT_TO_SVC = {"movie": "radarr", "show": "sonarr", "anime": "sonarr", "music": "lidarr"}
        by_cat: dict[str, list] = {}
        for p in all_proposals:
            by_cat.setdefault(p.get("category", "movie"), []).append(p)

        with get_db_session() as db:
            added = 0
            for cat, cat_proposals in by_cat.items():
                svc = _CAT_TO_SVC.get(cat, "")
                # Pass 90b: SUPERSEDE stale pending rows instead of
                # hard-DELETE (handles both category-column match AND
                # legacy NULL-category rows by service). The earlier
                # hard-delete freed ROWIDs that SQLite (without
                # AUTOINCREMENT) reused for the new INSERTs below; stale
                # frontend caches holding old proposal_ids then resolved
                # to the WRONG title on follow-up requests (see Pass 90a's
                # commentary in chat.py). Soft-delete preserves IDs (no
                # reuse possible for these rows), gives us a superseded-
                # audit-trail, and is silently ignored by every status
                # filter elsewhere in the codebase (all look for
                # ``pending`` / ``limbo`` / ``rejected`` / ``deleted``).
                db.query(DeletionProposal).filter(
                    DeletionProposal.user_id == user_id,
                    DeletionProposal.status == "pending",
                    or_(
                        DeletionProposal.category == cat,
                        and_(
                            DeletionProposal.category.is_(None),
                            DeletionProposal.service == svc,
                        ),
                    ),
                ).update(
                    {"status": "superseded", "resolved_at": datetime.utcnow()},
                    synchronize_session=False,
                )

                for p in cat_proposals:
                    db.add(DeletionProposal(
                        user_id=user_id,
                        media_id=str(p.get("arr_id", "")),
                        title=p["title"],
                        service=p.get("service", ""),
                        arr_url=p.get("arr_url", ""),
                        reason=p["pitch"],
                        confidence=p["confidence"],
                        storage_mb=p.get("size_mb", 0),
                        status="pending",
                        category=cat,                    # ← was missing before
                        poster_url=p.get("poster_url"),
                        synopsis=p.get("synopsis"),
                        genres=p.get("genres"),
                    ))
                    added += 1
            db.commit()

        set_state("last_arr_sync_at", datetime.utcnow().isoformat())
        task_monitor.done(task, f"Complete: {added} proposals (replaced per category)")
        logger.info("[scheduler] ARR sync complete: %d proposals written", added)
    except Exception as e:
        task_monitor.error(task, str(e))
        logger.error("[scheduler] ARR sync failed: %s", e)


async def job_proactive_messages():
    """Check for binge/marathon/completion patterns and generate messages."""
    logger.info("[scheduler] Checking proactive messages")
    from src.services.task_monitor import task_monitor
    task = task_monitor.create(name="Proactive Messages", category="proactive")
    task_monitor.start(task)
    if _gaming():
        task_monitor.skip(task, "Game running — skipping to leave VRAM for the game")
        return
    try:
        from src.database.connection import get_db_session
        from src.database.models import User
        from src.services.proactive_messages import check_and_generate_messages

        with get_db_session() as db:
            users = [{"id": u.id} for u in db.query(User).filter_by(is_active=True).all()]

        task_monitor.update(task, total=len(users))
        for i, u in enumerate(users):
            await check_and_generate_messages(u["id"])
            task_monitor.update(task, processed=i + 1)

        task_monitor.done(task, f"Checked {len(users)} user(s)")
        logger.info("[scheduler] Proactive messages checked for %d users", len(users))
    except Exception as e:
        task_monitor.error(task, str(e))
        logger.error("[scheduler] Proactive messages failed: %s", e)


async def job_memory_decay():
    """Weekly: reduce importance of old memories."""
    logger.info("[scheduler] Running memory decay")
    from src.services.task_monitor import task_monitor
    task = task_monitor.create(name="Memory Decay", category="memory_decay")
    task_monitor.start(task)
    try:
        from src.database.connection import get_db_session
        from src.database.models import EpisodicMemory
        from datetime import timedelta

        cutoff = datetime.utcnow() - timedelta(days=90)
        decayed = 0

        with get_db_session() as db:
            old = db.query(EpisodicMemory).filter(
                EpisodicMemory.created_at < cutoff,
                EpisodicMemory.importance > 0.1,
            ).all()
            task_monitor.update(task, total=len(old))
            for m in old:
                m.importance = max(0.1, m.importance * 0.85)
                decayed += 1
            db.commit()

        task_monitor.done(task, f"{decayed} memories decayed")
        logger.info("[scheduler] Memory decay: %d memories decayed", decayed)
    except Exception as e:
        task_monitor.error(task, str(e))
        logger.error("[scheduler] Memory decay failed: %s", e)


async def job_orphan_check():
    """Weekly: detect orphaned Plex sections and notify admin via proactive message."""
    logger.info("[scheduler] Running orphaned section check")
    from src.services.task_monitor import task_monitor
    task = task_monitor.create(name="Orphan Check", category="orphan_check")
    task_monitor.start(task)
    try:
        from src.services.orphan_repair import detect_orphaned_sections
        orphans = await detect_orphaned_sections()
        if not orphans:
            task_monitor.done(task, "No orphaned sections")
            logger.info("[scheduler] No orphaned sections found")
            return

        from src.database.connection import get_db_session
        from src.database.models import User, ProactiveMessage
        import json

        with get_db_session() as db:
            admin = db.query(User).filter_by(is_admin=True).first()
            if not admin:
                task_monitor.skip(task, "No admin user")
                return
            summary = ", ".join(
                f"section {o['section_id']} ({o['count']} items)" for o in orphans[:3]
            )
            db.add(ProactiveMessage(
                user_id=admin.id,
                trigger_type="orphan_sections",
                trigger_data=json.dumps(orphans),
                message=(
                    f"Curatarr found {len(orphans)} orphaned Plex section(s) "
                    f"({summary}) that are not mapped to a library category. "
                    "Open Settings → Library to review and remap them."
                ),
            ))
            db.commit()

        task_monitor.done(task, f"{len(orphans)} orphaned section(s) found")
        logger.info("[scheduler] Orphan check: %d orphaned sections found, admin notified", len(orphans))
    except Exception as e:
        task_monitor.error(task, str(e))
        logger.error("[scheduler] Orphan check failed: %s", e)


async def job_arr_pre_enrich():
    """
    Daily at 02:30: enrich a batch of unenriched ARR library items so the
    deletion-proposal engine always has rating + genre data when it runs.

    Runs BEFORE job_arr_sync (daily variable time) and job_enrichment_ttl_refresh
    (03:30) so newly queued items are included in the same overnight window.

    Batch size: ARR_PRE_ENRICH_BATCH (default 80) — small enough to finish in
    ~20-30 min on typical hardware, leaving the night free for music pipeline.
    Items already fully LLM-enriched (ArrEnrichmentStatus.enriched=True) are
    skipped automatically by _run_enrichment's pre-filter logic.
    """
    logger.info("[scheduler] Starting ARR pre-enrichment batch")
    from src.services.task_monitor import task_monitor
    from src.services.app_state import get_state

    task = task_monitor.create(name="ARR Pre-Enrichment", category="enrichment")
    task_monitor.start(task)

    try:
        from src.database.connection import get_db_session
        from src.database.models import User

        # Must resolve to a real user ID — use admin. Read ``.id`` INSIDE the
        # session block: get_db_session() commits on exit, and the default
        # expire_on_commit=True then marks every attribute on ``admin`` stale —
        # touching ``admin.id`` afterwards tries to refresh it against a closed
        # session → DetachedInstanceError. Every other scheduler job resolves
        # the id inside the block; this one closed it a line too early.
        with get_db_session() as db:
            admin = db.query(User).filter(User.is_active == True).first()
            if not admin:
                task_monitor.skip(task, "No active users — skipping ARR pre-enrichment")
                return
            user_id = admin.id

        # Don't start if full enrichment is already running. Atomic
        # compare-and-set so a /start POST coming in at the same moment
        # can't race past us.
        from src.services.app_state import acquire_state_lock, release_state_lock
        if not acquire_state_lock("enrichment_running"):
            task_monitor.skip(task, "Enrichment already running — skipping pre-enrich")
            return

        # Pass 46 (Bug 3): the awaited ``_run_enrichment`` releases the
        # lock in its own finally on the happy path. Wrap our setup in a
        # try/finally so a crash BEFORE that finally registers (import
        # error, batch math, task_monitor update) still clears the lock.
        # Double-release is safe: ``release_state_lock`` is just
        # ``set_state(key, "0")`` — idempotent.
        try:
            batch = int(getattr(settings, "ARR_PRE_ENRICH_BATCH", 80))
            task_monitor.update(task, message=f"Enriching up to {batch} ARR items (movie/show/anime/music)")

            from src.routers.enrichment import _run_enrichment
            # source="arr" — only look at ARR items, not watch history
            # limit=batch   — cap per run to keep runtime predictable
            # force=False   — skip already-LLM-enriched items
            await _run_enrichment(
                user_id=user_id,
                categories=["movie", "show", "anime", "music"],
                source="arr",
                limit=batch,
                force=False,
            )

            task_monitor.done(task, f"ARR pre-enrichment batch complete (up to {batch} items)")
            logger.info("[scheduler] ARR pre-enrichment done (batch=%d)", batch)
        finally:
            release_state_lock("enrichment_running")

    except Exception as e:
        task_monitor.error(task, str(e))
        logger.error("[scheduler] ARR pre-enrichment failed: %s", e, exc_info=True)


async def job_enrichment_ttl_refresh():
    """
    Daily: mark the oldest N LLM-enriched items as pending again so they get
    refreshed on the next enrichment run. Staggered to avoid big-bang expiry.

    With ENRICHMENT_REFRESH_BATCH_SIZE=150 and a 10k-title library the full
    cycle completes in ~67 days — well within the 90-day TTL window.
    """
    logger.info("[scheduler] Starting enrichment TTL refresh")
    from src.services.task_monitor import task_monitor
    task = task_monitor.create(name="Enrichment TTL Refresh", category="enrichment")
    task_monitor.start(task)
    try:
        from src.database.connection import get_db_session
        from src.database.models import EnrichmentStatus

        cutoff = datetime.utcnow() - timedelta(days=settings.ENRICHMENT_TTL_DAYS)
        batch  = settings.ENRICHMENT_REFRESH_BATCH_SIZE

        with get_db_session() as db:
            expired = (
                db.query(EnrichmentStatus)
                .filter(
                    EnrichmentStatus.enriched   == True,
                    EnrichmentStatus.error.is_(None),
                    EnrichmentStatus.enriched_at <= cutoff,
                )
                .order_by(EnrichmentStatus.enriched_at.asc())
                .limit(batch)
                .all()
            )
            for item in expired:
                # Pass 86: PRESERVE the old ``enriched_at`` timestamp on a
                # TTL re-queue. The producer's priority sort (enrichment.py
                # post-pre-filter) treats ``enriched_at IS NULL`` as
                # "never-enriched, top priority" and everything else as
                # "TTL-refresh, oldest-first" — without this preservation,
                # nulling ``enriched_at`` here made TTL re-queues
                # indistinguishable from genuinely-new imports and they
                # crowded fresh items out of the daily batch budget.
                item.enriched = False
                item.error    = None
            db.commit()

        task_monitor.done(task, f"Queued {len(expired)} items for re-enrichment")
        logger.info("[scheduler] TTL refresh: %d items queued (cutoff=%s)", len(expired), cutoff.date())
    except Exception as e:
        task_monitor.error(task, str(e))
        logger.error("[scheduler] Enrichment TTL refresh failed: %s", e)


async def trigger_post_enrichment(user_id: int):
    """Called after enrichment completes — recompute taste + cache recs."""
    logger.info("[scheduler] Post-enrichment pipeline for user %d", user_id)
    await _recompute_and_cache_recs(user_id)


async def _recompute_and_cache_recs(user_id: int = None):
    """Recompute taste vectors then pre-cache recommendations."""
    if _gaming():
        logger.info("[scheduler] Game running — skipping recompute + cache (VRAM)")
        return
    try:
        from src.database.connection import get_db_session
        from src.database.models import User

        if not user_id:
            with get_db_session() as db:
                admin = db.query(User).filter_by(is_admin=True).first()
                if not admin:
                    return
                user_id = admin.id

        # Step 1: Recompute taste vectors
        logger.info("[scheduler] Recomputing taste vectors for user %d", user_id)
        from src.services.taste_engine import compute_all_taste_vectors
        await compute_all_taste_vectors(user_id)

        # Step 2: Pre-cache recommendations per category
        logger.info("[scheduler] Pre-caching recommendations for user %d", user_id)
        await _cache_recommendations(user_id)

        # Step 3: Trigger verification questions
        try:
            from src.services.verification_session import start_verification_session
            await start_verification_session(user_id)
        except Exception as e:
            logger.debug("[scheduler] Verification session skipped: %s", e)

    except Exception as e:
        logger.error("[scheduler] Recompute+cache failed: %s", e)


async def _warm_top_track_metadata(user_id: int, limit: int = 100, days: int = 30) -> int:
    """Pass 69: pre-warm Last.fm track metadata for the most-played recent tracks.

    The discuss-context path (chat.py, ``track_obsession`` branch) fetches
    track metadata on demand via ``enrich_track``. This pre-warms that cache
    for the top ``limit`` tracks of the last ``days`` days so opening a track
    discussion is an instant cache hit instead of a live Last.fm call.

    Runs as a phase of the DAILY music pipeline — and the daily cadence is
    deliberate. ``enrich_track`` caches with a 30-day TTL; ``MetadataCache``
    has no expiry-purge (``set_cache`` overwrites in place, cf. pass 48), so
    nothing is ever "thrown out" — an expired entry is just re-fetched. A
    daily re-warm means a top track's metadata is refreshed within ~a day of
    expiring, never gapped. Cheap on repeat runs: ``enrich_track`` returns
    instantly on a cache hit, only expired/new tracks hit the API.

    Returns the number of tracks warmed (0 on any failure — best-effort, must
    not break the music pipeline's success reporting).
    """
    try:
        from sqlalchemy import func as _func
        from src.database.connection import get_db_session
        from src.database.models import WatchHistoryEntry
        from src.services.music_metadata import enrich_track

        cutoff = datetime.utcnow() - timedelta(days=days)
        with get_db_session() as db:
            rows = (
                db.query(
                    WatchHistoryEntry.title,
                    WatchHistoryEntry.series_title,
                    _func.count(WatchHistoryEntry.id).label("plays"),
                )
                .filter(
                    WatchHistoryEntry.user_id == user_id,
                    WatchHistoryEntry.media_type == "music",
                    WatchHistoryEntry.title.isnot(None),
                    WatchHistoryEntry.viewed_at >= cutoff,
                )
                .group_by(WatchHistoryEntry.title, WatchHistoryEntry.series_title)
                .order_by(_func.count(WatchHistoryEntry.id).desc())
                .limit(limit)
                .all()
            )
            # Extract to plain tuples INSIDE the session block — rows detach
            # once it closes (cf. pass 67 DetachedInstanceError).
            tracks = [(r.title, r.series_title or "") for r in rows]

        if not tracks:
            logger.info("[scheduler] track-metadata warm: no recent music plays")
            return 0

        warmed = 0
        for track, artist in tracks:
            try:
                await enrich_track(track, artist)
                warmed += 1
            except Exception as e:
                logger.debug("[scheduler] warm track %r failed: %s", track, e)
        logger.info("[scheduler] track-metadata warm: %d/%d tracks cached (last %dd, top %d)",
                    warmed, len(tracks), days, limit)
        return warmed
    except Exception as e:
        logger.error("[scheduler] track-metadata warm failed: %s", e)
        return 0


async def job_music_pipeline():
    """
    Daily at 04:00: match Spotify plays to Plex tracks, then fetch Last.fm genres
    for any plays still missing them (both Spotify and Plex).
    """
    logger.info("[scheduler] Starting daily music pipeline")
    from src.services.task_monitor import task_monitor
    task = task_monitor.create(name="Music Match + Enrichment", category="music")
    task_monitor.start(task)
    try:
        from src.database.connection import get_db_session
        from src.database.models import User
        from src.services.music_matcher import run_music_pipeline

        with get_db_session() as db:
            admin = db.query(User).filter_by(is_admin=True).first()
            if not admin:
                task_monitor.skip(task, "No admin user")
                return
            user_id = admin.id

        task_monitor.update(task, message="Running Plex match + Last.fm enrichment")
        result = await run_music_pipeline(user_id)

        # Pass 69: phase 3 — pre-warm Last.fm metadata for the most-played
        # recent tracks so the discuss-context path serves them from cache
        # instead of a live call. Best-effort: returns 0 on failure, never
        # breaks the pipeline's success reporting.
        task_monitor.update(task, message="Warming top-track metadata cache")
        warmed = await _warm_top_track_metadata(user_id)

        p1 = result.get("phase1_plex_match", {})
        p2 = result.get("phase2_lastfm_genres", {})
        summary = (
            f"Plex matched={p1.get('matched', 0)} | "
            f"Last.fm enriched={p2.get('enriched_plays', 0)} plays "
            f"({p2.get('tracks_queried', 0)} tracks) | "
            f"track-meta warmed={warmed}"
        )
        task_monitor.done(task, summary)
        logger.info("[scheduler] Music pipeline complete: %s", summary)
    except Exception as e:
        task_monitor.error(task, str(e))
        logger.error("[scheduler] Music pipeline failed: %s", e)


async def job_db_vacuum():
    """
    Pass 15b: weekly off-hours SQLite VACUUM.

    SQLite reclaims free pages only when VACUUM runs explicitly. Without
    this, the file grows even when row counts stay flat (taste-vector
    rebuilds, metadata cache churn, watch-history rotation all leave
    tombstoned pages behind). NEVER deletes data — only reclaims free
    space.

    Targets: data/curatarr.db (main app DB) and data/cache/metadata.db
    (api_cache). Each file is vacuumed independently with
    its own connection so a failure on one doesn't block the other.

    Logs old vs new file size per DB so the impact is visible.
    """
    import os
    import sqlite3
    from src.services.task_monitor import task_monitor
    from src.config import settings as _s

    logger.info("[scheduler] Starting weekly DB vacuum")
    task = task_monitor.create(name="Weekly DB vacuum", category="maintenance")
    task_monitor.start(task)

    targets = []
    # Main app DB
    main_path = getattr(_s, "DATABASE_URL", "").replace("sqlite:///", "")
    if main_path and os.path.exists(main_path):
        targets.append(("curatarr.db", main_path))
    # Metadata cache DB
    cache_path = str(getattr(_s, "ENRICHMENT_CACHE", "")) or ""
    if cache_path and os.path.exists(cache_path):
        targets.append(("metadata.db", cache_path))

    if not targets:
        task_monitor.skip(task, "No SQLite files found to vacuum")
        return

    summary_lines = []
    for label, path in targets:
        try:
            size_before = os.path.getsize(path)
            # Open a dedicated connection (must NOT be in a transaction
            # for VACUUM to work). isolation_level=None → autocommit mode.
            conn = sqlite3.connect(path, isolation_level=None)
            try:
                # incremental_vacuum reclaims pages from the freelist when
                # auto_vacuum is enabled. Harmless when not.
                try:
                    conn.execute("PRAGMA incremental_vacuum;")
                except sqlite3.OperationalError:
                    pass
                conn.execute("VACUUM;")
                # Refresh query optimiser stats after page reorg.
                try:
                    conn.execute("PRAGMA optimize;")
                except sqlite3.OperationalError:
                    pass
            finally:
                conn.close()
            size_after = os.path.getsize(path)
            saved_mb = (size_before - size_after) / (1024 * 1024)
            line = f"{label}: {size_before/1024/1024:.1f}MB → {size_after/1024/1024:.1f}MB (saved {saved_mb:+.1f}MB)"
            summary_lines.append(line)
            logger.info("[scheduler] vacuum %s", line)
        except Exception as e:
            line = f"{label}: vacuum failed — {type(e).__name__}: {e}"
            summary_lines.append(line)
            logger.warning("[scheduler] %s", line)

    task_monitor.done(task, " | ".join(summary_lines))


async def _cache_recommendations(user_id: int):
    """Pre-generate and store recommendations for all categories."""
    try:
        from src.services.recommendations_engine import generate_recommendations
        from src.database.connection import get_db_session
        from src.database.models import CachedRecommendation
        from src.services.app_state import set_state

        categories = ["movie", "show", "anime", "music"]
        total = 0

        for cat in categories:
            try:
                recs = await generate_recommendations(
                    user_id=user_id,
                    category=cat,
                    limit=10,
                )
                if not recs:
                    continue

                # Store in cache table
                with get_db_session() as db:
                    db.query(CachedRecommendation).filter(
                        CachedRecommendation.user_id == user_id,
                        CachedRecommendation.category == cat,
                    ).delete()

                    for rec in recs:
                        try:
                            from src.routers.recommendations import _fetch_tmdb
                            rec["poster_url"], rec["synopsis"] = await _fetch_tmdb(rec.get("title",""), cat)
                        except Exception:
                            rec["poster_url"] = None
                            rec["synopsis"] = None

                        db.add(CachedRecommendation(
                            user_id=user_id,
                            category=cat,
                            title=rec.get("title", ""),
                            reason=rec.get("reason") or rec.get("pitch", ""),
                            confidence=rec.get("confidence", 0.7),
                            genres=rec.get("genres", ""),
                            poster_url=rec.get("poster_url"),
                            synopsis=rec.get("synopsis"),
                            cached_at=datetime.utcnow(),
                        ))
                    db.commit()
                    total += len(recs)
                    logger.info("[scheduler] Cached %d recs for %s", len(recs), cat)

            except Exception as e:
                logger.warning("[scheduler] Rec cache failed for %s: %s", cat, e)

        set_state("recs_cached_at", datetime.utcnow().isoformat())
        logger.info("[scheduler] Recommendations cached: %d total", total)

    except Exception as e:
        logger.error("[scheduler] Recommendation caching failed: %s", e)


# ── Pass 16o: keep Library Manager arr cache warm ────────────────────────────
async def job_arr_cache_refresh():
    """
    Periodic background refresh for the Library Manager arr cache.

    Pre-warm at startup loads the L2 (DB) cache into L1 and triggers one
    initial refresh — but if Lidarr was down at boot, the L1 entry stays
    stale until a user opens the Lidarr tab. This job keeps trying every
    30 minutes so the cache catches a recovery window even when nobody's
    clicking.

    Per-arr error isolation — a hung Lidarr doesn't block Sonarr/Radarr
    refreshes. No task_monitor entry: this is a quiet background cycle,
    not user-facing work.
    """
    from src.config import settings as _s
    from src.routers.library import _fetch_arr_library

    pairs = (
        ("sonarr", _s.SONARR_URL, _s.SONARR_API_KEY),
        ("radarr", _s.RADARR_URL, _s.RADARR_API_KEY),
        ("lidarr", _s.LIDARR_URL, _s.LIDARR_API_KEY),
    )
    succeeded = 0
    failed    = 0
    for svc, url, key in pairs:
        if not url or not key:
            continue
        try:
            await _fetch_arr_library(svc, force_refresh=True)
            succeeded += 1
        except Exception as e:
            # _fetch_arr_library raises HTTPException only when there's
            # no cache to fall back on; with stale cache it returns
            # gracefully. Either way we just log and move on.
            failed += 1
            logger.info("[scheduler] arr cache refresh: %s skipped (%s)", svc, e)
    if succeeded or failed:
        logger.info(
            "[scheduler] arr cache refresh done — %d refreshed, %d skipped",
            succeeded, failed,
        )
