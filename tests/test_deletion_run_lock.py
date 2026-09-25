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
# One release, in a finally: the old three hand-placed releases (error,
# empty, saved) missed any exception in the enrich / DB-write block, which
# left the lock held until restart. The functional check below proves it.
check("manual path releases in a finally (covers every exit)",
      rr.count('release_state_lock("deletion_run")') == 1
      and 'finally:\n        release_state_lock("deletion_run")' in rr)

# ── functional: an exception AFTER generation still frees the lock ──────────
import asyncio

import src.routers.recommendations as recs
import src.services.app_state as app_state
import src.services.recommendations_engine as engine

_lock = {"held": False, "released": 0}


def _acquire(name):
    if _lock["held"]:
        return False
    _lock["held"] = True
    return True


def _release(name):
    _lock["held"] = False
    _lock["released"] += 1


async def _candidates(category=None):
    return [{"title": "X", "service": "radarr", "arr_id": 1, "category": "movie"}]


async def _no_imports(svc):
    return {}


async def _gen(user_id, items, category, monitor_task=None):
    return [{"title": "X", "pitch": "p", "confidence": 0.9, "arr_id": 1}]


async def _enrich_boom(p, item_map, category):
    raise RuntimeError("TMDB exploded")


app_state.acquire_state_lock = _acquire
app_state.release_state_lock = _release
engine.generate_deletion_proposals = _gen
recs._fetch_arr_candidates = _candidates
recs._fetch_arr_recent_imports = _no_imports
recs._enrich_proposal = _enrich_boom


class _U:
    id = 1


try:
    asyncio.run(recs.get_deletion_proposals(
        category="movie", refresh=True, recent_only=False, recent_days=7,
        user=_U(), db=None))
    raised = False
except RuntimeError:
    raised = True
check("enrich-stage exception propagates", raised)
check("...and the deletion_run lock is released anyway",
      _lock["held"] is False and _lock["released"] == 1)
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
