"""The Activity view: Queue / Scheduled / History (2026-11 redesign).

Owner report: the page was unreadable — "Done" tasks clogged "Live tasks",
the feed and the table below repeated each other, and status lines carried
raw Python: "Starting pipeline: 32 items across ['music', 'movie', …]",
"Enrichment setup: Done" (an empty name suffix), "Done · 31 / 32 · ETA 8s",
"ARR Sync Running 0%" for minutes while it judged, and four "Taste vector:
anime / show / movie / music" rows per recompute.

Pins:
1. task_monitor: the live snapshot holds only running/queued runs (plus a
   short fade window); no ETA after the end; a completed run shows its full
   count; outcomes; the no-op judgement; sub-steps fold into their run.
2. The ARR sync's step progress moves while a category is judged; a run
   with no count is indeterminate, not 0%.
3. The history persists across a restart (app_state).
4. The custodian stores each task's last result + summary, and Run now runs
   one task (at once when its queue is idle, after it otherwise).
5. The scheduled overview, the formatting helpers, the router, the wiring.

    python tests/test_activity_view.py
"""
import asyncio
import pathlib
import sys
import time

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from _harness import wait_until  # noqa: E402,I001 - first: temp stores
from _i18n_js import with_words  # noqa: E402
from _checks import check, real_state  # noqa: E402

from src.services import activity  # noqa: E402
from src.services.task_monitor import LIVE_GRACE_S, TaskMonitor, TaskStatus  # noqa: E402


# ── 1. formatting ────────────────────────────────────────────────────────────

check("a category list reads as words, never as a Python list",
      activity.fmt_list(["music", "movie", "show", "anime"]) == "music, movie, show, anime"
      and "[" not in activity.fmt_list(["music"]))
check("an empty category list says so instead of 'across []'",
      activity.fmt_list([]) == "all categories" and activity.fmt_list(None) == "all categories")
check("tags are de-duplicated, order kept",
      activity.clean_tags(["movie", "movie", " show ", ""]) == ["movie", "show"])
check("cadences in words",
      [activity.fmt_cadence(h) for h in (0.4, 1, 6, 24, 72, 168)]
      == ["every 24 min", "hourly", "every 6 h", "daily", "every 3 days", "weekly"])
check("durations", [activity.fmt_duration(s) for s in (8, 88, 3725)] == ["8s", "1m 28s", "1h 2m"])
check("areas: a custodian card takes its job's area, categories map",
      activity.area_of("custodian", "cust-db_backup") == "maintenance"
      and activity.area_of("custodian", "cust-plex_sync") == "sync"
      and activity.area_of("enrichment") == "enrichment"
      and activity.area_of("taste") == "taste"
      and activity.area_of("arr_sync") == "sync"
      and activity.area_of("something-new") == "maintenance")

# ── 2. the queue holds only live runs ───────────────────────────────────────

tm = TaskMonitor()
tm.persist = False
a = tm.create("Enrichment", "enrichment", total=32, tags=["music", "movie"])
tm.start(a)
# Backdated, not timed: time.time() moves in ~15 ms steps on Windows, so a
# start and an update in the same step measure 0 s — no rate, no ETA.
a.started_at = time.time() - 10
tm.update(a, processed=31)
d = a.to_dict()
check("a running task has an ETA, a rate and its pills",
      d["eta_s"] is not None and d["rate"] > 0 and d["tags"] == ["music", "movie"]
      and d["outcome"] == "running" and d["name"] == "Enrichment")
tm.done(a, "31 of 32 items processed")
d = a.to_dict()
check("a finished task never shows an ETA or a rate", d["eta_s"] is None and d["rate"] == 0)
check("a completed run shows its full count, not the last tick (31 / 32)",
      d["processed"] == 32 and d["total"] == 32 and d["progress"] == 100)
check("...its summary is kept", d["summary"] == "31 of 32 items processed")

q = tm.create("Queued thing", "sync")
live_now = {t["id"] for t in tm.get_live()}
check("the live snapshot: running/queued + a run that ended moments ago",
      q.id in live_now and a.id in live_now)
