"""Plex: a play that ended is fetched within minutes (services/plex_sessions).

- the pace: every PLEX_SESSION_POLL_S (5 min) while something played in
  the last hour or a sync is pending, three times that otherwise;
- an ended session schedules the history sync SETTLE (3 min) later; more
  ends in the meantime move it, never closer than MIN_GAP (15 min) after
  the last sync; nothing ended, no sync;
- the loop: off at 0, idle on a Jellyfin install, a failed read is logged
  and the loop goes on, and the sync runs once when due.

    python tests/test_plex_sessions.py
"""
from _harness import check  # noqa: I001 - first: temp stores

import asyncio
from datetime import datetime, timedelta

from src.services import plex_sessions as ps

T = datetime(2026, 11, 8, 20, 0)
w = ps.SessionWatch()
check("idle from the start: the slow pace", w.interval(300, T) == 900)
w.observe({("41", "1001")}, T)
check("something plays: the 5-minute pace, no sync", w.interval(300, T) == 300 and not w.should_sync(T))
w.observe({("41", "1001")}, T + timedelta(minutes=5))
check("still playing: still nothing to fetch", w.sync_due is None)
w.observe(set(), T + timedelta(minutes=10))
check("the session ended: the sync is due three minutes later",
      w.sync_due == T + timedelta(minutes=13) and not w.should_sync(T + timedelta(minutes=12))
      and w.should_sync(T + timedelta(minutes=13)))
w.synced(T + timedelta(minutes=13))
w.observe({("42", "5")}, T + timedelta(minutes=15))
w.observe(set(), T + timedelta(minutes=18))
check("the next end soon after: not before fifteen minutes after the last sync",
      w.sync_due == T + timedelta(minutes=28))
check("an hour after the last play: the slow pace again",
      ps.SessionWatch().interval(300, T) == 900 and w.interval(300, T + timedelta(minutes=19)) == 300)
w.synced(T + timedelta(minutes=28))
check("...and once synced and quiet for an hour", w.interval(300, T + timedelta(hours=2)) == 900)


class _Stop(Exception):
    pass


async def _loop(reads, kind="plex", poll=300):
    """The watch loop over ``reads`` (sets, or an exception to raise); the
    sync and the clock replaced."""
    from src.config import settings
    import src.services.media_server as ms
    synced, sleeps = [], []
    it = iter(reads)

    async def read(adapter=None):
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r

    async def sync():
        synced.append(1)

    async def sleep(s):
        sleeps.append(s)
        if len(sleeps) >= len(reads):
            raise _Stop()
        clock[0] += timedelta(seconds=s)
    clock = [T]

    class _DT:
        @staticmethod
        def utcnow():
            return clock[0]
    saved = (ps.read_sessions, ps._sync, ps.asyncio.sleep, ps.datetime, ms.active_kind, settings.PLEX_SESSION_POLL_S)
    ps.read_sessions, ps._sync, ps.asyncio.sleep, ps.datetime = read, sync, sleep, _DT
    ms.active_kind, settings.PLEX_SESSION_POLL_S = (lambda: kind), poll
    try:
        await ps.run_session_watch()
    except _Stop:
        pass
    finally:
        (ps.read_sessions, ps._sync, ps.asyncio.sleep, ps.datetime, ms.active_kind,
         settings.PLEX_SESSION_POLL_S) = saved
    return synced, sleeps

play = {("41", "1001")}
synced, sleeps = asyncio.run(_loop([play, set(), set(), set()]))
check("the loop: a play ends, the next read after the settle time fetches it once",
      synced == [1] and sleeps[0] == 300)
synced, sleeps = asyncio.run(_loop([play, RuntimeError("plex down"), set(), set(), set()]))
check("a failed read is logged and the loop goes on", synced == [1])
synced, sleeps = asyncio.run(_loop([play, set()], kind="jellyfin"))
check("on a Jellyfin install it idles", synced == [] and sleeps[0] == 900)
synced, sleeps = asyncio.run(_loop([play, set()], poll=0))
check("PLEX_SESSION_POLL_S=0: off", synced == [] and sleeps[0] == 900)

check.done()
