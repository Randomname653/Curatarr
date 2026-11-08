"""Plex: notice a play that ended and fetch it now, not at the daily sync.

Plex keeps every play with its time, device and account, so nothing is
lost between syncs; but the curator learned of an evening's viewing only
the next day (chat openers about "yesterday", the recently-watched guard
on deletions, recommendations a day behind). This watcher reads
``/status/sessions`` at a calm pace and, when a session it saw is gone,
runs the incremental history sync a few minutes later. No live display,
no webhooks (they need a Plex Pass), no fast polling: every 5 minutes
while something played in the last hour, every 15 minutes otherwise
(``PLEX_SESSION_POLL_S``; 0 switches it off). One small LAN request each.

Ends are batched: a music session ends every few minutes, so the sync
waits ``SETTLE`` after the last end it saw and runs at most every
``MIN_GAP`` (the sync itself is single-flight).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

QUIET_AFTER = timedelta(hours=1)
QUIET_FACTOR = 3                 # 5 min becomes 15 when nothing played for an hour
SETTLE = timedelta(minutes=3)
MIN_GAP = timedelta(minutes=15)


class SessionWatch:
    """What the last read saw, and when a sync is due. Pure: the loop
    below feeds it reads and asks it what to do."""

    def __init__(self) -> None:
        self.seen: set = set()
        self.last_activity: Optional[datetime] = None
        self.sync_due: Optional[datetime] = None
        self.last_sync: Optional[datetime] = None

    def observe(self, sessions: set, now: datetime) -> None:
        """``sessions``: {(session key, item key)} playing now."""
        if sessions:
            self.last_activity = now
        if self.seen - sessions:                # something ended
            due = now + SETTLE
            if self.last_sync and due < self.last_sync + MIN_GAP:
                due = self.last_sync + MIN_GAP
            self.sync_due = due
        self.seen = sessions

    def should_sync(self, now: datetime) -> bool:
        return self.sync_due is not None and now >= self.sync_due

    def synced(self, now: datetime) -> None:
        self.sync_due, self.last_sync = None, now

    def interval(self, base_s: int, now: datetime) -> int:
        """Seconds to the next read: ``base_s`` while there was activity in
        the last hour or a sync is pending, three times that otherwise."""
        busy = (self.sync_due is not None
                or (self.last_activity is not None and now - self.last_activity < QUIET_AFTER))
        return base_s if busy else base_s * QUIET_FACTOR


async def read_sessions(adapter=None) -> set:
    """{(session key, item key)} of what plays on the Plex server now."""
    from src.services.media_server import get_adapter
    adapter = adapter or get_adapter()
    return {(s.session_key, s.item_key) for s in await adapter.live_sessions()}


async def _sync() -> None:
    """The incremental history sync, outside its hourly cooldown; the
    taste follow-up is flagged as after a scheduled sync."""
    from src.database.runner import run_write
    from src.services.app_state import set_state
    from src.services.plex_sync import sync_plex_history
    res = await sync_plex_history(force=True, recompute_taste=False)
    if (res or {}).get("synced"):
        await run_write(set_state, "plex_followup_pending", datetime.utcnow().isoformat())
        logger.info("[plex] %d new play(s) fetched after a session ended", res["synced"])


async def run_session_watch() -> None:
    """For the life of the process, on a Plex install. A failed read is
    logged and the loop goes on."""
    from src.config import settings
    from src.services.media_server import active_kind
    watch = SessionWatch()
    while True:
        base = int(settings.PLEX_SESSION_POLL_S or 0)
        now = datetime.utcnow()
        if base > 0 and active_kind() == "plex":
            try:
                watch.observe(await read_sessions(), now)
                if watch.should_sync(now):
                    watch.synced(now)
                    await _sync()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - the watcher outlives a Plex restart; logged
                logger.warning("[plex] sessions not read: %s", e)
        await asyncio.sleep(watch.interval(base, now) if base > 0 else 900)


_task: Optional[asyncio.Task] = None


def start_session_watch() -> None:
    global _task
    if _task is None or _task.done():
        from src.services.bg_tasks import track_task
        _task = track_task(run_session_watch(), name="plex_session_watch")