later = {t["id"] for t in tm.get_live(now=time.time() + LIVE_GRACE_S + 1)}
check("after the fade window a finished run is gone from the queue",
      a.id not in later and q.id in later)
check("...and is in the history", any(e["id"] == a.id for e in tm.get_history()))

# Outcomes
o = TaskMonitor(); o.persist = False
t1 = o.create("x", "custodian", task_id="cust-facet_backfill"); o.start(t1)
o.update(t1, processed=500, total=3000, message="page 1 of 6")
o.done(t1, "Partial — continues next tick")
check("a partial run is 'partial', keeps its true count, and is summarised "
      "by the last informative line", t1.outcome == "partial"
      and t1.to_dict()["processed"] == 500 and t1.summary == "page 1 of 6"
      and not t1.is_noop)
t2 = o.create("y", "sync"); o.start(t2); o.error(t2, "boom")
t3 = o.create("z", "sync"); o.start(t3); o.cancel(t3.id)
t4 = o.create("w", "sync"); o.start(t4); o.log(t4, "slow source", level="warn"); o.done(t4, "ok 3")
check("failed / cancelled / warnings",
      (t2.outcome, t3.outcome, t4.outcome) == ("failed", "cancelled", "warning"))
o.done(t3, "56/3,186 processed")      # the cooperative-cancel path ends with done()
check("a cancelled task stays cancelled when its code calls done() afterwards",
      t3.outcome == "cancelled" and "56/3,186" in t3.summary)

# No-op judgement: what "Hide empty runs" hides
def _run(msg, processed=0, total=0, secs=0.0, **kw):
    m = TaskMonitor(); m.persist = False
    t = m.create("t", "maintenance"); m.start(t)
    t.started_at -= secs
    if total or processed:
        m.update(t, processed=processed, total=total)
    m.done(t, msg, **kw)
    return t.is_noop

check("no-op: '0 expired rows removed', 'Nothing to enrich', 'Complete: 0/4 processed'",
      _run("0 expired rows removed") and _run("Nothing to enrich — all items already have profiles", secs=8)
      and _run("Complete: 0/4 processed", total=4, secs=30))
check("not a no-op: a backup's '439.2 MB · kept 7', a run that processed items, a partial",
      not _run("439.2 MB · kept 7", secs=1) and not _run("done", processed=3, total=3)
      and not _run("Partial — continues next tick", partial=True))
check("an explicit noop= wins", _run("12 things", noop=True) and not _run("", noop=False))

# Grouping: sub-steps fold into their run
g = TaskMonitor(); g.persist = False
parent = g.create("Taste vectors", "taste", total=4, tags=["anime", "show", "movie", "music"],
                  task_id="taste-run-1-1")
g.start(parent)
with g.group(parent):
    for i, cat in enumerate(["anime", "show", "movie", "music"]):
        c = g.create(f"Taste vector: {cat}", "taste", tags=[cat], task_id=f"taste-{cat}-1")
        g.start(c); g.update(c, processed=10, total=10); g.done(c, "Complete: 9 embeddings")
        g.update(parent, processed=i + 1)
check("sub-steps carry their run's id", all(t.parent_id == parent.id for t in g.children(parent.id)))
check("a sub-step that ends is not a history row of its own", g.get_history() == [])
g.done(parent, "4 of 4 profiles updated")
hist = g.get_history()
check("one history row for the run, its four steps inside",
      len(hist) == 1 and hist[0]["name"] == "Taste vectors"
      and [s["tags"][0] for s in hist[0]["steps"]] == ["anime", "show", "movie", "music"]
      and all(s["outcome"] == "completed" for s in hist[0]["steps"]))
check("a task created outside the group has no parent",
      g.create("Other", "sync").parent_id is None)
check("dedupe matches name AND pills (two enrichment runs of different categories)",
      g.create("Enrichment", "enrichment", tags=["movie"]) is not
      g.create("Enrichment", "enrichment", tags=["music"]))

# ── 3. step progress (the ARR sync's 0%) ────────────────────────────────────

