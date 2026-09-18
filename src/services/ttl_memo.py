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

Deliberately in-process and tiny, no persistence. A payload at most TTL
seconds stale on a view that polls every 10 is invisible; anything that
needs live progress (the Activity stream) uses SSE, not these endpoints.
The one escape hatch is ``endpoint.invalidate()`` — for a write that must
be visible on the very next poll (a process the user just classified must
not keep prompting for another TTL).
"""

import asyncio
import functools
import time


def ttl_response(seconds: float, key=None, *, shared: bool = False):
    """Decorate an async endpoint: cache its return value for ``seconds``.

    ``key(**kwargs) -> hashable`` scopes the cache — per user id, per query
    flag, whatever the answer depends on. Exceptions are never cached.
    Place UNDER the router decorator so FastAPI wraps the memoized callable.

    One cache entry for everybody has to be asked for by name
    (``shared=True``). The 2026-09-14 scan filed this as a critical
    cross-user leak; it was not one, because every global user today returns
    library- or host-wide numbers and the one per-user endpoint was already
    keyed by ``user.id``. But a silent default is the wrong shape for a
    multi-user app: a future per-user endpoint would have inherited one
    shared body without anyone typing a word about it. Now the call site
    has to say which it is, and a reviewer sees it in the diff.
    """
    if key is None and not shared:
        raise ValueError(
            "ttl_response needs key=… (per-user or per-argument) or shared=True "
            "for a response that is identical for every caller")
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
        wrapped.invalidate = cache.clear    # drop every key; next call recomputes
        return wrapped

    return deco
