"""A short-TTL response memo for status endpoints the frontend polls.

Second telemetry round, Maintenance view open: the status quartet
(enrichment /status, /overview, /custodian, /backfill-status) is polled
every ~10 seconds and each poll recomputed its full-table aggregates from
scratch — 6-8 seconds of DB work per cycle, a near-100% duty cycle that
then competes with every real job (a running deletion pass doubled all of
it). Numbers that change at walker pace were being recomputed at poll
pace.

The memo caches the ENDPOINT'S return value for a few seconds with
single-flight: concurrent polls during a recompute share one execution
instead of stampeding. FastAPI still resolves dependencies on every call,
so auth is enforced exactly as before — only the body is skipped.

Deliberately in-process and tiny: no invalidation API, no persistence.
A payload at most TTL seconds stale on a view that polls every 10 is
invisible; anything that needs live progress (the Activity stream) uses
SSE, not these endpoints.
"""

import asyncio
import functools
import time


def ttl_response(seconds: float, key=None):
    """Decorate an async endpoint: cache its return value for ``seconds``.

    ``key(**kwargs) -> hashable`` scopes the cache (e.g. per-user id, or a
    query flag); default is one global entry. Exceptions are never cached.
    Place UNDER the router decorator so FastAPI wraps the memoized callable.
    """
    cache: dict = {}
    locks: dict = {}

    def deco(fn):
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            k = key(**kwargs) if key else ()
            ent = cache.get(k)
            now = time.monotonic()
            if ent and now - ent[0] < seconds:
                return ent[1]
            lock = locks.setdefault(k, asyncio.Lock())
            async with lock:
                ent = cache.get(k)
                if ent and time.monotonic() - ent[0] < seconds:
                    return ent[1]
                value = await fn(*args, **kwargs)
                cache[k] = (time.monotonic(), value)
                return value

        wrapped._ttl_seconds = seconds      # introspectable for tests
        return wrapped

    return deco
