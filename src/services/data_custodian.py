"""
Curatarr — Data Custodian: debt-based maintenance instead of a button zoo.

The app runs ON DEMAND on the owner's PC — nightly crons (02:00-04:30)
practically never fired, which is how 10k enriched profiles rotted unnoticed
and OMDb/significance backfills only ever ran when a button was clicked.

The custodian is the anacron answer: every maintenance task carries a CADENCE
and a last-run timestamp (the scheduler's existing _tracked/_job_overdue
plumbing). A TICK runs 5 minutes after app start (settle window) and then
every 30 minutes: whatever is overdue runs, in priority order, one task at a
time, as a background trickle — yielding to the curator between items and
stopping when a game grabs the GPU. Turn the PC on and it catches up; no
clicking required.

Partial tasks (enrichment backlog, OMDb, significance, Spotify phases) report
done=False when work remains — the custodian then leaves them due, so the
next tick continues where they stopped. The whole state is observable in the
Knowledge-Base view (custodian status line + report).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)

# One tick at a time; "run now" and the interval tick share this.
_tick_lock = asyncio.Lock()
_first_tick_done = False

_SETTLE_SECONDS = 300          # first tick waits this long after app start
_REPORT_KEY = "custodian_report"


def _gaming() -> bool:
    try:
        from src.services.app_state import get_state
        return get_state("game_active") == "1"
    except Exception:
        return False


def _admin_id() -> int | None:
    from src.database.connection import get_db_session
    from src.database.models import User
    with get_db_session() as db:
        admin = db.query(User).filter_by(is_admin=True).first()
        return admin.id if admin else None


# ── runners ───────────────────────────────────────────────────────────────────
# Each returns True when fully done (stamp the cadence) or False when work
# remains / the task was blocked (stay due — continue next tick).

async def _run_enrichment_cycle(deep: bool = False) -> bool:
    """New items + dead-cache revive + metadata-changed + retries, via the
    normal pipeline (source=both). Budgeted; the pre-filter does the smart
    skipping. Respects the same lock as the manual /start endpoint."""
    from src.services.app_state import acquire_state_lock
    from src.routers.enrichment import _run_enrichment, CATEGORIES
    uid = _admin_id()
    if uid is None:
        return True
    if not acquire_state_lock("enrichment_running"):
        logger.info("[custodian] enrichment already running — retry next tick")
        return False
    limit = 2000 if deep else 400
    await _run_enrichment(uid, list(CATEGORIES), "both", limit)
    return True


async def _run_omdb(deep: bool = False) -> bool:
    from src.services.media_enricher import run_omdb_backfill
    limit = 5000 if deep else 2000
    r = await run_omdb_backfill(limit=limit)
    cand, done = r.get("candidates", 0), r.get("enriched", 0)
    logger.info("[custodian] omdb top-up: %d/%d", done, cand)
    return cand <= limit


async def _run_significance(deep: bool = False) -> bool:
    from src.services.media_enricher import run_significance_backfill
    r = await run_significance_backfill(limit=400 if deep else 150)
    logger.info("[custodian] significance: +%d (checked %d, remaining %d)",
                r.get("added", 0), r.get("checked", 0), r.get("remaining", 0))
    return r.get("remaining", 0) == 0


async def _run_spotify(deep: bool = False) -> bool:
    from src.services.music_matcher import run_music_pipeline
    uid = _admin_id()
    if uid is None:
        return True
    batch = 2000 if deep else 1000
    r = await run_music_pipeline(uid, batch=batch)
    # Heuristic done-check: every phase consumed less than its batch → the
    # queues are (near) drained; otherwise stay due and continue next tick.
    mbid = (r.get("phase1_4_mbid") or {})
    sp = (r.get("phase1_5_spotify") or {})
    lf = (r.get("phase2_lastfm_genres") or {})
    busy = max(
        mbid.get("resolved", 0) or 0,
        sp.get("enriched", 0) or 0,
        (lf.get("queried", 0) or 0),
    )
    return busy < batch


async def _run_taste_if_due(deep: bool = False) -> bool:
    """Trigger-based: recompute taste vectors once enough NEW profiles landed
    since the last compute (the manual button's job, automated)."""
    from src.database.connection import get_db_session
    from src.database.models import EnrichmentStatus
    from src.services.scheduler import _last_job_run
    last = _last_job_run("custodian_taste") or datetime(1970, 1, 1)
    with get_db_session() as db:
        fresh = (db.query(EnrichmentStatus)
                 .filter(EnrichmentStatus.enriched == True,
                         EnrichmentStatus.enriched_at > last).count())
    if fresh < 100 and not deep:
        return True   # nothing meaningful to recompute — counts as done
    uid = _admin_id()
    if uid is None:
        return True
    from src.routers.enrichment import _run_taste_computation
    logger.info("[custodian] %d new profiles since last taste compute — recomputing",
                fresh)
    await _run_taste_computation(uid, None)
    return True


async def _run_memory_catchup(deep: bool = False) -> bool:
    from src.services.episodic_memory import extract_catchup
    r = await extract_catchup()
    n = r.get("threads_caught_up", 0)
    if n:
        logger.info("[custodian] memory catch-up: %d thread(s) finished", n)
    return True


async def _run_audit(deep: bool = False) -> bool:
    from src.routers.enrichment import _audit_enrichments
    r = await _audit_enrichments(dry_run=False)
    logger.info("[custodian] audit-requeue: %s", r)
    return True


@dataclass
class Task:
    job_id: str
    label: str
    cadence_h: float
    runner: object
    needs_llm: bool = False      # skipped while a game holds the GPU
    settle_only: bool = False    # only in the first tick after app start
    takes_deep: bool = False     # runner accepts the deep= budget flag


def _registry() -> list[Task]:
    # Priority order — first overdue runs first.
    from src.services import scheduler as sched
    return [
        Task("db_backup",        "DB backup",            24.0,  sched.job_db_backup),
        Task("plex_sync",        "Plex sync",            24.0,  sched.job_plex_sync),
        Task("arr_sync",         "ARR sync + deletion candidates", 24.0,
             sched.job_arr_sync, needs_llm=True),
        Task("arr_pre_enrich",   "ARR metadata prefetch", 24.0, sched.job_arr_pre_enrich),
        Task("memory_catchup",   "Memory-extraction catch-up", 0.4,
             _run_memory_catchup, needs_llm=True, takes_deep=True),
        Task("custodian_enrich", "Enrichment cycle",     24.0,  _run_enrichment_cycle,
             needs_llm=True, takes_deep=True),
        Task("custodian_omdb",   "OMDb top-up",          24.0,  _run_omdb,
             takes_deep=True),
        Task("custodian_signif", "Wikipedia significance", 24.0, _run_significance,
             needs_llm=True, takes_deep=True),
        Task("music_pipeline",   "Spotify pipeline",     24.0,  _run_spotify,
             takes_deep=True),
        Task("custodian_taste",  "Taste vectors",        24.0,  _run_taste_if_due,
             needs_llm=True, takes_deep=True),
        Task("custodian_audit",  "Profile audit",        168.0, _run_audit,
             takes_deep=True),
        Task("memory_decay",     "Memory decay",         168.0, sched.job_memory_decay),
        Task("orphan_check",     "Orphaned sections",    168.0, sched.job_orphan_check),
        Task("db_vacuum",        "DB vacuum",            168.0, sched.job_db_vacuum,
             settle_only=True),
    ]


def custodian_status() -> dict:
    """Pure read: the report of the last tick + per-task due state."""
    from src.services.app_state import get_state
    from src.services.scheduler import _last_job_run, _job_overdue
    report = None
    try:
        raw = get_state(_REPORT_KEY)
        report = json.loads(raw) if raw else None
    except Exception:
        pass
    tasks = []
    for t in _registry():
        last = _last_job_run(t.job_id)
        tasks.append({
            "job_id": t.job_id,
            "label": t.label,
            "cadence_h": t.cadence_h,
            "last_run": last.isoformat() if last else None,
            "due": _job_overdue(t.job_id, t.cadence_h),
        })
    return {"report": report, "tasks": tasks, "ticking": _tick_lock.locked()}


async def custodian_tick(first_tick: bool = False, force: bool = False,
                         deep: bool = False) -> dict:
    """Run every overdue task, in priority order, one at a time.

    ``first_tick`` sleeps the settle window first (called once at startup);
    ``force`` ignores cadence (the "Run maintenance now" button);
    ``deep`` raises the partial-task budgets for a catch-up sprint.
    """
    global _first_tick_done
    if first_tick:
        await asyncio.sleep(_SETTLE_SECONDS)
    if _tick_lock.locked():
        logger.info("[custodian] tick skipped — previous tick still running")
        return {"skipped": "busy"}

    from src.services.app_state import set_state
    from src.services.scheduler import _job_overdue, _record_job_run
    actions = []
    async with _tick_lock:
        started = time.time()
        for t in _registry():
            try:
                if t.settle_only and _first_tick_done and not force:
                    continue
                if not force and not _job_overdue(t.job_id, t.cadence_h):
                    continue
                if t.needs_llm and _gaming():
                    actions.append({"task": t.job_id, "result": "skipped (game)"})
                    continue
                logger.info("[custodian] running %s …", t.job_id)
                t0 = time.time()
                done = await (t.runner(deep=deep) if t.takes_deep else t.runner())
                done = True if done is None else bool(done)
                if done:
                    _record_job_run(t.job_id)
                actions.append({
                    "task": t.job_id,
                    "result": "done" if done else "partial (continues next tick)",
                    "seconds": round(time.time() - t0, 1),
                })
            except Exception as e:
                logger.warning("[custodian] task %s failed: %s", t.job_id, e)
                actions.append({"task": t.job_id, "result": f"error: {e}"})
        _first_tick_done = True
        report = {
            "ts": datetime.utcnow().isoformat(),
            "duration_s": round(time.time() - started, 1),
            "forced": force, "deep": deep,
            "actions": actions,
        }
        try:
            set_state(_REPORT_KEY, json.dumps(report))
        except Exception:
            pass
        if actions:
            logger.info("[custodian] tick done in %.0fs: %s", report["duration_s"],
                        ", ".join(f"{a['task']}={a['result']}" for a in actions))
        return report
