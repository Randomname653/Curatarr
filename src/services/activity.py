"""
Curatarr - Activity view model

The Activity page has three parts, the way Sonarr / Radarr lay out theirs:

* Queue      — what runs or waits now (task_monitor.get_live, over SSE)
* Scheduled  — every registered background job: cadence, last execution,
               last duration and result, next execution, Run now
               (``scheduled_overview`` / ``run_now`` below)
* History    — finished runs (task_monitor.get_history)

This module holds what the three share: which area a job or task belongs to
(the History filters and the Scheduled groups), the formatting of category
lists, and the Scheduled tab itself.

Imports at module level are stdlib only: task_monitor imports this module.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ── areas ────────────────────────────────────────────────────────────────────

AREAS = {
    "sync": "Media server & Arr apps",
    "enrichment": "Enrichment",
    "taste": "Taste, recommendations & curator",
    "maintenance": "Maintenance & backup",
    "system": "System watchers",
}

# Background jobs (data custodian tasks + the scheduler's own interval jobs).
JOB_AREA = {
    # media server & arr apps
    "plex_sync": "sync", "arr_sync": "sync", "arr_cache_refresh": "sync",
    "arr_ledger": "sync", "music_catalog_sync": "sync", "shoko_watched": "sync",
    "deletion_returns": "sync", "limbo_reconcile": "sync", "plex_players": "sync",
    "editions_sync": "sync", "netflix_relink": "sync", "plex_rec_playlist": "sync",
    "plex_music_playlist": "sync", "plex_collections": "sync", "lyrics_sync": "sync",
    "music_pipeline": "sync", "spotify_reread": "sync",
    # enrichment
    "arr_pre_enrich": "enrichment", "custodian_enrich": "enrichment",
    "custodian_omdb": "enrichment", "custodian_signif": "enrichment",
    "custodian_recept": "enrichment", "custodian_wikidata": "enrichment",
    "source_upgrade": "enrichment", "raw_refresh": "enrichment",
    "discogs_styles": "enrichment", "credits_index": "enrichment",
    "anime_crossref": "enrichment", "custodian_audit": "enrichment",
    "music_tags": "enrichment", "lyrics_profile": "enrichment",
    "facet_backfill": "enrichment", "facet_phrases": "enrichment",
    "vector_reconcile": "enrichment",
    # taste, recommendations & curator
    "plex_followup": "taste", "custodian_taste": "taste", "custodian_recs": "taste",
    "taste_snapshots": "taste", "nanogenres": "taste", "nanogenre_names": "taste",
    "debate_outcomes": "taste", "wrapup_pregen": "taste", "chat_starters": "taste",
    "memory_catchup": "taste", "proactive_messages": "taste", "milestones": "taste",
    # maintenance & backup
    "db_backup": "maintenance", "db_vacuum": "maintenance", "cache_prune": "maintenance",
    "retention": "maintenance", "memory_decay": "maintenance",
    "orphan_check": "maintenance", "model_update_check": "maintenance",
    "model_update_recover": "maintenance", "data_custodian": "maintenance",
    "custodian_sprint": "maintenance",
    # system watchers (heartbeats)
    "game_watcher": "system", "ollama_watch": "system", "pc_agent_poll": "system",
}

# task_monitor categories → area.
CATEGORY_AREA = {
    "sync": "sync", "arr_sync": "sync", "import": "sync", "music": "sync",
    "media_server": "sync",
    "enrichment": "enrichment", "embedding": "enrichment",
    "taste": "taste", "recs": "taste", "proactive": "taste", "curation": "taste",
    "memory": "taste", "wrapup": "taste",
    "maintenance": "maintenance", "memory_decay": "maintenance",
    "orphan_check": "maintenance", "models": "maintenance",
}

# Heartbeats: a result per 30 s tick is kept in memory only — a database
# write every 30 s for "the PC answered" would be the noise, not the log.
# The sprint tick fires every 2 min and is a no-op without a sprint.
HEARTBEAT_JOBS = {"game_watcher", "ollama_watch", "pc_agent_poll", "custodian_sprint"}

# Jobs the Scheduled tab lists but offers no Run now for: the one-shot
# recovery runs once after a start; the sprint tick is a no-op without one.
NO_RUN_NOW = {"model_update_recover"}


def area_of(category: str, task_id: str = "") -> str:
    """The area of a live task / history entry (History filter)."""
    cat = category or ""
    tid = task_id or ""
    if cat == "custodian" and tid.startswith("cust-"):
        return JOB_AREA.get(tid[5:], "maintenance")
    if cat.startswith("enrich-"):
        return "enrichment"
    if tid.startswith("del-analysis-"):
        return "taste"
    return CATEGORY_AREA.get(cat, "maintenance")


# ── formatting ───────────────────────────────────────────────────────────────

def clean_tags(tags) -> list:
    """A list of category pills: strings, de-duplicated, order kept."""
    if not tags:
        return []
    if isinstance(tags, str):
        tags = [tags]
    out = []
    for t in tags:
        s = str(t).strip()
        if s and s not in out:
            out.append(s)
    return out


def fmt_list(items, empty: str = "all categories") -> str:
    """["music", "movie"] → "music, movie"; never the str() of a list, and an
    empty list says so in words instead of "across []"."""
    items = clean_tags(items)
    return ", ".join(items) if items else empty


def fmt_duration(seconds) -> str:
    s = int(round(float(seconds or 0)))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


def fmt_cadence(hours: float) -> str:
    """0.4 → "every 24 min", 24 → "daily", 168 → "weekly"."""
    h = float(hours or 0)
    if h <= 0:
        return "on demand"
    minutes = round(h * 60)
    if minutes < 60:
        return f"every {minutes} min"
    if abs(h - 24) < 1e-6:
        return "daily"
    if abs(h - 168) < 1e-6:
        return "weekly"
    if h % 24 == 0:
        return f"every {int(h // 24)} days"
    if h == int(h):
        return "hourly" if h == 1 else f"every {int(h)} h"
    return f"every {h:g} h"


def _trigger_cadence(trigger) -> str:
    """An APScheduler trigger in words."""
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    if isinstance(trigger, IntervalTrigger):
        secs = trigger.interval.total_seconds()
        if secs < 60:
            return f"every {int(secs)} s"
        return fmt_cadence(secs / 3600)
    if isinstance(trigger, DateTrigger):
        return "once after start"
    if isinstance(trigger, CronTrigger):
        fields = {f.name: str(f) for f in trigger.fields}
        if (fields.get("hour") == "*" and fields.get("day") == "*"
                and fields.get("day_of_week") == "*" and fields.get("minute", "*") != "*"):
            return f"hourly at :{int(fields['minute']):02d}" \
                if fields["minute"].isdigit() else f"hourly ({fields['minute']})"
        return "cron " + " ".join(f"{k}={v}" for k, v in fields.items()
                                  if v != "*" and k not in ("second", "year", "week"))
    return str(trigger)


def _utc_iso(dt: datetime | None) -> str | None:
    """A naive-UTC stamp (app_state) or an aware one as ISO with offset, so
    the browser renders it in its own time zone."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# ── the scheduler's own jobs: running state + last result ───────────────────

