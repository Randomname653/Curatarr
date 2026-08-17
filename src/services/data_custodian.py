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
    # Audit #7: same lock as the manual /music/start endpoint — the two
    # paths used to run the pipeline CONCURRENTLY into MusicBrainz/Last.fm.
    from src.services.app_state import acquire_state_lock, release_state_lock
    if not acquire_state_lock("music_pipeline_running"):
        logger.info("[custodian] music pipeline already running — retry next tick")
        return False
    batch = 2000 if deep else 1000
    try:
        r = await run_music_pipeline(uid, batch=batch)
    finally:
        release_state_lock("music_pipeline_running")
    # Top-track metadata warm phase (Pass 69) — its only caller used to be
    # the retired job_music_pipeline, so the track-obsession discuss cache
    # went cold when the custodian absorbed the pipeline (audit #10).
    try:
        from src.services.scheduler import _warm_top_track_metadata
        warmed = await _warm_top_track_metadata(uid)
        if warmed:
            logger.info("[custodian] top-track metadata warmed: %d", warmed)
    except Exception as e:
        logger.debug("[custodian] track warm failed: %s", e)
    # Heuristic done-check: every phase consumed less than its batch → the
    # queues are (near) drained; otherwise stay due and continue next tick.
    # Audit #1: the keys MUST match what the phases actually return
    # (enriched_plays / tracks_queried) — the old enriched/queried read 0
    # forever and stamped the pipeline done with thousands of tracks open.
    mbid = (r.get("phase1_4_mbid") or {})
    sp = (r.get("phase1_5_spotify") or {})
    lf = (r.get("phase2_lastfm_genres") or {})
    busy = max(
        mbid.get("resolved", 0) or 0,
        sp.get("enriched_plays", 0) or 0,
        lf.get("tracks_queried", 0) or 0,
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


async def _run_reception(deep: bool = False) -> bool:
    from src.services.reception import run_reception_backfill
    r = await run_reception_backfill(limit=150 if deep else 40)
    logger.info("[custodian] reception: +%d (checked %d, remaining %d)",
                r.get("added", 0), r.get("checked", 0), r.get("remaining", 0))
    return r.get("remaining", 0) == 0


async def _run_discogs_styles(deep: bool = False) -> bool:
    """Monthly CC0 masters-dump refresh (~590 MB stream, CPU-only). The
    module itself skips when the local DB is <30 days old, so the 24h task
    cadence just means 'check often, download rarely'."""
    from src.services.discogs_offline import refresh_discogs_styles
    r = await refresh_discogs_styles()
    if r.get("skipped"):
        return True
    logger.info("[custodian] discogs styles: %s", r)
    # 429 from the dump endpoint → stay due, retry next tick
    return "error" not in r


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


# How stale a user's rec cache may get before the weekly task regenerates it.
# 144h (6d) < the 168h task cadence, so the true weekly run refreshes every
# user, while an EARLY run (all-sampled flag cleared the job stamp) only does
# LLM work for the flagged user — everyone else short-circuits as fresh.
_RECS_FRESH_HOURS = 144


async def _run_recs(deep: bool = False) -> bool:
    """Weekly recommendation-cache refresh for ALL active users. Closes the
    old gap where recs regenerated only at startup-if-empty / manually /
    post-arr-sync — and only ever for the admin. Returns False when any user
    failed so the task stays due and resumes next tick (the freshness guard
    makes the retry skip users already done)."""
    from datetime import datetime, timedelta
    from src.database.connection import get_db_session
    from src.database.models import CachedRecommendation, User
    from src.services.app_state import get_state, set_state
    from src.services import scheduler as sched

    with get_db_session() as db:
        users = [(u.id, u.plex_username) for u in
                 db.query(User).filter(User.is_active == True).all()]
    all_ok = True
    for uid, uname in users:
        sampled_flag = get_state(f"recs_all_sampled:user_id={uid}") or ""
        with get_db_session() as db:
            newest = (db.query(CachedRecommendation.cached_at)
                      .filter(CachedRecommendation.user_id == uid)
                      .order_by(CachedRecommendation.cached_at.desc())
                      .first())
        fresh = (newest and newest[0]
                 and datetime.utcnow() - newest[0] < timedelta(hours=_RECS_FRESH_HOURS))
        if fresh and not sampled_flag:
            continue
        try:
            logger.info("[custodian] refreshing recommendations for %s%s",
                        uname, " (all sampled)" if sampled_flag else "")
            await sched._cache_recommendations(uid)
            set_state(f"recs_all_sampled:user_id={uid}", "")
        except Exception as e:
            logger.warning("[custodian] rec refresh failed for %s: %s", uname, e)
            all_ok = False
    return all_ok


async def _run_playlist_push(deep: bool = False) -> bool:
    """Push/refresh every active user's Curatarr-Recommended Plex playlists.
    Users without a stored token are logged-and-skipped and do NOT hold the
    task open (it would stay due forever); a transient Plex error does."""
    from src.database.connection import get_db_session
    from src.database.models import User
    from src.services.plex_playlists import push_user_playlists

    with get_db_session() as db:
        users = db.query(User).filter(User.is_active == True).all()
        # detach the attributes we need before the session closes
        users = [type("U", (), {"id": u.id, "plex_username": u.plex_username,
                                "plex_token": u.plex_token})() for u in users]
    all_ok = True
    for u in users:
        try:
            await push_user_playlists(u)
        except Exception as e:
            logger.warning("[custodian] playlist push failed for %s: %s",
                           u.plex_username, e)
            all_ok = False
    return all_ok


async def _run_music_playlist_push(deep: bool = False) -> bool:
    """Push/refresh every active user's Curatarr-Recommended MUSIC playlist
    (one unheard album per recommended artist). Token-less users are
    logged-and-skipped, same contract as the video push."""
    from src.database.connection import get_db_session
    from src.database.models import User
    from src.services.plex_playlists import push_user_music_playlist

    with get_db_session() as db:
        users = db.query(User).filter(User.is_active == True).all()  # noqa: E712
        users = [type("U", (), {"id": u.id, "plex_username": u.plex_username,
                                "plex_token": u.plex_token})() for u in users]
    all_ok = True
    for u in users:
        try:
            await push_user_music_playlist(u)
        except Exception as e:
            logger.warning("[custodian] music playlist push failed for %s: %s",
                           u.plex_username, e)
            all_ok = False
    return all_ok


async def _run_music_catalog_sync(deep: bool = False) -> bool:
    """SoulSync→Lidarr catalog sync (Modell B): SoulSync owns the files,
    Lidarr is the passive structure index — this keeps it complete
    (disarmed adds) and surfaces folder-name drift."""
    from src.services.music_catalog_sync import sync_soulsync_to_lidarr
    res = await sync_soulsync_to_lidarr()
    return bool(res.get("ok"))


async def _run_facet_backfill(task=None) -> bool:
    """Multi-vector items stage 1: page the corpus into theme-facet points.
    False while unfinished → the task stays due and every tick advances the
    app_state cursor; True once the done flag is stamped. ``task`` is the
    tick's Activity card — the backfill reports page progress into it."""
    from src.services.facet_index import run_facet_backfill
    return await run_facet_backfill(task=task)


async def _run_collections(deep: bool = False) -> bool:
    """Design + push the household's "Curatarr · " collection shelves."""
    from src.services.collection_designer import design_collections
    from src.services.plex_collections import push_collections
    designs = await design_collections()
    if not designs:
        logger.info("[custodian] no collection designs produced — task stays due")
        return False
    res = await push_collections(designs)
    logger.info("[custodian] collections pushed: %s", res)
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
    takes_task: bool = False     # runner accepts task= (the Activity card) for progress
    # True = the runner (or the code it calls) creates its own task_monitor
    # card(s), so the tick must NOT add a wrapper card on top (double-carding
    # the same work looks like a bug). False = the tick wraps the run in a
    # generic Activity card — the DEFAULT, so newly added maintenance work is
    # visible in the Activity view without remembering to instrument it.
    reports_own: bool = False


def _registry() -> list[Task]:
    # Priority order — first overdue runs first.
    # reports_own=True marks runners whose inner code already creates its own
    # Activity cards (scheduler jobs, the enrichment pipeline, taste compute,
    # the rec-cache refresh, memory extraction) — everything else gets the
    # tick's generic wrapper card so NO maintenance work runs invisibly.
    from src.services import scheduler as sched
    return [
        Task("db_backup",        "DB backup",            24.0,  sched.job_db_backup,
             reports_own=True),
        Task("plex_sync",        "Plex sync",            24.0,  sched.job_plex_sync,
             reports_own=True),
        Task("arr_sync",         "ARR sync + deletion candidates", 24.0,
             sched.job_arr_sync, needs_llm=True, reports_own=True),
        Task("arr_pre_enrich",   "ARR metadata prefetch", 24.0, sched.job_arr_pre_enrich,
             reports_own=True),
        # 0.4h cadence — a wrapper card every 24 min would drown the Activity
        # view, so extract_memories_from_thread cards itself only when a
        # thread actually gets extracted.
        Task("memory_catchup",   "Memory-extraction catch-up", 0.4,
             _run_memory_catchup, needs_llm=True, takes_deep=True,
             reports_own=True),
        Task("custodian_enrich", "Enrichment cycle",     24.0,  _run_enrichment_cycle,
             needs_llm=True, takes_deep=True, reports_own=True),
        Task("custodian_omdb",   "OMDb top-up",          24.0,  _run_omdb,
             takes_deep=True),
        Task("custodian_signif", "Wikipedia significance", 24.0, _run_significance,
             needs_llm=True, takes_deep=True),
        Task("custodian_recept", "Community reception",  24.0,  _run_reception,
             needs_llm=True, takes_deep=True),
        Task("music_pipeline",   "Spotify pipeline",     24.0,  _run_spotify,
             takes_deep=True),
        Task("discogs_styles",   "Discogs styles dump",  24.0,  _run_discogs_styles,
             takes_deep=True),
        Task("custodian_taste",  "Taste vectors",        24.0,  _run_taste_if_due,
             needs_llm=True, takes_deep=True, reports_own=True),
        # Curatarr-Recommended pipeline: taste feeds recs, recs feed the Plex
        # playlists — this order means one weekly tick does taste → recs →
        # playlists. Early refresh: the all-sampled hook in plex_sync clears
        # both job stamps, so the next 30-min tick reruns just these two
        # (the 144h per-user freshness guard shields everyone already done).
        Task("custodian_recs",   "Recommendation cache refresh", 168.0,
             _run_recs, needs_llm=True, reports_own=True),
        Task("plex_rec_playlist", "Curatarr Recommended playlists", 168.0,
             _run_playlist_push),
        # Music variant: one unheard album per recommended artist, per user.
        # Plex reads/writes only — no LLM gate.
        Task("plex_music_playlist", "Curatarr music playlists", 168.0,
             _run_music_playlist_push),
        # Catalog mode: nightly SoulSync→Lidarr index completion (disarmed
        # adds + folder-drift report). No-op without SoulSync configured.
        Task("music_catalog_sync", "SoulSync→Lidarr catalog sync", 24.0,
             _run_music_catalog_sync),
        # Household collections: the 27B designs rotating themed shelves from
        # the OWNED library (section-global — one set via the owner token).
        Task("plex_collections", "Curatarr collections", 168.0,
             _run_collections, needs_llm=True),
        # Multi-vector items stage 1: backfill theme-facet points for the
        # existing corpus (CPU embeds only, resumable via app_state cursor;
        # returns False until complete so every tick continues the run,
        # then becomes a stamped no-op). takes_task: page progress lands in
        # the wrapper card (N/total + ETA) instead of console-only logs.
        Task("facet_backfill",   "Facet index backfill", 24.0,
             _run_facet_backfill, takes_task=True),
        Task("custodian_audit",  "Profile audit",        168.0, _run_audit,
             takes_deep=True),
        Task("memory_decay",     "Memory decay",         168.0, sched.job_memory_decay,
             reports_own=True),
        Task("orphan_check",     "Orphaned sections",    168.0, sched.job_orphan_check,
             reports_own=True),
        Task("db_vacuum",        "DB vacuum",            168.0, sched.job_db_vacuum,
             settle_only=True, reports_own=True),
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
    from src.services.task_monitor import task_monitor
    actions = []
    async with _tick_lock:
        started = time.time()
        for t in _registry():
            mon = None
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
                # Activity card for every runner that doesn't card itself.
                # Stable task_id: each run REPLACES the previous card instead
                # of stacking (a still-backfilling task runs every tick — 48
                # same-named cards a day would bury the rest of the view).
                if not t.reports_own:
                    mon = task_monitor.create(name=t.label, category="custodian",
                                              task_id=f"cust-{t.job_id}")
                    task_monitor.start(mon)
                kwargs = {}
                if t.takes_deep:
                    kwargs["deep"] = deep
                if t.takes_task:
                    kwargs["task"] = mon
                done = await t.runner(**kwargs)
                done = True if done is None else bool(done)
                if done:
                    _record_job_run(t.job_id)
                if mon is not None:
                    task_monitor.done(mon, "Complete" if done
                                      else "Partial — continues next tick")
                actions.append({
                    "task": t.job_id,
                    "result": "done" if done else "partial (continues next tick)",
                    "seconds": round(time.time() - t0, 1),
                })
            except Exception as e:
                logger.warning("[custodian] task %s failed: %s", t.job_id, e)
                if mon is not None:
                    task_monitor.error(mon, str(e))
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
