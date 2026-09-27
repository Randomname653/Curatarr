"""The custodian's two queues: model work never holds up the rest.

2026-09-27: one tick ran every task in one line. The scheduled deletion scan
held the GPU from 09:13 until the app was closed at 10:56, every tick in
between was skipped ("previous tick still running"), and the editions walk,
which needs no model at all, never ran. Now tasks that drive a model run in
the model queue, everything else in the background queue, side by side.

Pins: the queue of every task on the real registry; a long model task does
not stop the background queue, tick after tick; two ticks at once start each
queue once; the cross-queue order (Task.after) waits in the model queue and
defers in the background queue; the merged report; the Plex sync's model
half moving to plex_followup; the wiring (interval job, run-now, UI).

    python tests/test_custodian_queues.py
"""
import asyncio
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import src.services.app_state as app_state  # noqa: E402
import src.services.data_custodian as dc  # noqa: E402
import src.services.plex_sync as ps  # noqa: E402
import src.services.scheduler as sched  # noqa: E402
import src.services.taste_engine as te  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── 1. the real registry ────────────────────────────────────────────────────
real = dc._registry()
by_id = {t.job_id: t for t in real}
q = {t.job_id: dc.queue_of(t) for t in real}
check("every task that drives a model is in the model queue",
      all(q[t.job_id] == dc.QUEUE_MODEL for t in real if t.needs_llm))
check("syncs, walkers, top-ups and backups run in the background queue",
      all(q[j] == dc.QUEUE_BACKGROUND for j in
          ("plex_sync", "editions_sync", "lyrics_sync", "custodian_omdb", "custodian_wikidata",
           "music_pipeline", "raw_refresh", "db_backup", "db_vacuum")))
check("the ARR prefetch runs the summariser, so it is model work now",
      q["arr_pre_enrich"] == dc.QUEUE_MODEL and by_id["arr_pre_enrich"].llm_role == "summarizer")
order = [t.job_id for t in real]
check("plex_followup is model work right after the sync and before the deletion scan",
      q["plex_followup"] == dc.QUEUE_MODEL
      and order.index("plex_sync") < order.index("plex_followup") < order.index("arr_sync"))
check("the deletion scan and the follow-up wait for the Plex sync",
      "plex_sync" in by_id["arr_sync"].after and "plex_sync" in by_id["plex_followup"].after)
check("the playlist pushes follow the recommendation refresh",
      "custodian_recs" in by_id["plex_rec_playlist"].after
      and "custodian_recs" in by_id["plex_music_playlist"].after)
check("every 'after' names a task in the other queue",
      all(dep in by_id and q[dep] != q[t.job_id] for t in real for dep in t.after))

# ── fakes for the scenarios ─────────────────────────────────────────────────
state: dict = {}
app_state.get_state = lambda k: state.get(k)
app_state.set_state = lambda k, v: state.__setitem__(k, v)
recorded: list = []
sched._record_job_run = lambda job_id: recorded.append(job_id)
sched._job_overdue = lambda job_id, cadence_h: True
dc._gaming = lambda: False
dc._AFTER_POLL_S = 0.01
ran: list = []
gates: dict = {}


def runner(name, gate=None):
    async def _run():
        ran.append(f"{name}:start")
        if gate:
            await gates[gate].wait()
        ran.append(f"{name}:end")
        return True
    return _run


def reset(reg):
    ran.clear()
    recorded.clear()
    state.clear()
    dc._registry = lambda: reg


# ── 2. a long model task does not stop the background queue ────────────────
reg = [dc.Task("gpu_long", "GPU long", 24.0, runner("gpu_long", "gpu"), needs_llm=True, reports_own=True),
       dc.Task("bg_quick", "Background quick", 24.0, runner("bg_quick"), reports_own=True)]


