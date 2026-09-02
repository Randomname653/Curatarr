"""Status numbers change at walker pace; they must not be computed at poll pace.

Second telemetry round, Maintenance view open: the status quartet was
polled every ~10s and recomputed full-table aggregates each time — 6-8s
of DB work per cycle, competing with every real job. The ttl_response
memo caches each endpoint's payload for a few seconds with single-flight;
auth dependencies still run on every request, only the body is skipped.
"""

import asyncio
import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.services.ttl_memo import ttl_response


def test_hits_within_ttl_share_one_execution():
    calls = []

    @ttl_response(60)
    async def fn(user=None):
        calls.append(1)
        return {"n": len(calls)}

    async def run():
        a = await fn(user="x")
        b = await fn(user="y")      # different caller, same global payload
        return a, b

    a, b = asyncio.run(run())
    assert a == b == {"n": 1}
    assert len(calls) == 1


def test_keys_separate_cache_entries():
    calls = []

    class U:
        def __init__(self, i): self.id = i

    @ttl_response(60, key=lambda **kw: kw["user"].id)
    async def fn(user=None):
        calls.append(user.id)
        return {"uid": user.id}

    async def run():
        assert (await fn(user=U(1)))["uid"] == 1
        assert (await fn(user=U(2)))["uid"] == 2
        assert (await fn(user=U(1)))["uid"] == 1   # cached, no third call

    asyncio.run(run())
    assert calls == [1, 2]


def test_concurrent_cold_calls_single_flight():
    calls = []

    @ttl_response(60)
    async def fn():
        calls.append(1)
        await asyncio.sleep(0.05)
        return "v"

    async def run():
        return await asyncio.gather(*(fn() for _ in range(8)))

    assert asyncio.run(run()) == ["v"] * 8
    assert len(calls) == 1, "a poll stampede must share one execution"


def test_exceptions_are_never_cached():
    calls = []

    @ttl_response(60)
    async def fn():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("plex down")
        return "ok"

    async def run():
        try:
            await fn()
        except RuntimeError:
            pass
        return await fn()

    assert asyncio.run(run()) == "ok"
    assert len(calls) == 2


def test_the_polled_endpoints_are_actually_memoized():
    def src(rel):
        return (_ROOT / "src" / rel).read_text(encoding="utf-8")

    en = src("routers/enrichment.py")
    for anchor in ('@ttl_response(10)\nasync def enrichment_overview',
                   '@ttl_response(10)\nasync def custodian_status_endpoint',
                   '@ttl_response(10, key=lambda **kw: bool(kw.get("quick")))\nasync def enrichment_status',
                   '@ttl_response(15)\nasync def backfill_status'):
        assert anchor in en, anchor
    assert ('@ttl_response(10, key=lambda **kw: kw["user"].id)\nasync def sync_status'
            in src("routers/history.py"))
    assert '@ttl_response(30)\nasync def discover_libraries' in src("routers/libraries.py")
