"""A cancelled custodian queue stops before its next task, even when a
runner swallowed the cancel.

2026-10-07: a reload cancelled the ARR prefetch; _run_enrichment caught the
CancelledError and returned, and the model queue started memory catch-up,
the enrichment cycle and significance after "Curatarr shutting down".
uvicorn's reloader waits for the old worker without a limit on Windows, so
the app stayed down for three minutes, until the worker was killed.

    python tests/test_custodian_shutdown.py
"""
import asyncio
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import src.services.app_state as app_state  # noqa: E402
import src.services.data_custodian as dc  # noqa: E402
import src.services.scheduler as sched  # noqa: E402

state: dict = {}
app_state.get_state = lambda k: state.get(k)
app_state.set_state = lambda k, v: state.__setitem__(k, v)
sched._record_job_run = lambda job_id: None
sched._job_overdue = lambda job_id, cadence_h: True
dc._gaming = lambda: False


def _tasks(*runners):
    return [dc.Task(f"t{i}", f"T{i}", 24.0, r, needs_llm=True, reports_own=True)
            for i, r in enumerate(runners)]


def test_a_swallowed_cancel_still_stops_the_queue():
    ran = []

    async def swallow():
        ran.append("first")
        asyncio.current_task().cancel()          # the shutdown reaching the queue
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass                                 # what _run_enrichment does
        return True

    async def second():
        ran.append("second")
        return True

    dc._queue_busy[dc.QUEUE_MODEL] = True
    try:
        asyncio.run(dc._run_queue(dc.QUEUE_MODEL, _tasks(swallow, second), force=True, deep=False))
        raise AssertionError("expected the queue to stop with CancelledError")
    except asyncio.CancelledError:
        pass
    assert ran == ["first"], ran
    assert dc._queue_busy[dc.QUEUE_MODEL] is False, "the queue is free for the next start"


def test_without_a_cancel_the_queue_runs_on():
    ran = []

    async def one():
        ran.append(1)
        return True

    async def two():
        ran.append(2)
        return True

    dc._queue_busy[dc.QUEUE_MODEL] = True
    asyncio.run(dc._run_queue(dc.QUEUE_MODEL, _tasks(one, two), force=True, deep=False))
    assert ran == [1, 2]


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
