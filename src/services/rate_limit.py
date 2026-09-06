"""
Curatarr — per-user request budgets for the endpoints that turn ONE HTTP
request into LLM time or external-API spend.

The app is a household server on a LAN with one GPU. Every logged-in
member may chat, search and refresh their recommendations — that is the
product — but a script with a member token (or a stolen one) could queue
hundreds of curator generations, hammer the summarizer with search parses,
or stack recommendation refreshes for the same user. The global curator
gate serialises the GPU; it does not stop one caller from owning the queue.

Two small primitives, both in-process (one worker, one loop):

  * ``enforce(scope, key, limit, window_s)`` — sliding-window budget per
    (scope, key); raises 429 with a Retry-After header when exhausted.
  * ``InFlight`` — "one at a time per key" guard for long-running work
    (a chat generation, a recommendation refresh) so a second call returns
    409 instead of stacking.

Both fail OPEN on internal errors (a limiter bug must never lock the owner
out of their own server) and forget keys once their window expires.
"""
from __future__ import annotations

import threading
import time
from typing import Hashable, Optional

from fastapi import HTTPException

_BUCKETS: dict[tuple[str, str], list[float]] = {}
_LOCK = threading.Lock()
_MAX_KEYS = 5000

# Budgets. Generous for a human, tight for a script. Owner-tunable later via
# settings if a household ever hits them in earnest.
CHAT_MESSAGES_PER_5MIN = 30
SEARCH_PARSES_PER_MIN = 20
ARR_LOOKUPS_PER_MIN = 30
RECS_REFRESH_PER_10MIN = 2
MEMORY_FLUSHES_PER_MIN = 20
MUSIC_STARTS_PER_10MIN = 2
SYNC_TRIGGERS_PER_10MIN = 3


def enforce(scope: str, key: Hashable, limit: int, window_s: float,
            detail: Optional[str] = None) -> None:
    """Count one hit for (scope, key); raise 429 when more than ``limit``
    landed inside the last ``window_s`` seconds."""
    now = time.time()
    k = (scope, str(key))
    try:
        with _LOCK:
            hits = [t for t in _BUCKETS.get(k, ()) if now - t < window_s]
            if len(hits) >= limit:
                _BUCKETS[k] = hits
                retry = max(1, int(window_s - (now - hits[0])) + 1)
                raise HTTPException(
                    status_code=429,
                    detail=detail or f"Too many {scope} requests — try again in {retry}s",
                    headers={"Retry-After": str(retry)},
                )
            hits.append(now)
            _BUCKETS[k] = hits
            if len(_BUCKETS) > _MAX_KEYS:
                for stale in [kk for kk, v in _BUCKETS.items() if not v or now - v[-1] > window_s]:
                    _BUCKETS.pop(stale, None)
    except HTTPException:
        raise
    except Exception:
        return   # fail open


def remaining(scope: str, key: Hashable, limit: int, window_s: float) -> int:
    """How many hits (scope, key) has left in the current window — for tests
    and status lines; never raises."""
    now = time.time()
    hits = [t for t in _BUCKETS.get((scope, str(key)), ()) if now - t < window_s]
    return max(0, limit - len(hits))


def reset(scope: Optional[str] = None) -> None:
    """Forget every bucket (or one scope) — tests and admin resets."""
    with _LOCK:
        if scope is None:
            _BUCKETS.clear()
        else:
            for k in [k for k in _BUCKETS if k[0] == scope]:
                _BUCKETS.pop(k, None)


class InFlight:
    """One long-running job per key at a time. ``try_enter`` returns False
    while the key is busy; always ``leave`` in a ``finally``.

    Entries expire after ``ttl_s`` on their own: a streaming generator that
    is never iterated (client gone before the first chunk) would otherwise
    hold the key for ever and lock that user out with 409s until a restart."""

    def __init__(self, name: str, ttl_s: float = 900.0):
        self.name = name
        self.ttl_s = ttl_s
        self._active: dict[str, float] = {}
        self._lock = threading.Lock()

    def try_enter(self, key: Hashable) -> bool:
        with self._lock:
            k = str(key)
            now = time.time()
            if k in self._active and now - self._active[k] < self.ttl_s:
                return False
            self._active[k] = now
            return True

    def leave(self, key: Hashable) -> None:
        with self._lock:
            self._active.pop(str(key), None)

    def busy(self, key: Hashable) -> bool:
        ts = self._active.get(str(key))
        return ts is not None and time.time() - ts < self.ttl_s

    def enter_or_409(self, key: Hashable, detail: Optional[str] = None) -> None:
        if not self.try_enter(key):
            raise HTTPException(
                status_code=409,
                detail=detail or f"{self.name} is already running for you — wait for it to finish")


# Shared guards (module singletons; one process, one loop).
CHAT_IN_FLIGHT = InFlight("A chat reply")
RECS_REFRESH_IN_FLIGHT = InFlight("A recommendation refresh")