async def starvation():
    gates["gpu"] = asyncio.Event()
    reset(reg)
    r1 = await dc.custodian_tick(wait=False)
    await asyncio.sleep(0.05)
    after_1 = list(ran)
    status_mid = dc.custodian_status()
    r2 = await dc.custodian_tick(wait=False)        # "30 minutes later"
    await asyncio.sleep(0.05)
    after_2 = list(ran)
    gates["gpu"].set()
    await asyncio.sleep(0.05)
    return r1, r2, after_1, after_2, status_mid


r1, r2, after_1, after_2, status_mid = asyncio.run(starvation())
check("first tick starts both queues", sorted(r1["started"]) == ["background", "model"])
check("the background task finishes while the model task still runs",
      "bg_quick:end" in after_1 and "gpu_long:start" in after_1 and "gpu_long:end" not in after_1)
check("the status names what the model queue is on",
      status_mid["queues"]["model"] == {"busy": True, "current": "gpu_long"}
      and status_mid["queues"]["background"]["busy"] is False and status_mid["ticking"] is True)
check("the next tick runs the background queue again and leaves the busy one alone",
      r2 == {"started": ["background"], "busy": ["model"]} and after_2.count("bg_quick:end") == 2)
check("once the model task ends both queues are idle",
      dc.queues_busy() == {"background": False, "model": False} and "gpu_long:end" in ran)
merged = __import__("json").loads(state[dc._REPORT_KEY])
check("the merged report carries both queues' latest runs",
      set(merged["queues"]) == {"background", "model"}
      and {a["task"] for a in merged["actions"]} == {"bg_quick", "gpu_long"})

# ── 3. two ticks at once start each queue once ─────────────────────────────
reg3 = [dc.Task("gpu_a", "GPU a", 24.0, runner("gpu_a"), needs_llm=True, reports_own=True),
        dc.Task("bg_a", "Background a", 24.0, runner("bg_a"), reports_own=True)]


async def double():
    reset(reg3)
    a, b = await asyncio.gather(dc.custodian_tick(wait=False), dc.custodian_tick(wait=False))
    await asyncio.sleep(0.05)
    return a, b


a, b = asyncio.run(double())
check("the second of two simultaneous ticks finds both queues claimed",
      sorted(a["started"]) == ["background", "model"] and b == {"skipped": "busy"}
      and ran.count("gpu_a:end") == 1 and ran.count("bg_a:end") == 1)

# ── 4. cross-queue order: the model queue waits, the background queue defers ─
reg4 = [dc.Task("bg_sync", "Sync", 24.0, runner("bg_sync", "sync"), reports_own=True),
        dc.Task("gpu_after", "After sync", 24.0, runner("gpu_after"), needs_llm=True,
                reports_own=True, after=("bg_sync",))]


async def wait_case():
    gates["sync"] = asyncio.Event()
    reset(reg4)
    await dc.custodian_tick(wait=False)
    await asyncio.sleep(0.05)
    before = list(ran)
    gates["sync"].set()
    await asyncio.sleep(0.1)
    return before


before = asyncio.run(wait_case())
check("a model task waits while the background task it needs is still running",
      "gpu_after:start" not in before and "bg_sync:start" in before)
check("...and runs right after it", ran.index("bg_sync:end") < ran.index("gpu_after:start"))

reg5 = [dc.Task("gpu_recs", "Recs", 24.0, runner("gpu_recs", "recs"), needs_llm=True, reports_own=True),
        dc.Task("bg_push", "Push", 24.0, runner("bg_push"), reports_own=True, after=("gpu_recs",))]


async def defer_case():
    gates["recs"] = asyncio.Event()
    reset(reg5)
    await dc.custodian_tick(wait=False)
    await asyncio.sleep(0.05)
    snap = (list(ran), list(recorded), __import__("json").loads(state[f"{dc._REPORT_KEY}:background"]))
    gates["recs"].set()
    await asyncio.sleep(0.05)
    return snap


ran5, rec5, bg_report = asyncio.run(defer_case())
check("a background task never waits on model work: it defers and stays due",
      "bg_push:start" not in ran5 and "bg_push" not in rec5
      and bg_report["actions"] == [{"task": "bg_push", "result": "deferred (after gpu_recs)"}])