s = TaskMonitor(); s.persist = False
arr = s.create("Arr sync", "arr_sync"); s.start(arr)
check("no item count yet: indeterminate, not 0%", arr.to_dict()["indeterminate"] is True)
s.update(arr, total=40)
s.begin_step(arr, 20, message="Analysing movie: 20 candidates")
before = arr.progress
s.substep(arr, 5, 10)
check("the bar moves while a category is judged (half of a 20-item step = 25%)",
      before == 0 and arr.progress == 25 and arr.to_dict()["indeterminate"] is False)
s.update(arr, processed=20)
check("the category's end counts its items and ends the step",
      arr.progress == 50 and arr.step_size == 0 and arr.sub_total == 0)
check("a substep without an item count still gives a percentage",
      (lambda t: (s.start(t), s.substep(t, 3, 4), t.progress)[-1])(s.create("Deletion analysis", "curation")) == 75)

# ── 4. persistence across a restart ─────────────────────────────────────────

state = real_state()
p1 = TaskMonitor()
r = p1.create("Daily DB backup", "maintenance"); p1.start(r); p1.done(r, "439.2 MB · kept 7")
# An hour earlier: two runs ending in the same clock step (~15 ms on Windows)
# would tie on finished_at, and "newest first" would read the insertion order.
r.started_at -= 3600
r.finished_at -= 3600
p1._history.clear()
p1._archive(r)
p1._persist_now()
p2 = TaskMonitor()           # a fresh process
check("the history survives a restart",
      [e["summary"] for e in p2.get_history()] == ["439.2 MB · kept 7"])
n = p2.create("Plex sync", "sync"); p2.start(n); p2.done(n, "12 new plays")
check("...and this process's runs join it, newest first",
      [e["name"] for e in p2.get_history()] == ["Plex sync", "Daily DB backup"])

# ── 5. the custodian: last result, Run now ──────────────────────────────────

import src.services.data_custodian as dc  # noqa: E402
import src.services.scheduler as sched  # noqa: E402
from src.services.task_monitor import task_monitor  # noqa: E402

task_monitor.persist = False
sched._record_job_run = lambda job_id: None
sched._job_overdue = lambda job_id, cadence_h: True
dc._gaming = lambda: False
gate = asyncio.Event()
ran = []


async def _prune(task=None):
    task_monitor.update(task, message="0 expired rows removed")
    return True


async def _slow(task=None):
    ran.append("slow:start")
    await gate.wait()
    ran.append("slow:end")
    return True


async def _quick(task=None):
    ran.append("quick")
    task_monitor.update(task, message="7 rows retired")
    return "7 rows retired"


reg = [dc.Task("cache_prune", "Expired cache rows", 24.0, _prune, takes_task=True),
       dc.Task("slow_job", "Slow job", 24.0, _slow, takes_task=True),
       dc.Task("retention", "Retired rows", 24.0, _quick, takes_task=True)]
dc._registry = lambda: reg


async def scenario():
    # A normal queue run stores each task's result with the card's summary.
    await dc._run_queue(dc.QUEUE_BACKGROUND, [reg[0]], False, False)
    rec = await asyncio.to_thread(dc.last_result, "cache_prune")
    ok1 = rec.get("result") == "done" and rec.get("summary") == "0 expired rows removed" \
        and rec.get("noop") is True
    # Run now on an idle queue starts at once.
    gate.clear()
    first = dc.request_run("slow_job")
    await wait_until(lambda: "slow:start" in ran, what="slow job start")
    state_running = dc.job_state("slow_job")
    again = dc.request_run("slow_job")
    # Another task of the busy queue is queued behind it …
    queued = dc.request_run("retention")
    state_queued = dc.job_state("retention")
    gate.set()
    await wait_until(lambda: "quick" in ran and not dc._queue_busy[dc.QUEUE_BACKGROUND],
                     what="queue drain")
    rec2 = await asyncio.to_thread(dc.last_result, "retention")
    return ok1, first, state_running, again, queued, state_queued, rec2


