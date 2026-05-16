"""
Curatarr — Music Pipeline Router

Manually startable, interruptible, persist-safe pipeline for
Spotify-import → Plex-matching → Last.fm genre enrichment.

State flags (AppState):
  music_pipeline_running        "0" | "1"
  music_pipeline_stop_requested "0" | "1"
  music_pipeline_interrupted    "0" | "1"  → auto-resumed on next start
  music_pipeline_progress       JSON {phase, processed, total, tracks_queried, enriched_plays}
  music_pipeline_last_run       ISO timestamp

Endpoints:
  POST /api/music/start    — Start the pipeline (rejected if already running)
  POST /api/music/stop     — Request a soft stop (current track finishes, then pauses)
  GET  /api/music/status   — Runtime state + statistics
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from src.database.models import User
from src.routers.auth import get_current_user
from src.services.app_state import get_state, set_state

logger = logging.getLogger(__name__)
router = APIRouter()


class PipelineRequest(BaseModel):
    # Max unique tracks per enrichment phase per run.
    # Caps Phase 1.5 (Spotify) AND Phase 2 (Last.fm) independently.
    # Old name `lastfm_batch` kept as alias for backwards compatibility — older
    # frontends may still send it; new frontends send `batch`.
    batch: int | None = None
    lastfm_batch: int | None = None   # legacy alias

    @property
    def effective_batch(self) -> int:
        return self.batch or self.lastfm_batch or 300


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/ping")
async def pipeline_ping():
    """
    Auth-free endpoint — returns pipeline running state + progress only.
    No user-specific data. Use this to check status from the browser without a token.
    """
    import json as _json
    running  = get_state("music_pipeline_running") == "1"
    raw_prog = get_state("music_pipeline_progress")
    progress = _json.loads(raw_prog) if raw_prog else {}
    last_run = get_state("music_pipeline_last_run")
    return {"running": running, "progress": progress, "last_run": last_run}


@router.post("/start")
async def start_pipeline(
    req: PipelineRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
):
    """
    Start Plex-Match + Last.fm Genre Enrichment pipeline in the background.
    Idempotent on re-run: already-matched plays are skipped, already-enriched
    tracks are skipped.  Safe to call multiple times.
    """
    if get_state("music_pipeline_running") == "1":
        raise HTTPException(status_code=409, detail="Music pipeline already running")

    set_state("music_pipeline_stop_requested", "0")
    batch = req.effective_batch
    background_tasks.add_task(_run_music_pipeline, user.id, batch)
    return {"status": "started", "batch": batch}


@router.post("/stop")
async def stop_pipeline(_user: User = Depends(get_current_user)):
    """
    Request a soft stop.  The pipeline finishes the current DB batch / Last.fm
    call then saves progress.  Re-running /start afterwards continues safely
    (already-matched plays are never re-processed).
    """
    if get_state("music_pipeline_running") != "1":
        return {"status": "not_running"}
    set_state("music_pipeline_stop_requested", "1")
    return {"status": "stop_requested"}


@router.get("/status")
async def pipeline_status(user: User = Depends(get_current_user)):
    """
    Return current pipeline state + enrichment statistics.
    """
    import json as _json
    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry

    running   = get_state("music_pipeline_running") == "1"
    stopped   = get_state("music_pipeline_stop_requested") == "1"
    last_run  = get_state("music_pipeline_last_run")

    raw_prog  = get_state("music_pipeline_progress")
    progress  = _json.loads(raw_prog) if raw_prog else {}

    with get_db_session() as db:
        base = db.query(WatchHistoryEntry).filter(
            WatchHistoryEntry.user_id    == user.id,
            WatchHistoryEntry.media_type == "music",
        )
        total          = base.count()
        src_spotify    = base.filter(WatchHistoryEntry.source == "spotify").count()
        unmatched      = base.filter(
            WatchHistoryEntry.source == "spotify",
            WatchHistoryEntry.plex_item_id.like("spotify%"),
        ).count()
        missing_genres = base.filter(WatchHistoryEntry.genres.is_(None)).count()

    return {
        "running":        running,
        "stop_requested": stopped,
        "last_run":       last_run,
        "progress":       progress,
        "stats": {
            "total_music":       total,
            "source_spotify":    src_spotify,
            "source_plex":       total - src_spotify,
            "unmatched_spotify": unmatched,
            "missing_genres":    missing_genres,
            "has_genres":        total - missing_genres,
        },
    }


# ── Background task ───────────────────────────────────────────────────────────

async def _run_music_pipeline(user_id: int, batch: int) -> None:
    """
    Full pipeline: Phase 1 (Plex match) + Phase 2 (Last.fm genres).
    Writes progress to AppState so the frontend can poll /status.
    Checks stop_requested flag between every significant batch.
    On shutdown (interrupted flag stays "1") → next start resumes automatically.
    """
    import json as _json
    from datetime import datetime
    from src.services.task_monitor import task_monitor

    set_state("music_pipeline_running",     "1")
    set_state("music_pipeline_interrupted", "1")
    set_state("music_pipeline_stop_requested", "0")
    set_state("music_pipeline_progress", _json.dumps({
        "phase": "starting", "processed": 0, "total": 0,
        "tracks_queried": 0, "enriched_plays": 0,
    }))

    task = task_monitor.create(name="Music Pipeline", category="music")
    task_monitor.start(task)

    try:
        from src.services.music_matcher import (
            match_spotify_to_plex,
            resolve_artist_mbids,
            enrich_music_genres_spotify,
            enrich_music_genres_lastfm,
        )

        # ── Phase 1: Plex match ───────────────────────────────────────────────
        task_monitor.update(task, message="Phase 1: Matching Spotify plays to Plex tracks…")
        set_state("music_pipeline_progress", _json.dumps({
            "phase": "plex_match", "processed": 0, "total": 0,
            "tracks_queried": 0, "enriched_plays": 0,
        }))

        p1 = await match_spotify_to_plex(user_id)
        set_state("music_pipeline_progress", _json.dumps({
            "phase":           "plex_match_done",
            "matched":         p1.get("matched", 0),
            "unmatched":       p1.get("unmatched", 0),
            "tracks_queried":  0,
            "enriched_plays":  0,
        }))
        task_monitor.update(task,
            message=f"Phase 1 done — matched={p1.get('matched',0)} unmatched={p1.get('unmatched',0)}")

        # Check stop after Phase 1
        if get_state("music_pipeline_stop_requested") == "1":
            task_monitor.skip(task, "Stopped after Phase 1 (Plex match)")
            set_state("music_pipeline_interrupted", "0")
            set_state("music_pipeline_last_run", datetime.utcnow().isoformat())
            return

        # ── Phase 1.4: MusicBrainz MBID resolve ──────────────────────────────
        # Pass 16n: this orchestrator was the actual entry point used by the
        # UI/scheduler, but Phase 1.4 was only wired into the unused
        # ``music_matcher.run_music_pipeline`` helper. Add it here so the
        # Spotify-Backlog "Add to Lidarr" button actually has a pre-resolved
        # MBID to ship.
        task_monitor.update(task, message="Phase 1.4: Resolving artist MBIDs via MusicBrainz…")
        p_mbid = await resolve_artist_mbids(user_id, batch=batch)
        task_monitor.update(task,
            message=(
                f"Phase 1.4 done — resolved={p_mbid.get('resolved',0)}, "
                f"failed={p_mbid.get('failed',0)} "
                f"({p_mbid.get('remaining',0)} artists remain for next run)"
            ))

        # Check stop after Phase 1.4
        if get_state("music_pipeline_stop_requested") == "1":
            task_monitor.skip(task, "Stopped after Phase 1.4 (MBID resolve)")
            set_state("music_pipeline_interrupted", "0")
            set_state("music_pipeline_last_run", datetime.utcnow().isoformat())
            return

        # ── Phase 1.5: Spotify genre enrichment ──────────────────────────────
        task_monitor.update(task, message="Phase 1.5: Fetching genres from Spotify…")
        set_state("music_pipeline_progress", _json.dumps({
            "phase":          "spotify_enrich",
            "matched":        p1.get("matched", 0),
            "unmatched":      p1.get("unmatched", 0),
            "tracks_queried": 0,
            "enriched_plays": 0,
        }))

        psp = await enrich_music_genres_spotify(user_id, batch=batch)
        if psp.get("spotify_rate_limited"):
            spot_msg = (
                f"Spotify rate-limited at {psp.get('tracks_queried',0)} tracks — "
                f"handing remainder to Last.fm"
            )
            logger.warning("[music_pipeline] %s", spot_msg)
            task_monitor.update(task, message=spot_msg)
        else:
            task_monitor.update(task,
                message=(
                    f"Spotify done — {psp.get('enriched_plays',0)} plays enriched "
                    f"({psp.get('tracks_resolved',0)}/{psp.get('tracks_queried',0)} tracks resolved)"
                    if not psp.get("note") else f"Spotify skipped: {psp.get('note')}"
                ))

        if get_state("music_pipeline_stop_requested") == "1":
            task_monitor.skip(task, "Stopped after Phase 1.5 (Spotify genres)")
            set_state("music_pipeline_interrupted", "0")
            set_state("music_pipeline_last_run", datetime.utcnow().isoformat())
            return

        # ── Phase 2: Last.fm genres ───────────────────────────────────────────
        task_monitor.update(task, message="Phase 2: Fetching genres from Last.fm…")
        set_state("music_pipeline_progress", _json.dumps({
            "phase":                "lastfm_enrich",
            "matched":              p1.get("matched", 0),
            "unmatched":            p1.get("unmatched", 0),
            "spotify_enriched":     psp.get("enriched_plays", 0),
            "tracks_queried":       0,
            "enriched_plays":       0,
        }))

        p2 = await enrich_music_genres_lastfm(user_id, batch=batch)
        final_progress = {
            "phase":                  "done",
            "matched":                p1.get("matched", 0),
            "unmatched":              p1.get("unmatched", 0),
            # Pass 16n: Phase 1.4 stats so the "done" summary in the UI
            # actually reflects all four phases, not just 1/1.5/2.
            "mbid_resolved":          p_mbid.get("resolved", 0),
            "mbid_remaining":         p_mbid.get("remaining", 0),
            "spotify_enriched":       psp.get("enriched_plays", 0),
            "spotify_rate_limited":   psp.get("spotify_rate_limited", False),
            "tracks_queried":         p2.get("tracks_queried", 0),
            "enriched_plays":         p2.get("enriched_plays", 0),
            "artist_fallback":        p2.get("artist_fallback", 0),
            "no_data":                p2.get("no_data", 0),
        }
        set_state("music_pipeline_progress", _json.dumps(final_progress))

        spotify_note = (
            f" | Spotify: {psp.get('enriched_plays',0)} plays enriched"
            + (" (RATE-LIMITED)" if psp.get("spotify_rate_limited") else "")
            if psp.get("enriched_plays") or psp.get("spotify_rate_limited")
            else ""
        )
        mbid_note = (
            f" | MBIDs: {p_mbid.get('resolved',0)} resolved"
            + (f", {p_mbid.get('remaining',0)} pending" if p_mbid.get("remaining") else "")
        ) if p_mbid.get("resolved") or p_mbid.get("remaining") else ""
        summary = (
            f"Plex matched={p1.get('matched',0)}{mbid_note}{spotify_note} | "
            f"Last.fm: {p2.get('enriched_plays',0)} plays enriched "
            f"({p2.get('tracks_queried',0)} queried, "
            f"{p2.get('artist_fallback',0)} via artist fallback, "
            f"{p2.get('no_data',0)} no-data cached)"
        )
        task_monitor.done(task, summary)
        set_state("music_pipeline_interrupted", "0")
        set_state("music_pipeline_last_run", datetime.utcnow().isoformat())
        logger.info("[music_pipeline] Complete: %s", summary)

    except Exception as exc:
        logger.error("[music_pipeline] Failed: %s", exc, exc_info=True)
        task_monitor.error(task, str(exc))
        # Keep interrupted="1" so startup auto-resumes
    finally:
        set_state("music_pipeline_running",        "0")
        set_state("music_pipeline_stop_requested", "0")
