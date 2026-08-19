"""One deletion run at a time.

Live failure 2026-08-18: the owner clicked Analyze while the custodian's
ARR scan was already judging — two full deletion runs interleaved
call-by-call on the LLM gate (double wall clock), and the later batch
superseded the earlier one's freshly written proposals. Both entry
points now share the "deletion_run" app_state mutex (same pattern as
enrichment_running / music_pipeline_running).

    python tests/test_deletion_run_lock.py
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


root = Path(__file__).resolve().parents[1]
rr = (root / "src/routers/recommendations.py").read_text(encoding="utf-8")
sch = (root / "src/services/scheduler.py").read_text(encoding="utf-8")
mn = (root / "src/main.py").read_text(encoding="utf-8")

check("manual Analyze acquires the mutex before generating",
      'acquire_state_lock("deletion_run")' in rr
      and "A deletion analysis is already running" in rr)
check("manual path releases on ALL exits (error, empty, saved)",
      rr.count('release_state_lock("deletion_run")') == 3)
check("scheduler scan respects the mutex and STAYS DUE when busy "
      "(return False, not a done-stamp)",
      'if not acquire_state_lock("deletion_run"):' in sch
      and "retrying next tick" in sch
      and sch.count("return False") >= 1)
check("scheduler releases on no-proposals, success AND error — error "
      "release guarded so it never clears the MANUAL run's lock",
      sch.count('release_state_lock("deletion_run")') >= 2
      and "_dr_locked = True" in sch
      and "if _dr_locked:" in sch)
check("boot + shutdown clear a crashed run's lock",
      mn.count('set_state("deletion_run", "0")') == 2)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