ok1, first, st_run, again, queued, st_q, rec2 = asyncio.run(scenario())
check("the custodian stores a task's last result and the card's summary", ok1)
check("Run now on an idle queue starts it", first == "started" and st_run == "running")
check("Run now on a running task says so (no double run)", again == "running")
check("Run now behind a busy queue queues it, and it runs after",
      queued == "queued" and st_q == "queued" and ran == ["slow:start", "slow:end", "quick"])
check("a runner's returned string is its summary", rec2.get("summary") == "7 rows retired")


async def first_run_after_start():
    """A restart: no regular run yet. Run now must not use up the first
    tick that a settle-only task (the VACUUM) waits for."""
    q_ = dc.QUEUE_BACKGROUND
    dc._first_run_done[q_] = False
    ran.clear()
    dc.request_run("retention")
    await wait_until(lambda: "quick" in ran and not dc._queue_busy[q_], what="run-now drain")
    after_run_now = dc._first_run_done[q_]
    await dc._run_queue(q_, [reg[2]], False, False)
    return after_run_now, dc._first_run_done[q_]


after_run_now, after_regular = asyncio.run(first_run_after_start())
check("Run now before the first regular run leaves settle-only tasks waiting",
      after_run_now is False)
check("...the first regular run is what ends the wait", after_regular is True)
try:
    dc.request_run("no_such_job")
    unknown = False
except KeyError:
    unknown = True
check("an unknown job id is refused", unknown)

# ── 6. the scheduled overview ───────────────────────────────────────────────

from datetime import datetime, timedelta  # noqa: E402
stamps = {"cache_prune": datetime.utcnow() - timedelta(hours=2)}
sched._last_job_run = lambda job_id: stamps.get(job_id)
sched._job_overdue = lambda job_id, cadence_h: job_id not in stamps
ov = activity.scheduled_overview()
jobs = {j["job_id"]: j for g_ in ov["groups"] for j in g_["jobs"]}
check("every registered custodian task is listed, grouped by area",
      set(jobs) == {"cache_prune", "slow_job", "retention"}
      and [g_["area"] for g_ in ov["groups"]] == ["maintenance"])
cp = jobs["cache_prune"]
check("a job that ran: last execution, duration, result + summary, next run, Run now",
      cp["last_run"] and cp["last_result"] == "done" and cp["last_summary"] == "0 expired rows removed"
      and cp["next_run"] and not cp["due"] and cp["can_run"] and cp["cadence"] == "daily")
check("...the next run is one cadence (× the grace) after the last",
      abs((datetime.fromisoformat(cp["next_run"]).replace(tzinfo=None)
           - (stamps["cache_prune"] + timedelta(hours=24 * 1.1))).total_seconds()) < 5)
check("a due job says so", jobs["slow_job"]["due"] and jobs["slow_job"]["next_note"].startswith("due"))
dc._backoff_due = lambda job_id, cadence_h: job_id != "slow_job"
_slow_row = next(j for g_ in activity.scheduled_overview()["groups"] for j in g_["jobs"]
                 if j["job_id"] == "slow_job")
check("a due job in backoff says why it waits",
      "backing off" in _slow_row["blocked"] and _slow_row["next_note"] == _slow_row["blocked"])

# ── 7. the router ───────────────────────────────────────────────────────────

from fastapi import HTTPException  # noqa: E402
from src.routers import tasks as tasks_router  # noqa: E402
from src.routers.auth import require_admin  # noqa: E402
import inspect  # noqa: E402

check("/scheduled and Run now are admin-only",
      all(any(getattr(p.default, "dependency", None) is require_admin
              for p in inspect.signature(fn).parameters.values())
          for fn in (tasks_router.get_scheduled, tasks_router.run_scheduled)))


async def _run_unknown():
    try:
        await tasks_router.run_scheduled("nope", _admin=None)
    except HTTPException as e:
        return e.status_code
    return 200


check("Run now of an unknown job is a 404", asyncio.run(_run_unknown()) == 404)


class _U:
    def __init__(self, uid, admin):
        self.id, self.is_admin = uid, admin


