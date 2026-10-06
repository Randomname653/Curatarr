"""Keep working: a sprint works through the backlog without the 30-minute
pauses and keeps the PC awake, then lets go.

2026-10-06: with the owner away the GPU sat mostly idle. The custodian
ticked every 30 minutes, the enrichment cycle stamped 24 hours of silence
after 400 of 2,300 items, and the Balanced power plan sleeps after 15
minutes without input. A sprint runs until a set time: two-minute ticks,
the deep budgets, a Windows power request. It ends at that time, on Stop,
or by itself once nothing is due and no enrichment runs.

    python tests/test_custodian_sprint.py
"""
import asyncio
import pathlib
import sys
import threading
import time
from datetime import datetime, timedelta

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import src.services.app_state as app_state  # noqa: E402
import src.services.data_custodian as dc  # noqa: E402
import src.services.scheduler as sched  # noqa: E402

state: dict = {}
app_state.get_state = lambda k: state.get(k)
app_state.set_state = lambda k, v: state.__setitem__(k, v)
app_state.force_set_state = lambda k, v, timeout_s=120: state.__setitem__(k, v) or True

real_keep_awake = dc._keep_awake
awake: list = []


def _fake_keep_awake(on):
    awake.append(on)
    dc._awake_stop = threading.Event() if on else None


dc._keep_awake = _fake_keep_awake
dc._STARTED = time.monotonic() - 10 * dc._SETTLE_SECONDS
real_tick = dc.custodian_tick
ticks: list = []


async def _fake_tick(**kw):
    ticks.append(kw)
    return {"started": []}


dc.custodian_tick = _fake_tick


def _reset():
    state.clear()
    awake.clear()
    ticks.clear()
    dc._awake_stop = None
    for q in dc._QUEUES:
        dc._queue_busy[q] = False


def test_a_sprint_starts_and_stops():
    _reset()
    until = dc.set_sprint(10)
    left = until - datetime.utcnow()
    assert timedelta(hours=9, minutes=59) < left <= timedelta(hours=10), left
    assert dc.sprint_until() == until and state[dc._SPRINT_KEY] == until.isoformat()
    assert awake == [True] and dc.sprint_status() == {"until": until.isoformat(), "awake": True}
    assert dc.set_sprint(0) is None
    assert state[dc._SPRINT_KEY] == "" and dc.sprint_until() is None
    assert awake == [True, False] and dc.sprint_status() == {"until": None, "awake": False}


def test_hours_are_capped_and_a_past_end_is_no_sprint():
    _reset()
    until = dc.set_sprint(100)
    assert until - datetime.utcnow() <= timedelta(hours=dc.SPRINT_MAX_HOURS)
    state[dc._SPRINT_KEY] = (datetime.utcnow() - timedelta(minutes=1)).isoformat()
    assert dc.sprint_until() is None
    state[dc._SPRINT_KEY] = "not a time"
    assert dc.sprint_until() is None
    assert dc.set_sprint(-3) is None


def test_the_tick_works_while_something_is_due():
    _reset()
    sched._job_overdue = lambda job_id, cadence_h: job_id == "custodian_signif"
    dc._queue_busy[dc.QUEUE_BACKGROUND] = True   # a busy background queue changes nothing
    dc.set_sprint(4)
    asyncio.run(dc.custodian_sprint_tick())
    assert ticks == [{"deep": True, "wait": False, "quiet": True,
                      "queues": (dc.QUEUE_MODEL,)}], ticks


def test_the_background_queue_keeps_its_rhythm():
    """Only model work keeps a sprint going; a background walker that is
    due (editions, lyrics, Wikidata) waits for the 30-minute tick."""
    _reset()
    sched._job_overdue = lambda job_id, cadence_h: job_id == "editions_sync"
    dc.set_sprint(4)
    assert asyncio.run(dc.custodian_sprint_tick()) == {"sprint": None, "done": True}
    assert ticks == []


def test_a_tick_can_start_one_queue():
    _reset()
    ran = []

    def runner(name):
        async def run():
            ran.append(name)
            return True
        return run

    real_registry, real_record = dc._registry, sched._record_job_run
    dc._registry = lambda: [
        dc.Task("gpu", "GPU", 24.0, runner("gpu"), needs_llm=True, reports_own=True),
        dc.Task("bg", "Background", 24.0, runner("bg"), reports_own=True)]
    sched._job_overdue = lambda job_id, cadence_h: True
    sched._record_job_run = lambda job_id: None
    try:
        asyncio.run(real_tick(wait=True, quiet=True, queues=(dc.QUEUE_MODEL,)))
        assert ran == ["gpu"], ran
    finally:
        dc._registry, sched._record_job_run = real_registry, real_record


