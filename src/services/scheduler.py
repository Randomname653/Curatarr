"""
Curatarr 1.0 - Background Scheduler

All scheduled jobs in one place. Started at app startup.

Schedule:
  Every 24h  — Plex sync (if new items found: recompute taste + cache recs)
  Every 24h  — ARR incremental sync
  Every 6h   — Check for proactive messages (binge/marathon/completion detection)
  Every 7d   — Memory decay (reduce importance of old memories)
  Every 7d   — Orphaned section check (notify admin if Plex sections are unmapped)
  On demand  — Triggered after enrichment completes: recompute taste + cache recs
"""

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()
_started = False


def start_scheduler():
    global _started
    if _started:
        return
    _started = True

    scheduler.add_job(
        job_plex_sync,
        IntervalTrigger(hours=24),
        id="plex_sync",
        name="Daily Plex sync",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        job_arr_sync,
        IntervalTrigger(hours=24),
        id="arr_sync",
        name="Daily ARR sync",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        job_proactive_messages,
        IntervalTrigger(minutes=30),
        id="proactive_messages",
        name="Proactive message cache fill",
        replace_existing=True,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        job_memory_decay,
        CronTrigger(day_of_week="sun", hour=3),
        id="memory_decay",
        name="Weekly memory decay",
        replace_existing=True,
        misfire_grace_time=7200,
    )
    scheduler.add_job(
        job_orphan_check,
        CronTrigger(day_of_week="sun", hour=4),
        id="orphan_check",
        name="Weekly orphaned section check",
        replace_existing=True,
        misfire_grace_time=7200,
    )

    scheduler.start()
    logger.info("Scheduler started: plex_sync(24h), arr_sync(24h), "
                "proactive(6h), memory_decay(weekly), orphan_check(weekly)")

    # Run startup check — cache recommendations if missing, sync if overdue
    import asyncio as _asyncio
    _asyncio.ensure_future(_startup_check())


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)


# ── JOBS ──────────────────────────────────────────────────────────────────────

async def _startup_check():
    """On startup: cache recommendations if empty, sync if overdue."""
    import asyncio
    await asyncio.sleep(5)  # wait for DB to be ready

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

        # Check if Plex sync is overdue (>25h since last sync)
        last_sync = get_state("last_sync_at")
        if last_sync:
            try:
                last_dt = datetime.fromisoformat(last_sync)
                if datetime.utcnow() - last_dt > timedelta(hours=25):
                    logger.info("[startup] Plex sync overdue — running now")
                    await job_plex_sync()
            except Exception:
                pass
        else:
            logger.info("[startup] No sync history — skipping startup sync")

    except Exception as e:
        logger.debug("[startup] Startup check failed: %s", e)


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

        with get_db_session() as db:
            existing_titles = {
                p.title for p in db.query(DeletionProposal).filter(
                    DeletionProposal.user_id == user_id,
                    DeletionProposal.status == "pending",
                ).all()
            }
            added = 0
            for p in all_proposals:
                if p["title"] not in existing_titles:
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
                    ))
                    added += 1
            db.commit()

        set_state("last_arr_sync_at", datetime.utcnow().isoformat())
        task_monitor.done(task, f"Complete: {added} new proposals")
        logger.info("[scheduler] ARR sync complete: %d new proposals", added)
    except Exception as e:
        task_monitor.error(task, str(e))
        logger.error("[scheduler] ARR sync failed: %s", e)


async def job_proactive_messages():
    """Check for binge/marathon/completion patterns and generate messages."""
    logger.info("[scheduler] Checking proactive messages")
    from src.services.task_monitor import task_monitor
    task = task_monitor.create(name="Proactive Messages", category="proactive")
    task_monitor.start(task)
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


async def trigger_post_enrichment(user_id: int):
    """Called after enrichment completes — recompute taste + cache recs."""
    logger.info("[scheduler] Post-enrichment pipeline for user %d", user_id)
    await _recompute_and_cache_recs(user_id)


async def _recompute_and_cache_recs(user_id: int = None):
    """Recompute taste vectors then pre-cache recommendations."""
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
                            from src.routers.recommendations import _fetch_poster
                            rec["poster_url"] = await _fetch_poster(rec.get("title",""), cat)
                        except Exception:
                            rec["poster_url"] = None

                        db.add(CachedRecommendation(
                            user_id=user_id,
                            category=cat,
                            title=rec.get("title", ""),
                            reason=rec.get("reason") or rec.get("pitch", ""),
                            confidence=rec.get("confidence", 0.7),
                            genres=rec.get("genres", ""),
                            poster_url=rec.get("poster_url"),
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