tasks_router.task_monitor = mon = TaskMonitor()
mon.persist = False
mine = mon.create("Deletion analysis", "curation", task_id="del-analysis-2"); mon.start(mine)
srv = mon.create("Plex sync", "sync", task_id="cust-plex_sync"); mon.start(srv); mon.done(srv, "3 new plays")
check("/live: the queue only; a member sees their own runs",
      [t["id"] for t in tasks_router.get_live(user=_U(2, False))["tasks"]] == ["del-analysis-2"]
      and {t["id"] for t in tasks_router.get_live(user=_U(1, True))["tasks"]}
      == {"del-analysis-2", "cust-plex_sync"})
h_admin = tasks_router.get_task_history(user=_U(1, True))
h_member = tasks_router.get_task_history(user=_U(2, False))
check("/history: the persisted runs (admin all, member own) + the old last_runs",
      [r["id"] for r in h_admin["runs"]] == ["cust-plex_sync"] and h_member["runs"] == []
      and "sync" in h_admin["last_runs"] and h_member["last_runs"] == {})

# ── 8. the strings the owner saw, and the grouped recompute ─────────────────

from src.routers.enrichment import _pipeline_start_line  # noqa: E402
check("the pipeline start line lists categories as words, never a Python list",
      _pipeline_start_line(32, ["music", "movie", "show", "anime"]).startswith(
          "Starting pipeline: 32 items across music, movie, show, anime")
      and "[]" not in _pipeline_start_line(0, []))

import src.services.taste_engine as te_mod  # noqa: E402
te_mod.task_monitor = tv = TaskMonitor()
tv.persist = False


async def _fake_vector(user_id, category=None):
    c = tv.create(f"Taste vector: {category}", "taste", tags=[category],
                  task_id=f"taste-{category}-x")
    tv.start(c)
    tv.done(c, "Complete: 0 embeddings, 0 entries")
    return {"watch_count": 0}


te_mod.compute_taste_vector_for_user = _fake_vector
asyncio.run(te_mod.compute_all_taste_vectors(1))
th = tv.get_history()
check("a recompute is ONE history row with the four categories as sub-steps",
      len(th) == 1 and th[0]["name"] == "Taste vectors"
      and [s_["tags"][0] for s_ in th[0]["steps"]] == ["music", "movie", "show", "anime"])
check("...a recompute with no taste data is a no-op run",
      th[0]["noop"] is True and th[0]["outcome"] == "completed")

# The scheduler's own jobs report their state and last result.
seen = {}


async def _job():
    seen["during"] = "w_job" in activity._job_running
    return "2 caches refreshed"

asyncio.run(sched._watched("w_job")(_job)())
check("a watched scheduler job is 'running' while it runs and keeps its result",
      seen["during"] and "w_job" not in activity._job_running
      and activity._job_results["w_job"]["summary"] == "2 caches refreshed"
      and activity._job_results["w_job"]["result"] == "done")


async def _boom():
    raise RuntimeError("down")

try:
    asyncio.run(sched._watched("game_watcher")(_boom)())
except RuntimeError:
    pass
check("...a failing one records the error (a heartbeat in memory only)",
      activity._job_results["game_watcher"]["result"] == "error"
      and "down" in activity._job_results["game_watcher"]["summary"])

# ── 9. the page (markup and modules: no runtime to call here) ───────────────

from tests.frontend_files import markup, script  # noqa: E402
html, js = markup(), script()
check("the view has the three tabs and the history filters",
      all(f'id="act-tab-{t}"' in html for t in ("queue", "scheduled", "history"))
      and all(f'data-args=\'["status","{v}"]\'' in html for v in ("all", "completed", "warn", "failed"))
      and all(f'data-args=\'["area","{v}"]\'' in html for v in ("sync", "enrichment", "taste", "maintenance"))
      and 'id="act-hide-noop" checked' in html)
check("the empty queue says all queues are idle",
      "No active tasks running — all queues idle." in with_words(js))
check("Run now posts to the scheduled endpoint, encoded",
      "`/api/tasks/scheduled/${encodeURIComponent(jobId)}/run`" in js)

check.done()
