"""
Curatarr - Task Monitor

Central registry for all background tasks.
Tasks report progress here, frontend polls via SSE.

Three views come out of it (the Activity page, 2026-11):

* ``get_live()``   — what runs or waits NOW (plus the few seconds after a
  task ends, so the row can fade instead of vanishing). This is what the
  SSE stream sends.
* ``get_history()`` — finished runs, newest first, persisted in app_state so
  the log survives a restart. Sub-steps (the per-category taste vectors of
  one recompute, …) are folded into their parent's entry instead of each
  landing as a row of its own.
* ``get_all()``    — the raw registry, kept for the plain ``GET /api/tasks``.
"""

import asyncio
import contextvars
import json
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from src.services.activity import area_of, clean_tags

logger = logging.getLogger(__name__)

# Set by the UI shutdown endpoint BEFORE it raises SIGINT: long-lived SSE
# streams (the task stream's while-True keepalive loop) otherwise hold
# uvicorn's graceful shutdown open forever — "Waiting for connections to
# close" until every browser tab is closed or unloaded. Stream loops wait
# on this and end themselves, releasing the connections within a second.
shutdown_event = asyncio.Event()

# A finished task stays in the live snapshot this long, so the Activity row
# can show its outcome and fade out instead of disappearing mid-read.
LIVE_GRACE_S = 3.0
HISTORY_MAX = 300
_HISTORY_KEY = "activity_history"
_PERSIST_DELAY_S = 2.0

# The task a create() inside this context belongs to (see group()).
_parent_var: contextvars.ContextVar = contextvars.ContextVar("task_parent", default=None)


class TaskStatus(str, Enum):
    PENDING  = "pending"
    RUNNING  = "running"
    DONE     = "done"
    ERROR    = "error"
    SKIPPED  = "skipped"


_LIVE = (TaskStatus.RUNNING, TaskStatus.PENDING)
_FINISHED = (TaskStatus.DONE, TaskStatus.ERROR, TaskStatus.SKIPPED)

# A done-message that says nothing ("Complete") hands the summary to the
# last line the run logged before it.
_GENERIC_DONE = {"", "complete", "completed", "done", "ok", "finished"}
_PARTIAL_PREFIX = "Partial — continues next tick"

# Finished runs that did nothing worth reading: "Nothing to enrich",
# "0 expired rows removed", "No new plays", "pool already full" …
_NOOP_RE = re.compile(
    r"(?:\bnothing (?:to|new)\b|\bno (?:pending|new|items|changes|work|taste data)\b"
    r"|\bup to date\b|\balready (?:up to date|full|fresh|done|current)\b"
    r"|\bnone (?:due|left|found)\b|\bno-op\b|^0\b|\(0\)\s*$|^complete: 0/)",
    re.I,
)


@dataclass
class TaskLog:
    ts: float
    message: str
    level: str = "info"   # info / warn / error


