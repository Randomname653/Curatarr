"""The backfill walker processes titles with a few in flight at once.

    python tests/test_backfill_pool.py

Offline — the top-ups are replaced with stubs, nothing is fetched or written.

Why this exists
---------------
Measured on the significance walk: 0.6s of Wikipedia and 2.0s of model per
title, strictly alternating. The card idled through every fetch and the network
through every distillation, so overlapping them buys back roughly the 23% an
offline Wikipedia copy would — without the copy.

Concurrency is where a walker quietly starts doing a title twice, or skipping
one, or ignoring a stop. None of that shows up as an error; it shows up as a
number being slightly wrong months later. So the semantics are pinned here.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


from src.services import archive_backfill as ab       # noqa: E402
from src.services import media_enricher as me         # noqa: E402


class FakeCache:
    def close(self):
        pass


def _titles(n):
    return [{"title": f"T{i}", "media_type": "movie", "tmdb_id": i,
             "tvdb_id": None, "imdb_id": f"tt{i}", "year": 2000}
            for i in range(n)]


def _run(n, *, width, delay=0.0, fail_on=(), stop_after=None):
    """Walk n fake titles, recording what the stub was asked to do."""
    calls, peak, live = [], [0], [0]

    async def stub(title, media_type, **kw):
        live[0] += 1
        peak[0] = max(peak[0], live[0])
        try:
            calls.append(title)
            if delay:
                await asyncio.sleep(delay)
            if title in fail_on:
                raise RuntimeError("stub failure")
            return True
        finally:
            live[0] -= 1

    real_pending, real_topup = ab.pending, me.topup_significance
    ab.pending = lambda source, cache=None: _titles(n)
    me.topup_significance = stub
    stop = {"n": 0}

    def should_stop():
        if stop_after is None:
            return False
        stop["n"] += 1
        return len(calls) >= stop_after

    try:
        return asyncio.run(ab.run_source(
            "significance", cache=FakeCache(), width=width,
            should_stop=should_stop if stop_after is not None else None,
        )), calls, peak[0]
    finally:
        ab.pending, me.topup_significance = real_pending, real_topup


# ── every title, exactly once ───────────────────────────────────────────────

res, calls, peak = _run(50, width=2)
check("every title is visited", sorted(calls) == sorted(t["title"] for t in _titles(50)))
check("...exactly once — no title is walked twice", len(calls) == len(set(calls)) == 50)
check("the report counts what was actually walked",
      res["visited"] == 50 and res["added"] == 50)

# ── width really is a width ─────────────────────────────────────────────────

_, _, peak1 = _run(12, width=1, delay=0.02)
check("width=1 keeps exactly one title in flight", peak1 == 1)

_, _, peak3 = _run(12, width=3, delay=0.02)
check("width=3 overlaps three", peak3 == 3)

check("the default is small on purpose — Ollama serialises one model on one "
      f"card (WIDTH={ab.WIDTH})", 1 < ab.WIDTH <= 4)

# ── and it is actually faster ───────────────────────────────────────────────

t0 = time.monotonic()
_run(12, width=1, delay=0.05)
serial = time.monotonic() - t0
t0 = time.monotonic()
_run(12, width=3, delay=0.05)
overlapped = time.monotonic() - t0
check(f"overlapping is measurably faster ({serial:.2f}s -> {overlapped:.2f}s)",
      overlapped < serial * 0.7)

# ── failure of one title is not failure of the walk ─────────────────────────

res, calls, _ = _run(20, width=2, fail_on={"T3", "T11"})
check("a title that raises does not stop the others", len(calls) == 20)
check("...and is not counted as added", res["added"] == 18)
check("...while still counting as visited", res["visited"] == 20)

# ── stopping ────────────────────────────────────────────────────────────────

res, calls, _ = _run(200, width=2, stop_after=20)
check("a stop request is honoured long before the end", len(calls) < 60)
check("...and what was done is still reported", res["visited"] == len(calls))

# ── an unknown source is refused before any work ────────────────────────────

out = asyncio.run(ab.run_source("nonsense", cache=FakeCache()))
check("an unknown source is an error, not a silent empty walk",
      out.get("error") and "nonsense" in out["error"])

# ── the source dispatch still covers every source ───────────────────────────
# The old loop had the dispatch inline; a source added to SOURCES without a
# branch here would walk zero titles and report success.

_src = (Path(__file__).resolve().parents[1]
        / "src/services/archive_backfill.py").read_text(encoding="utf-8")
for key in ab.SOURCES:
    check(f"{key} is dispatched", f'"{key}"' in _src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
