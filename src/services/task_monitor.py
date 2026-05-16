"""
Curatarr 1.0 - Task Monitor

Central registry for all background tasks.
Tasks report progress here, frontend polls via SSE.
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING  = "pending"
    RUNNING  = "running"
    DONE     = "done"
    ERROR    = "error"
    SKIPPED  = "skipped"


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

    @property
    def elapsed_s(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.time()
        return end - self.started_at

    @property
    def eta_s(self) -> Optional[float]:
        if self.rate <= 0 or self.total <= 0:
            return None
        remaining = self.total - self.processed
        return remaining / self.rate

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "status": self.status,
            "progress": self.progress,
            "total": self.total,
            "processed": self.processed,
            "elapsed_s": round(self.elapsed_s, 1),
            "eta_s": round(self.eta_s) if self.eta_s else None,
            "rate": round(self.rate, 1),
            "error": self.error,
            "started_at": datetime.fromtimestamp(self.started_at).isoformat() if self.started_at else None,
            "logs": [{"ts": l.ts, "msg": l.message, "level": l.level}
                     for l in list(self.logs)[-10:]],
        }


class TaskMonitor:
    """Singleton task registry. Import and use `task_monitor` everywhere."""

    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._subscribers: list[asyncio.Queue] = []
        self._counter = 0
        # last completed run per category: {category: {"at": iso, "status": "done|error|skipped", "name": str, "message": str}}
        self.last_runs: dict[str, dict] = {}

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def create(self, name: str, category: str, total: int = 0, task_id: str = None) -> Task:
        tid = task_id or self._next_id(category)

        # Deduplicate only when no explicit task_id — explicit IDs always create a new task
        if not task_id:
            for existing in self._tasks.values():
                if (existing.category == category and
                        existing.status in (TaskStatus.RUNNING, TaskStatus.PENDING)):
                    logger.debug("Task already running for category %s, reusing %s", category, existing.id)
                    return existing

        task = Task(id=tid, name=name, category=category, total=total)
        self._tasks[tid] = task
        # Keep only last 100 tasks to prevent unbounded growth
        if len(self._tasks) > 100:
            # Remove oldest finished tasks
            finished = [k for k, v in self._tasks.items()
                       if v.status in (TaskStatus.DONE, TaskStatus.ERROR, TaskStatus.SKIPPED)]
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
        task.finished_at = time.time()
        task.logs.append(TaskLog(ts=time.time(), message="Cancelled by user", level="warn"))
        self._broadcast()
        return True

    def start(self, task: Task) -> Task:
        task.status = TaskStatus.RUNNING
        task.started_at = time.time()
        self._broadcast()
        return task

    def update(self, task: Task, processed: int = None, total: int = None,
               message: str = None, level: str = "info") -> Task:
        if processed is not None:
            prev = task.processed
            task.processed = processed
            elapsed = task.elapsed_s
            if elapsed > 0 and processed > 0:
                task.rate = processed / elapsed
            if task.total > 0:
                task.progress = min(100, int(100 * processed / task.total))

        if total is not None:
            task.total = total
            if task.processed > 0:
                task.progress = min(100, int(100 * task.processed / task.total))

        if message:
            task.logs.append(TaskLog(ts=time.time(), message=message, level=level))

        self._broadcast()
        return task

    def _record_last_run(self, task: Task, message: str = None):
        self.last_runs[task.category] = {
            "at": datetime.fromtimestamp(task.finished_at or time.time()).isoformat(),
            "status": task.status.value,
            "name": task.name,
            "message": message or "",
            "elapsed_s": round(task.elapsed_s, 1),
        }

    def done(self, task: Task, message: str = None) -> Task:
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
            self._record_last_run(task, message)
            self._broadcast()
            return task
        task.status = TaskStatus.DONE
        task.progress = 100
        task.finished_at = time.time()
        if message:
            task.logs.append(TaskLog(ts=time.time(), message=message, level="info"))
        self._record_last_run(task, message)
        self._broadcast()
        return task

    def error(self, task: Task, error: str) -> Task:
        task.status = TaskStatus.ERROR
        task.error = error
        task.finished_at = time.time()
        task.logs.append(TaskLog(ts=time.time(), message=error, level="error"))
        self._record_last_run(task, error)
        self._broadcast()
        return task

    def skip(self, task: Task, reason: str) -> Task:
        task.status = TaskStatus.SKIPPED
        task.finished_at = time.time()
        task.logs.append(TaskLog(ts=time.time(), message=reason, level="info"))
        self._record_last_run(task, reason)
        self._broadcast()
        return task

    def log(self, task: Task, message: str, level: str = "info"):
        task.logs.append(TaskLog(ts=time.time(), message=message, level=level))
        self._broadcast()

    def get_all(self) -> list:
        # Return running first, then recent finished
        tasks = list(self._tasks.values())
        tasks.sort(key=lambda t: (
            0 if t.status == TaskStatus.RUNNING else
            1 if t.status == TaskStatus.PENDING else
            2,
            -(t.started_at or 0)
        ))
        return [t.to_dict() for t in tasks[-50:]]  # keep last 50

    def get_running(self) -> list:
        return [t.to_dict() for t in self._tasks.values()
                if t.status in (TaskStatus.RUNNING, TaskStatus.PENDING)]

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=10)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def _broadcast(self):
        snapshot = self.get_all()
        for q in list(self._subscribers):
            try:
                q.put_nowait(snapshot)
            except asyncio.QueueFull:
                pass  # subscriber too slow, drop


# Global singleton
task_monitor = TaskMonitor()