@dataclass
class Task:
    id: str
    name: str
    category: str          # sync / enrichment / embedding / taste / proactive
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0      # 0-100
    total: int = 0         # total items
    processed: int = 0     # items done
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    logs: deque = field(default_factory=lambda: deque(maxlen=50))
    rate: float = 0.0      # items/sec
    # Category pills (music / movie / show / anime): shown as badges, never
    # glued into the name as "Enrichment: ['music', …]".
    tags: list = field(default_factory=list)
    parent_id: Optional[str] = None
    # A step of unknown inner length inside the item count: while it runs,
    # sub_done / sub_total of it count towards ``processed`` (step_size items).
    step_size: int = 0
    sub_done: int = 0
    sub_total: int = 0
    partial: bool = False
    cancelled: bool = False
    noop: Optional[bool] = None   # None = decide from the outcome
    summary: str = ""
    created_at: float = field(default_factory=time.time)
    _archived: bool = False

    @property
    def live(self) -> bool:
        return self.status in _LIVE

    @property
    def elapsed_s(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.time()
        return end - self.started_at

    @property
    def effective_processed(self) -> float:
        """Items done, counting the running step's share of its own items."""
        p = float(self.processed)
        if self.live and self.step_size and self.sub_total > 0:
            p += self.step_size * min(1.0, self.sub_done / self.sub_total)
        return p

    @property
    def eta_s(self) -> Optional[float]:
        # A finished task has no ETA — "Done · 31 / 32 · ETA 8s" read as a bug.
        if self.status != TaskStatus.RUNNING or self.rate <= 0 or self.total <= 0:
            return None
        remaining = self.total - self.effective_processed
        if remaining <= 0:
            return None
        return remaining / self.rate

    @property
    def run_id(self) -> str:
        return f"{self.id}@{int((self.started_at or self.created_at) * 1000)}"

    @property
    def outcome(self) -> str:
        """running | queued | completed | warning | partial | failed |
        cancelled | skipped — the words the Activity view filters on."""
        if self.status == TaskStatus.RUNNING:
            return "running"
        if self.status == TaskStatus.PENDING:
            return "queued"
        if self.status == TaskStatus.ERROR:
            return "failed"
        if self.status == TaskStatus.SKIPPED:
            return "cancelled" if self.cancelled else "skipped"
        if self.partial:
            return "partial"
        if any(l.level in ("warn", "error") for l in self.logs):
            return "warning"
        return "completed"

    @property
    def is_noop(self) -> bool:
        if self.noop is not None:
            return bool(self.noop)
        if self.live or self.status == TaskStatus.ERROR or self.cancelled or self.partial:
            return False
        if self.processed > 0:
            return False
        if self.status == TaskStatus.SKIPPED:
            return True
        summary = (self.summary or "").strip()
        if _NOOP_RE.search(summary):
            return True
        if self.total > 0:
            return True          # "Complete: 0/4 processed"
        return self.elapsed_s < 3 and not re.search(r"[1-9]", summary)

    def current_message(self) -> str:
        return self.logs[-1].message if self.logs else ""

    def to_dict(self) -> dict:
        running = self.status == TaskStatus.RUNNING
        eta = self.eta_s
        processed = self.processed
        total = self.total
        # A completed run shows its full count, not the last progress tick
        # ("Done · 31 / 32"): the final item's update often never arrives.
        if self.status == TaskStatus.DONE and not self.partial and total > 0:
            processed = max(processed, total)
        return {
            "id": self.id,
            "run_id": self.run_id,
            "name": self.name,
            "category": self.category,
            "area": area_of(self.category, self.id),
            "tags": list(self.tags),
            "parent_id": self.parent_id,
            "status": self.status,
            "outcome": self.outcome,
            "progress": self.progress,
            "total": total,
            "processed": processed,
            # Running with no item count and no measurable step: the UI pulses.
            "indeterminate": running and total <= 0 and self.sub_total <= 0,
            "elapsed_s": round(self.elapsed_s, 1),
            "eta_s": round(eta) if eta else None,
            "rate": round(self.rate, 1) if running else 0.0,
            "error": self.error,
            "summary": self.summary,
            "noop": self.is_noop if not self.live else False,
            # UTC with offset: the browser shows it in its own time zone (the
            # server's local time read as the browser's was hours off).
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "logs": [{"ts": l.ts, "msg": l.message, "level": l.level}
                     for l in list(self.logs)[-10:]],
        }


def _iso(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds") if ts else None


def _pick_summary(task: Task, message: Optional[str]) -> str:
    """The line a finished run is remembered by: its done-message, unless
    that says nothing ("Complete", "Partial — …"), then the last informative
    line it logged before."""
    m = (message or "").strip()
    generic = m.lower().rstrip(".!") in _GENERIC_DONE or m.startswith(_PARTIAL_PREFIX)
    if not generic:
        return m
    for log in reversed(task.logs):
        text = (log.message or "").strip()
        if text and text != m and text.lower().rstrip(".!") not in _GENERIC_DONE \
                and not text.startswith(_PARTIAL_PREFIX) and text != "Cancelled by user":
            return text
    return m


class TaskMonitor:
    """Singleton task registry. Import and use `task_monitor` everywhere."""

    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._subscribers: list[asyncio.Queue] = []
        self._counter = 0
        # last completed run per category: {category: {"at": iso, "status": "done|error|skipped", "name": str, "message": str}}
        self.last_runs: dict[str, dict] = {}
        # Finished runs, oldest first; {run_id: entry} for upserts.
        self._history: dict[str, dict] = {}
        self._history_lock = threading.Lock()
        self._history_loaded = False
        self._persist_pending = False
        self.persist = True
        self._loop = None

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    # ── grouping ─────────────────────────────────────────────────────────────

    def group(self, parent: Task):
        """Context manager: every task created inside belongs to ``parent``
        (one recompute's four per-category taste vectors become one run with
        four sub-steps instead of four rows in the history)."""
        monitor = self

        class _Group:
            def __enter__(self_inner):
                self_inner.token = _parent_var.set(parent.id if parent else None)
                return parent

            def __exit__(self_inner, *exc):
                _parent_var.reset(self_inner.token)
                return False

        return _Group()

    def children(self, task_id: str) -> list:
        return sorted((t for t in self._tasks.values() if t.parent_id == task_id),
                      key=lambda t: t.started_at or t.created_at)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def create(self, name: str, category: str, total: int = 0, task_id: str = None,
               tags: list = None) -> Task:
        tid = task_id or self._next_id(category)
        tags = clean_tags(tags)

        # Deduplicate only when no explicit task_id AND the name matches too.
        # Matching on category alone was too broad: the manual "Enrichment: ..."
        # run and the scheduler's "Source Upgrade" / "Enrichment TTL Refresh" /
        # "ARR Pre-Enrichment" jobs ALL use category="enrichment". A scheduler
        # job firing mid-run would dedupe-return the manual run's Task, then
        # start()+done() it — so the live card flipped to "Done" (with a garbage
        # rate from the reset started_at) while the real pipeline kept running.
        # Requiring the same name keeps the intended "don't double-card the same
        # operation" behaviour without letting distinct jobs hijack each other.
        # The category pills are part of the name for this purpose.
        if not task_id:
            for existing in self._tasks.values():
                if (existing.category == category and
                        existing.name == name and existing.tags == tags and
                        existing.status in (TaskStatus.RUNNING, TaskStatus.PENDING)):
                    logger.debug("Task already running (%s / %s), reusing %s",
                                 category, name, existing.id)
                    return existing

        parent_id = _parent_var.get()
        if parent_id == tid or parent_id not in self._tasks:
            parent_id = None
        task = Task(id=tid, name=name, category=category, total=total, tags=tags,
                    parent_id=parent_id)
        self._tasks[tid] = task
        # Keep only last 100 tasks to prevent unbounded growth
        if len(self._tasks) > 100:
            # Remove oldest finished tasks
            finished = [k for k, v in self._tasks.items() if v.status in _FINISHED]
            for k in finished[:-50]:
                del self._tasks[k]
        self._broadcast()
        return task

    def cancel(self, task_id: str) -> bool:
        """Mark a task as cancelled. Actual cancellation depends on the running code checking status."""
        task = self._tasks.get(task_id)
        if not task:
            return False
        if task.status in (TaskStatus.DONE, TaskStatus.ERROR):
            return False
        task.status = TaskStatus.SKIPPED
        task.cancelled = True
        task.finished_at = time.time()
        task.logs.append(TaskLog(ts=time.time(), message="Cancelled by user", level="warn"))
        task.summary = "Cancelled by user"
        for child in self.children(task_id):
            if child.live:
                self.cancel(child.id)
        self._archive(task)
        self._broadcast()
        return True

    def start(self, task: Task) -> Task:
        task.status = TaskStatus.RUNNING
        task.started_at = time.time()
        task.finished_at = None
        task._archived = False
        self._broadcast()
        return task

    def update(self, task: Task, processed: int = None, total: int = None,
               message: str = None, level: str = "info") -> Task:
        if total is not None:
            task.total = total
        if processed is not None:
            task.processed = processed
            # A new item count ends the running step: its items are counted now.
            task.step_size = task.sub_done = task.sub_total = 0
        if processed is not None or total is not None:
            self._recalc(task)

        if message:
            task.logs.append(TaskLog(ts=time.time(), message=message, level=level))

        self._broadcast()
        return task

    def begin_step(self, task: Task, size: int, message: str = None) -> Task:
        """The next ``size`` items are worked as one opaque step whose inner
        progress substep() reports (the ARR sync's per-category judging: the
        bar sat at 0% for minutes while one category ran)."""
        task.step_size = max(0, int(size or 0))
        task.sub_done = task.sub_total = 0
        if message:
            task.logs.append(TaskLog(ts=time.time(), message=message))
        self._broadcast()
        return task

    def substep(self, task: Task, done: int, total: int, message: str = None) -> Task:
        """Inner progress of the running step (or of the whole task when it
        has no item count of its own)."""
        task.sub_done = max(0, int(done or 0))
        task.sub_total = max(0, int(total or 0))
        self._recalc(task)
        if message:
            task.logs.append(TaskLog(ts=time.time(), message=message))
        self._broadcast()
        return task

    def _recalc(self, task: Task) -> None:
        if task.total > 0:
            eff = task.effective_processed
            task.progress = min(100, int(100 * eff / task.total))
            elapsed = task.elapsed_s
            if elapsed > 0 and eff > 0:
                task.rate = eff / elapsed
        elif task.sub_total > 0:
            task.progress = min(100, int(100 * task.sub_done / task.sub_total))

    def _record_last_run(self, task: Task, message: str = None):
        self.last_runs[task.category] = {
            "at": datetime.fromtimestamp(task.finished_at or time.time()).isoformat(),
            "status": task.status.value,
            "name": task.name,
            "message": message or "",
            "elapsed_s": round(task.elapsed_s, 1),
        }

    def done(self, task: Task, message: str = None, *, partial: bool = False,
             noop: Optional[bool] = None) -> Task:
        """``partial``: the run made progress and more is left (the next tick
        continues it). ``noop``: say outright whether the run did anything —
        otherwise it is judged from the counts and the summary."""
        if noop is not None:
            task.noop = noop
        # Pass 92: don't clobber a prior SKIPPED status with DONE. When a
        # task is cancelled mid-run (``self.cancel(task_id)`` from the
        # /api/tasks/{id}/cancel endpoint), the cooperative-cancel path
        # — producer breaks the loop, consumer drains the queue and
        # calls done() at the end — used to flip status straight back to
        # DONE. The UI then showed "✓ Done" for an obviously-truncated
        # run (e.g. "56/3,186 processed"), masking the cancellation. Keep
        # the SKIPPED status but append the done-time message so the
        # audit log still records what got through. The progress bar
        # stays at the truncated value so the partial-completion is
        # visible at a glance.
        if task.status == TaskStatus.SKIPPED:
            if message:
                task.logs.append(TaskLog(
                    ts=time.time(),
                    message=f"(after cancel) {message}",
                    level="info",
                ))
                task.summary = f"Cancelled — {message}" if task.cancelled else message
            self._record_last_run(task, message)
            self._archive(task)
            self._broadcast()
            return task
        task.status = TaskStatus.DONE
        task.partial = bool(partial or (message or "").startswith(_PARTIAL_PREFIX))
        if not task.partial:
            task.progress = 100
        task.step_size = task.sub_done = task.sub_total = 0
        task.finished_at = time.time()
        if message:
            task.logs.append(TaskLog(ts=time.time(), message=message, level="info"))
        task.summary = _pick_summary(task, message)
        self._record_last_run(task, message)
        self._archive(task)
        self._broadcast()
        return task

    def error(self, task: Task, error: str) -> Task:
        task.status = TaskStatus.ERROR
        task.error = error
        task.finished_at = time.time()
        task.logs.append(TaskLog(ts=time.time(), message=error, level="error"))
        task.summary = error or ""
        self._record_last_run(task, error)
        self._archive(task)
        self._broadcast()
        return task

    def skip(self, task: Task, reason: str) -> Task:
        task.status = TaskStatus.SKIPPED
        task.finished_at = time.time()
        task.logs.append(TaskLog(ts=time.time(), message=reason, level="info"))
        task.summary = reason or ""
        self._record_last_run(task, reason)
        self._archive(task)
        self._broadcast()
        return task

    def log(self, task: Task, message: str, level: str = "info"):
        task.logs.append(TaskLog(ts=time.time(), message=message, level=level))
        self._broadcast()

    # ── reads ────────────────────────────────────────────────────────────────

    def get_all(self) -> list:
        # Return running first, then pending, then most-recent finished.
        tasks = list(self._tasks.values())
        tasks.sort(key=lambda t: (
            0 if t.status == TaskStatus.RUNNING else
            1 if t.status == TaskStatus.PENDING else
            2,
            -(t.started_at or 0)
        ))
        # Take the TOP 50 of the sorted list, NOT the last 50. The sort puts
        # running/pending tasks at the FRONT, so tasks[-50:] sliced them off
        # once the registry held >50 tasks — a long-running job (e.g. a 1h
        # enrichment) would silently vanish from the activity view while the
        # oldest finished jobs were shown instead. tasks[:50] keeps the
        # running jobs + the most recent finished ones.
        return [t.to_dict() for t in tasks[:50]]

    def get_live(self, now: float = None) -> list:
        """Running and queued tasks, plus those that ended in the last
        LIVE_GRACE_S seconds (the row fades out instead of vanishing). A
        finished task never lingers here: its place is the history."""
        now = time.time() if now is None else now
        out = [t for t in self._tasks.values()
               if t.live or (t.finished_at and now - t.finished_at <= LIVE_GRACE_S)]
        out.sort(key=lambda t: (
            0 if t.status == TaskStatus.RUNNING else 1 if t.status == TaskStatus.PENDING else 2,
            t.started_at or t.created_at))
        return [t.to_dict() for t in out]

    def get_running(self) -> list:
        return [t.to_dict() for t in self._tasks.values()
                if t.status in (TaskStatus.RUNNING, TaskStatus.PENDING)]

    # ── history ──────────────────────────────────────────────────────────────

    def _entry(self, task: Task) -> dict:
        d = task.to_dict()
        steps = []
        for child in self.children(task.id):
            c = child.to_dict()
            steps.append({k: c[k] for k in (
                "name", "tags", "outcome", "elapsed_s", "processed", "total",
                "summary", "error", "noop")})
        # A run whose sub-steps did work is not a no-op, whatever its own count.
        if steps and not all(st["noop"] for st in steps):
            d["noop"] = False
        logs = [{"msg": (l.message or "")[:240], "level": l.level}
                for l in list(task.logs)[-6:]]
        return {
            "run_id": d["run_id"], "id": d["id"], "name": d["name"],
            "category": d["category"], "area": d["area"], "tags": d["tags"],
            "outcome": d["outcome"], "noop": d["noop"],
            "started_at": d["started_at"], "finished_at": d["finished_at"],
            "elapsed_s": d["elapsed_s"], "processed": d["processed"], "total": d["total"],
            "summary": (d["summary"] or "")[:400], "error": d["error"],
            "steps": steps, "logs": logs,
        }

    def _archive(self, task: Task) -> None:
        """A finished run goes into the history — or, when it is a sub-step of
        a run, into its parent's entry."""
        if task.live:
            return
        parent = self._tasks.get(task.parent_id) if task.parent_id else None
        if parent is not None:
            if parent._archived:      # the parent ended first: refresh its entry
                self._archive(parent)
            return
        task._archived = True
        entry = self._entry(task)
        with self._history_lock:
            self._history.pop(entry["run_id"], None)
            self._history[entry["run_id"]] = entry
            while len(self._history) > HISTORY_MAX:
                self._history.pop(next(iter(self._history)))
        self._schedule_persist()

    def runs_since(self, ts: float) -> list:
        """History entries of top-level runs that started at or after ``ts``
        (newest last) — how the custodian learns what a self-carding job did."""
        stamp = _iso(ts)
        with self._history_lock:
            return [e for e in self._history.values()
                    if (e.get("started_at") or "") >= stamp]

    def get_history(self, limit: int = 200) -> list:
        """Finished runs, newest first (persisted entries included)."""
        self._ensure_loaded()
        with self._history_lock:
            entries = list(self._history.values())
        entries.sort(key=lambda e: e.get("finished_at") or "", reverse=True)
        return entries[:limit]

    def _ensure_loaded(self) -> None:
        """Merge the persisted history in once. Reads app_state: call it from
        a worker thread (sync routes, the DB writer), not the event loop."""
        if self._history_loaded or not self.persist:
            return
        try:
            from src.services.app_state import get_state
            raw = get_state(_HISTORY_KEY)
            stored = json.loads(raw) if raw else []
        except Exception as e:  # noqa: BLE001 - an unreadable log starts empty
            logger.warning("[tasks] stored activity history unreadable: %s", e)
            stored = []
        with self._history_lock:
            if self._history_loaded:
                return
            merged = {e["run_id"]: e for e in stored
                      if isinstance(e, dict) and e.get("run_id")}
            for rid, e in self._history.items():   # this process's runs win
                merged.pop(rid, None)
                merged[rid] = e
            ordered = sorted(merged.values(), key=lambda e: e.get("finished_at") or "")
            self._history = {e["run_id"]: e for e in ordered[-HISTORY_MAX:]}
            self._history_loaded = True

    def _persist_now(self) -> None:
        """Write the history to app_state (DB writer thread)."""
        self._ensure_loaded()
        with self._history_lock:
            snapshot = list(self._history.values())
        try:
            from src.services.app_state import set_state
            set_state(_HISTORY_KEY, json.dumps(snapshot, default=str))
        except Exception as e:  # noqa: BLE001 - the live view is unaffected
            logger.warning("[tasks] could not store the activity history: %s", e)

    def _schedule_persist(self) -> None:
        """Debounced: one write a couple of seconds after a burst of finishes,
        on the DB writer thread. No loop (scripts, tests): nothing is written."""
        if not self.persist or self._persist_pending:
            return
        loop = self._loop
        try:
            loop = asyncio.get_running_loop()
            self._loop = loop
        except RuntimeError:
            pass
        if loop is None or loop.is_closed():
            return
        self._persist_pending = True

        def _flush():
            self._persist_pending = False
            try:
                from src.database.runner import _get_writer
                loop.run_in_executor(_get_writer(), self._persist_now)
            except RuntimeError:
                pass   # loop shutting down

        try:
            loop.call_soon_threadsafe(loop.call_later, _PERSIST_DELAY_S, _flush)
        except RuntimeError:
            self._persist_pending = False

    # ── streaming ────────────────────────────────────────────────────────────

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=10)
        self._subscribers.append(q)
        # The loop the SSE streams wait on — a broadcast from a worker thread
        # (offloaded DB / Chroma work reporting progress) is handed to it.
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        return q

    def unsubscribe(self, q: asyncio.Queue):
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def _broadcast(self):
        if not self._subscribers:
            return
        # asyncio.Queue is not thread-safe: put_nowait from a worker thread
        # can miss waking the SSE coroutine waiting on it. Off the loop, the
        # put is scheduled onto the loop instead.
        loop = getattr(self, "_loop", None)
        if loop is not None and not loop.is_closed():
            try:
                on_loop = asyncio.get_running_loop() is loop
            except RuntimeError:
                on_loop = False
            if not on_loop:
                try:
                    loop.call_soon_threadsafe(self._broadcast_now)
                except RuntimeError:
                    pass   # loop shutting down
                return
        self._broadcast_now()

    def _broadcast_now(self):
        snapshot = self.get_live()
        for q in list(self._subscribers):
            try:
                q.put_nowait(snapshot)
            except asyncio.QueueFull:
                pass  # subscriber too slow, drop


# Global singleton
task_monitor = TaskMonitor()