_job_running: set = set()
_job_results: dict = {}      # job_id → {"at", "seconds", "result", "summary"}


def job_started(job_id: str) -> None:
    _job_running.add(job_id)


def job_finished(job_id: str, seconds: float, result: str, summary: str = "") -> dict:
    _job_running.discard(job_id)
    rec = {"at": datetime.utcnow().isoformat(timespec="seconds"),
           "seconds": round(float(seconds), 1), "result": result,
           "summary": (summary or "")[:240]}
    _job_results[job_id] = rec
    return rec


def _scheduler_result(job_id: str) -> dict:
    """In-memory first (this process); else the persisted one."""
    if job_id in _job_results:
        return _job_results[job_id]
    if job_id in HEARTBEAT_JOBS:
        return {}
    from src.services.data_custodian import last_result
    return last_result(job_id)


# ── scheduled overview ───────────────────────────────────────────────────────

def _blocked_reason(t) -> str:
    """Why a due custodian task would not start on the next tick, or ""."""
    from src.services import data_custodian as dc
    try:
        if not dc._backoff_due(t.job_id, t.cadence_h):
            return "backing off after a failed run"
        if t.needs_llm and dc._gaming():
            from src.services.llm_lane import CPU, lane
            if lane(t.llm_role) != CPU:
                return "waiting for the GPU (a game holds it)"
        if t.job_id == "arr_sync":
            from src.services.ollama_hosts import judge_ready
            if not judge_ready():
                return "waiting for the PC agent"
    except Exception as e:  # noqa: BLE001 - a status read never fails the page
        logger.debug("[activity] blocked-reason read for %s failed: %s", t.job_id, e)
    return ""