def test_the_tick_leaves_busy_queues_and_the_settle_window_alone():
    _reset()
    sched._job_overdue = lambda job_id, cadence_h: True
    dc.set_sprint(4)
    dc._queue_busy[dc.QUEUE_MODEL] = True
    asyncio.run(dc.custodian_sprint_tick())
    assert ticks == [], "the model queue is busy: nothing to start"
    for q in dc._QUEUES:
        dc._queue_busy[q] = False
    started = dc._STARTED
    dc._STARTED = time.monotonic()
    try:
        asyncio.run(dc.custodian_sprint_tick())
        assert ticks == [], "the first tick's settle window comes first"
    finally:
        dc._STARTED = started


def test_the_sprint_ends_by_itself_once_nothing_is_left():
    _reset()
    sched._job_overdue = lambda job_id, cadence_h: False
    dc.set_sprint(8)
    state["enrichment_running"] = "1"
    asyncio.run(dc.custodian_sprint_tick())
    assert dc.sprint_until() is not None and len(ticks) == 1, "a running enrichment is work"
    state["enrichment_running"] = "0"
    r = asyncio.run(dc.custodian_sprint_tick())
    assert r == {"sprint": None, "done": True}
    assert dc.sprint_until() is None and awake[-1] is False and len(ticks) == 1


def test_after_the_sprint_the_power_request_goes():
    _reset()
    dc._awake_stop = threading.Event()      # left from a sprint whose end has passed
    state[dc._SPRINT_KEY] = (datetime.utcnow() - timedelta(seconds=5)).isoformat()
    assert asyncio.run(dc.custodian_sprint_tick()) == {"sprint": None}
    assert awake == [False] and dc._awake_stop is None and ticks == []


def test_the_enrichment_cycle_stays_due_while_items_remain():
    _reset()
    import src.routers.enrichment as enr
    calls = []

    async def fake_run(uid, cats, source, limit):
        calls.append(limit)

    real = (enr._run_enrichment, app_state.acquire_state_lock, dc._admin_id)
    enr._run_enrichment = fake_run
    app_state.acquire_state_lock = lambda key: True
    dc._admin_id = lambda: 1
    try:
        for candidates, deep, done in ((2300, False, False), (400, False, True),
                                       (2300, True, False), (1500, True, True)):
            state["enrichment_candidates"] = str(candidates)
            got = asyncio.run(dc._run_enrichment_cycle(deep=deep))
            assert got is done, (candidates, deep, got)
        assert calls == [400, 400, 2000, 2000]
    finally:
        enr._run_enrichment, app_state.acquire_state_lock, dc._admin_id = real


def test_keep_awake_holds_one_request_and_lets_go():
    dc._awake_stop = None
    real_keep_awake(True)
    if sys.platform != "win32":
        assert dc._awake_stop is None, "a no-op off Windows"
        return
    held = [t for t in threading.enumerate() if t.name == "custodian-keep-awake"]
    assert dc._awake_stop is not None and len(held) == 1 and held[0].daemon
    real_keep_awake(True)
    assert len([t for t in threading.enumerate() if t.name == "custodian-keep-awake"]) == 1
    real_keep_awake(False)
    held[0].join(timeout=5)
    assert dc._awake_stop is None and not held[0].is_alive()


def test_the_console_switch():
    _reset()
    assert dc._cli(["--sprint", "3"]) == 0
    until = datetime.fromisoformat(state[dc._SPRINT_KEY])
    assert timedelta(hours=2, minutes=59) < until - datetime.utcnow() <= timedelta(hours=3)
    assert dc._cli(["--sprint", "0"]) == 0 and state[dc._SPRINT_KEY] == ""


def test_the_wiring():
    src = (_ROOT / "src" / "services" / "scheduler.py").read_text(encoding="utf-8")
    assert "custodian_sprint_tick,\n        IntervalTrigger(minutes=SPRINT_TICK_MINUTES)" in src
    router = (_ROOT / "src" / "routers" / "enrichment.py").read_text(encoding="utf-8")
    assert '@router.post("/custodian/sprint")' in router
    cut = router.index('set_state("enrichment_candidates", str(len(items)))')
    assert cut < router.index("items = items[:limit]", cut)
    kb = (_ROOT / "frontend" / "js" / "kb.js").read_text(encoding="utf-8")
    assert "act('startSprint', EL)" in kb and "act('stopSprint', EL)" in kb
    assert "/api/enrichment/custodian/sprint?hours=" in kb
    app = (_ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    assert "\n  startSprint,\n" in app and "\n  stopSprint,\n" in app
    assert '"sprint": sprint_status()' in (_ROOT / "src" / "services" / "data_custodian.py").read_text(encoding="utf-8")


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
