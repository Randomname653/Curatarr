"""Per-user request budgets (services/rate_limit.py): sliding window with
Retry-After, per-key in-flight guard, fail-open on internal errors.

    python tests/test_rate_limit.py
"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from fastapi import HTTPException

from src.services import rate_limit as rl


def test_sliding_window_per_scope_and_key():
    rl.reset()
    for _ in range(3):
        rl.enforce("t", 1, limit=3, window_s=60)
    assert rl.remaining("t", 1, 3, 60) == 0
    try:
        rl.enforce("t", 1, limit=3, window_s=60)
        assert False, "fourth hit must be refused"
    except HTTPException as e:
        assert e.status_code == 429 and "Retry-After" in e.headers and int(e.headers["Retry-After"]) >= 1
    rl.enforce("t", 2, limit=3, window_s=60)          # another user is untouched
    rl.enforce("other", 1, limit=3, window_s=60)      # another scope is untouched
    assert rl.remaining("t", 2, 3, 60) == 2


def test_window_expiry_and_reset():
    rl.reset()
    real_time = rl.time.time
    t = [1000.0]
    rl.time.time = lambda: t[0]
    try:
        rl.enforce("w", "u", limit=1, window_s=10)
        t[0] += 5
        try:
            rl.enforce("w", "u", limit=1, window_s=10)
            assert False
        except HTTPException as e:
            assert e.headers["Retry-After"] == "6"
        t[0] += 6
        rl.enforce("w", "u", limit=1, window_s=10)    # window passed → allowed again
    finally:
        rl.time.time = real_time
    rl.reset("w")
    assert rl.remaining("w", "u", 1, 10) == 1


def test_in_flight_guard():
    g = rl.InFlight("A job")
    assert g.try_enter(7) and not g.try_enter(7) and g.busy(7)
    assert g.try_enter(8), "other keys are independent"
    g.leave(7)
    assert g.try_enter(7)
    try:
        g.enter_or_409(7)
        assert False
    except HTTPException as e:
        assert e.status_code == 409 and "already running" in e.detail
    g.leave(7)
    g.leave(7)   # idempotent
    # a never-released entry expires on its own (a stream that never started)
    short = rl.InFlight("A job", ttl_s=0.01)
    assert short.try_enter(1) and not short.try_enter(1)
    rl.time.sleep(0.02)
    assert not short.busy(1) and short.try_enter(1)


def test_fails_open_on_internal_error():
    rl.reset()
    real = rl._BUCKETS
    rl._BUCKETS = None   # any internal failure must not lock the owner out
    try:
        rl.enforce("x", 1, limit=1, window_s=1)
    finally:
        rl._BUCKETS = real


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    sys.exit(1 if fails else 0)