# ── 5. wait=True returns the merged report (run-now path, tests) ────────────
reg6 = [dc.Task("gpu_w", "GPU w", 24.0, runner("gpu_w"), needs_llm=True, reports_own=True),
        dc.Task("bg_w", "Background w", 24.0, runner("bg_w"), reports_own=True)]
reset(reg6)
rep = asyncio.run(dc.custodian_tick(force=True))
check("wait=True merges both queues' actions",
      {x["task"] for x in rep["actions"]} == {"gpu_w", "bg_w"} and rep["forced"] is True)

# ── 6. the Plex sync's model half moves to plex_followup ────────────────────
calls: dict = {"sync_kw": None, "chain": 0, "taste": [], "recs": []}


async def fake_sync(**kw):
    calls["sync_kw"] = kw
    return {"synced": 3, "skipped": 0}


async def fake_tech(**kw):
    return {"skipped": True}


async def fake_chain(user_id=None):
    calls["chain"] += 1
    calls["recs"].append(user_id)


async def fake_taste(uid):
    calls["taste"].append(uid)


ps.sync_plex_history = fake_sync
ps.sync_tech_profiles = fake_tech
sched._recompute_and_cache_recs = fake_chain
te.compute_all_taste_vectors = fake_taste
sched._gaming = lambda: False
state.clear()
ok = asyncio.run(dc._run_plex_sync())
check("the custodian's Plex sync skips the inline taste run and chains nothing",
      ok is True and calls["sync_kw"] == {"force": False, "recompute_taste": False}
      and calls["chain"] == 0)
check("...and leaves the flag for the model queue", state.get(dc._FOLLOWUP_FLAG) == "1")

calls["sync_kw"] = None
asyncio.run(sched.job_plex_sync())
check("a direct job_plex_sync still chains inline as before",
      calls["sync_kw"] == {"force": False} and calls["chain"] == 1)

calls.update(chain=0, taste=[], recs=[])
dc._admin_id = lambda: 1
dc._active_user_ids = lambda: [1, 2]
state[dc._FOLLOWUP_FLAG] = "1"
sched._gaming = lambda: True
check("the follow-up stays due while a game holds the card",
      asyncio.run(dc._run_plex_followup()) is False and state[dc._FOLLOWUP_FLAG] == "1"
      and calls["chain"] == 0)
sched._gaming = lambda: False
check("the follow-up runs taste for the other users and the admin's full chain once",
      asyncio.run(dc._run_plex_followup()) is True and calls["taste"] == [2]
      and calls["recs"] == [1] and state[dc._FOLLOWUP_FLAG] == "0")
calls.update(chain=0, taste=[], recs=[])
check("without the flag it is a no-op",
      asyncio.run(dc._run_plex_followup()) is True and calls["chain"] == 0 and calls["taste"] == [])

# ── 7. wiring ────────────────────────────────────────────────────────────────
sch = (_ROOT / "src/services/scheduler.py").read_text(encoding="utf-8")
check("the interval job starts the queues and returns",
      "custodian_tick_background,\n        IntervalTrigger(minutes=30)" in sch
      and "custodian_tick(first_tick=True, wait=False)" in sch)
enr = (_ROOT / "src/routers/enrichment.py").read_text(encoding="utf-8")
check("run-now refuses only when both queues are busy and never waits",
      "if all(busy.values()):" in enr and "wait=False)" in enr and "_tick_lock" not in enr)
kb = (_ROOT / "frontend/js/kb.js").read_text(encoding="utf-8")
check("the Knowledge Base names what each queue is on",
      "c.queues" in kb and "'GPU work'" in kb and "allBusy ? ' disabled'" in kb)
check("the old single tick lock is gone",
      "_tick_lock" not in (_ROOT / "src/services/data_custodian.py").read_text(encoding="utf-8"))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
