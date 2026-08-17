"""Activity visibility: every background job must surface in the Activity
view (task_monitor). Owner complaint 2026-08-17: "oft tut der curatarr dinge,
welche nicht in der activity spalte angezeigt werden".

Guards the fix on three layers:
1. task_monitor semantics the custodian wrapper relies on (stable-task_id
   replacement, name+category dedup).
2. The custodian registry invariant: a runner either carries reports_own
   (its inner code creates cards) or gets the tick's wrapper card — there
   is no third, invisible state.
3. Wiring asserts for the previously-invisible paths: facet backfill,
   rec-cache refresh, chat-memory extraction + post-chat curator calls,
   manual deletion analysis, frontend icons.

    python tests/test_activity_visibility.py
"""
import sys
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


# ── 1. task_monitor semantics the wrapper depends on ─────────────────────────

from src.services.task_monitor import TaskMonitor, TaskStatus

tm = TaskMonitor()
a = tm.create(name="Facet index backfill", category="custodian",
              task_id="cust-facet_backfill")
tm.start(a)
tm.update(a, processed=500, total=3000, message="page 1")
tm.done(a, "Partial — continues next tick")
b = tm.create(name="Facet index backfill", category="custodian",
              task_id="cust-facet_backfill")
check("stable task_id REPLACES the previous run's card (no per-tick stacking)",
      b is not a and len([t for t in tm.get_all()
                          if t["name"] == "Facet index backfill"]) == 1)

c1 = tm.create(name="Chat memory extraction", category="memory",
               task_id="memx-1-general")
tm.start(c1)
c2 = tm.create(name="Chat memory extraction", category="memory",
               task_id="memx-1-deletion_proposal:9")
check("explicit task_id bypasses name-dedup: two threads extract in parallel "
      "on two cards", c2 is not c1)

tm.error(c1, "boom")
check("error path lands as ERROR with message",
      c1.status == TaskStatus.ERROR and c1.error == "boom")

# ── 2. custodian registry invariant ──────────────────────────────────────────

from src.services.data_custodian import _registry, Task as CustodianTask

check("Task dataclass carries reports_own + takes_task",
      hasattr(CustodianTask("x", "x", 1.0, None), "reports_own")
      and hasattr(CustodianTask("x", "x", 1.0, None), "takes_task"))

reg = _registry()
check("registry loads", len(reg) >= 20)

# Runners whose inner code creates its own Activity cards. Anything NOT in
# this set gets the tick's wrapper card — extend this list ONLY when the new
# runner really cards itself (grep it for task_monitor.create first).
EXPECTED_SELF_REPORTING = {
    "db_backup", "plex_sync", "arr_sync", "arr_pre_enrich", "memory_catchup",
    "custodian_enrich", "custodian_taste", "custodian_recs",
    "memory_decay", "orphan_check", "db_vacuum",
}
actual_self = {t.job_id for t in reg if t.reports_own}
check("reports_own set matches the audited self-reporting runners "
      f"(diff: {actual_self ^ EXPECTED_SELF_REPORTING or '{}'})",
      actual_self == EXPECTED_SELF_REPORTING)

check("registry job_ids are unique (stable card ids depend on it)",
      len({t.job_id for t in reg}) == len(reg))
check("facet backfill receives the wrapper card for inner progress",
      next(t for t in reg if t.job_id == "facet_backfill").takes_task
      and not next(t for t in reg if t.job_id == "facet_backfill").reports_own)
check("no runner is both self-reporting AND wrapper-progress-fed",
      not any(t.reports_own and t.takes_task for t in reg))

root = Path(__file__).resolve().parents[1]
dc = (root / "src/services/data_custodian.py").read_text(encoding="utf-8")
check("tick wraps runners in a custodian card with stable id",
      'task_monitor.create(name=t.label, category="custodian"' in dc
      and 'task_id=f"cust-{t.job_id}"' in dc)
check("tick closes the card on done AND partial",
      '"Partial — continues next tick"' in dc)
check("tick surfaces runner failures on the card",
      "task_monitor.error(mon, str(e))" in dc)
check("facet runner passes the card through",
      "run_facet_backfill(task=task)" in dc)

# ── 3. wiring: the previously-invisible paths ────────────────────────────────

fi = (root / "src/services/facet_index.py").read_text(encoding="utf-8")
check("facet backfill reports pages via task_monitor.update (dead "
      "task.message attr gone)",
      "task_monitor.update(task, processed=offset - start_offset" in fi
      and "task.message =" not in fi)
check("facet progress is tick-relative (resumed cursor must not fake the rate)",
      "start_offset = offset" in fi and "titles indexed overall" in fi)

sch = (root / "src/services/scheduler.py").read_text(encoding="utf-8")
check("rec-cache refresh creates a per-user Activity card",
      "Recommendations refresh: {uname}" in sch
      and 'task_id=f"recs-cache-{user_id}"' in sch)
check("rec-cache card reports lane progress and closes",
      "curator is generating…" in sch
      and 'task_monitor.done(task, f"{total} recommendations cached")' in sch)

em = (root / "src/services/episodic_memory.py").read_text(encoding="utf-8")
check("chat memory extraction is carded, work-gated, per-thread id",
      '"Chat memory extraction"' in em and 'task_id=f"memx-{user_id}-{thread_id}"' in em)
check("post-chat curator calls (principles, rec feedback) run under _carded",
      '"Principle capture (deletion debate)", f"princ-{thread_id}"' in em
      and '"Recommendation feedback analysis", f"recfb-{thread_id}"' in em)

rr = (root / "src/routers/recommendations.py").read_text(encoding="utf-8")
check("manual deletion analysis creates a curation card",
      'name=f"Deletion analysis: {category or ' in rr)
check("manual analysis feeds the card into generate_deletion_proposals "
      "(phase/per-pitch progress)", rr.count("monitor_task=mtask") == 2)

fe = (root / "frontend/index.html").read_text(encoding="utf-8")
for cat in ("custodian", "curation", "recs", "memory", "music", "maintenance"):
    check(f"frontend has an icon for category '{cat}'", f"{cat}: '" in fe)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