def _result_view(rec: dict) -> dict:
    result = (rec or {}).get("result") or ""
    if result.startswith("error"):
        result = "error"
    return {
        "last_duration_s": (rec or {}).get("seconds"),
        "last_result": result or None,
        "last_summary": (rec or {}).get("summary") or "",
        "last_noop": bool((rec or {}).get("noop")),
    }


def _first_tick_after(eligible: datetime, next_tick, tick_minutes: float) -> datetime:
    """A custodian task runs on the first tick at or after it becomes due."""
    if next_tick is None or next_tick >= eligible:
        return next_tick or eligible
    step = timedelta(minutes=tick_minutes)
    n = -(-(eligible - next_tick) // step)      # ceil
    return next_tick + n * step


def scheduled_overview() -> dict:
    """Every registered background job, grouped by area. Reads app_state
    (the persisted stamps survive restarts): call it off the event loop."""
    from src.services import data_custodian as dc
    from src.services.scheduler import _job_overdue, _last_job_run, scheduler

    now = datetime.now(timezone.utc)
    running = bool(getattr(scheduler, "running", False))

    def _next_fire(job_id: str):
        if not running:
            return None
        job = scheduler.get_job(job_id)
        return job.next_run_time if job is not None else None

    sprint = dc.sprint_until() is not None     # fails closed to "no sprint" itself
    next_tick = _next_fire("custodian_sprint" if sprint else "data_custodian")
    tick_minutes = dc.SPRINT_TICK_MINUTES if sprint else dc.CUSTODIAN_TICK_MINUTES
    tick_label = (f"every {tick_minutes} min (sprint)" if sprint
                  else f"every {tick_minutes} min")

    jobs: list = []
    for t in dc._registry():
        last = _last_job_run(t.job_id)
        rec = dc.last_result(t.job_id)
        due = _job_overdue(t.job_id, t.cadence_h)
        # A task ahead in a busy queue but not due is skipped at its turn:
        # it stays idle here, with its next run and a usable Run now.
        state = dc.job_state(t.job_id, due=bool(due))
        blocked = _blocked_reason(t) if due and state == "idle" else ""
        if state == "running":
            next_at, next_note = None, "running now"
        elif state == "queued":
            next_at, next_note = None, "queued"
        elif due:
            next_at = next_tick
            next_note = blocked or ("due — next custodian tick" if next_tick else "due now")
        else:
            eligible = (last + timedelta(hours=t.cadence_h * 1.1)).replace(tzinfo=timezone.utc)
            next_at = _first_tick_after(eligible, next_tick, tick_minutes)
            next_note = ""
        # A partial run stamps nothing: the last execution is the result's
        # time when that is newer than the stamp.
        last_exec = last
        if rec.get("at"):
            try:
                at = datetime.fromisoformat(rec["at"])
                last_exec = at if not last or at > last else last
            except (TypeError, ValueError):
                pass
        jobs.append({
            "job_id": t.job_id,
            "name": t.label,
            "area": JOB_AREA.get(t.job_id, "maintenance"),
            "kind": "custodian",
            "cadence": fmt_cadence(t.cadence_h),
            "cadence_h": t.cadence_h,
            "uses_model": bool(t.needs_llm),
            "last_run": _utc_iso(last_exec),
            "last_success": _utc_iso(last),
            **_result_view(rec),
            "state": state,
            "due": bool(due),
            "blocked": blocked,
            "next_run": _utc_iso(next_at) if next_at else None,
            "next_note": next_note,
            "can_run": state == "idle",
        })

    if running:
        for job in scheduler.get_jobs():
            jid = job.id
            if any(j["job_id"] == jid for j in jobs):
                continue
            last = _last_job_run(jid) if jid not in HEARTBEAT_JOBS else None
            rec = _scheduler_result(jid)
            if rec.get("at"):
                try:
                    at = datetime.fromisoformat(rec["at"])
                    last = at if not last or at > last else last
                except (TypeError, ValueError):
                    pass
            state = "running" if jid in _job_running else "idle"
            nrt = job.next_run_time
            jobs.append({
                "job_id": jid,
                "name": _JOB_NAMES.get(jid, job.name),
                "area": JOB_AREA.get(jid, "maintenance"),
                "kind": "scheduler",
                "cadence": (tick_label if jid == "data_custodian"
                            else _trigger_cadence(job.trigger)),
                "cadence_h": None,
                "uses_model": False,
                "last_run": _utc_iso(last),
                "last_success": _utc_iso(last),
                **_result_view(rec),
                "state": state,
                "due": bool(nrt and nrt <= now),
                "blocked": "",
                "next_run": _utc_iso(nrt) if nrt else None,
                "next_note": "running now" if state == "running"
                             else ("" if nrt else "paused"),
                "can_run": state == "idle" and jid not in NO_RUN_NOW and nrt is not None,
            })

    order = list(AREAS)
    jobs.sort(key=lambda j: (order.index(j["area"]) if j["area"] in order else 99,
                             j["name"].lower()))
    groups = [{"area": a, "label": AREAS[a],
               "jobs": [j for j in jobs if j["area"] == a]} for a in order]
    return {
        "now": _utc_iso(now),
        "scheduler_running": running,
        "custodian": {
            "tick": tick_label,
            "next_tick": _utc_iso(next_tick),
            "sprint": sprint,
            "queues": {q: {"busy": dc._queue_busy[q], "current": dc._queue_current[q]}
                       for q in dc._QUEUES},
        },
        "groups": [g for g in groups if g["jobs"]],
    }


# Plain names for the scheduler's own jobs (its registered names carry
# pass numbers and notes for the log).
_JOB_NAMES = {
    "data_custodian": "Data custodian tick",
    "custodian_sprint": "Keep-working sprint tick",
    "proactive_messages": "Proactive message cache",
    "source_upgrade": "Source upgrade pass",
    "arr_cache_refresh": "Arr library cache refresh",
    "model_update_check": "Model update check",
    "model_update_recover": "Model update test roll-back",
    "game_watcher": "Game-mode VRAM watcher",
    "ollama_watch": "Ollama health check",
    "pc_agent_poll": "PC agent poll",
}


class RunNowError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _custodian_job_ids() -> set:
    from src.services import data_custodian as dc
    return {t.job_id for t in dc._registry()}


async def run_now(job_id: str) -> dict:
    """Trigger one job outside its schedule (the Scheduled tab's Run now)."""
    from src.database.runner import run_read
    from src.services import data_custodian as dc
    from src.services.scheduler import scheduler
    # The registry's labels read the media-server setting: off the loop.
    if job_id in await run_read(_custodian_job_ids):
        state = dc.request_run(job_id)
        if state == "running":
            raise RunNowError(409, "already_running")
        return {"ok": True, "job_id": job_id, "state": state}
    if job_id in NO_RUN_NOW:
        raise RunNowError(400, "not_runnable")
    job = scheduler.get_job(job_id) if getattr(scheduler, "running", False) else None
    if job is None:
        raise RunNowError(404, "job_not_found")
    if job_id in _job_running:
        raise RunNowError(409, "already_running")
    if job.next_run_time is None:
        raise RunNowError(409, "job_paused")
    job.modify(next_run_time=datetime.now(timezone.utc))
    return {"ok": True, "job_id": job_id, "state": "started"}
