"""Functional tests for the bulk-delete path (Block 1).

Runs _delete_one_and_log and _run_bulk_delete_bg against a throwaway
in-memory SQLite DB with the arr/probe/LLM boundaries monkeypatched —
no network, no Ollama, no real data touched.

    python tests/test_bulk_delete.py
"""
import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.routers.recommendations as recs
import src.services.episodic_memory as em
from src.database.models import Base, CuratorResolutionLog, DeletionProposal

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── throwaway DB + patched boundaries ────────────────────────────────────────

engine = create_engine("sqlite://",
                       connect_args={"check_same_thread": False})
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
_shared = Session()   # single session so in-memory state is visible everywhere


@contextmanager
def fake_db_session():
    yield _shared


probe_calls = []


async def fake_probe(service):
    probe_calls.append(service)
    return service != "sonarr"          # sonarr plays unreachable


async def fake_execute(p):
    return p.title != "FailMe"          # one title fails at the arr


def fake_stance(db, user_id, pid, fallback_pitch=None):
    # the real one is a plain sync DB read (no LLM) — keep the fake sync too
    return (fallback_pitch or "stance", "CONFIRMED")


analyzed = []


async def fake_analyze(user_id, title, comment, media_category="show"):
    analyzed.append((title, comment))
    return False


recs.get_db_session = fake_db_session
recs._probe_arr = fake_probe
recs._execute_arr_delete = fake_execute
recs._latest_curator_stance_for_proposal = fake_stance
em.analyze_deletion_comment = fake_analyze   # runner imports it lazily

# ── seed proposals ───────────────────────────────────────────────────────────

for i, (title, svc, mb) in enumerate([
    ("Alpha", "radarr", 2048), ("Beta", "radarr", 1024),
    ("Gamma", "sonarr", 4096), ("FailMe", "radarr", 512),
], start=1):
    _shared.add(DeletionProposal(id=i, user_id=1, title=title, service=svc,
                                 media_id=i, reason="pitch", confidence=0.8,
                                 storage_mb=mb, status="pending",
                                 category="movie" if svc == "radarr" else "show"))
_shared.commit()

# ── single-item helper ───────────────────────────────────────────────────────

p1 = _shared.query(DeletionProposal).get(1)
ok = asyncio.run(recs._delete_one_and_log(_shared, 1, p1))
_shared.commit()
check("helper deletes + sets status", ok and p1.status == "deleted"
      and p1.resolved_at is not None)
check("helper writes a resolution-log row",
      _shared.query(CuratorResolutionLog).filter_by(title="Alpha").count() == 1)

# ── bulk runner ──────────────────────────────────────────────────────────────

from src.services.task_monitor import task_monitor

probe_calls.clear()
task = task_monitor.create(name="Bulk delete (test)", category="curation", total=3)
asyncio.run(recs._run_bulk_delete_bg(task, 1, [2, 3, 4, 999], "seen it, boring"))

b = _shared.query(DeletionProposal).get(2)
g = _shared.query(DeletionProposal).get(3)
f = _shared.query(DeletionProposal).get(4)
check("reachable item deleted", b.status == "deleted")
check("unreachable service -> limbo, file untouched", g.status == "limbo")
check("arr-level failure -> error status", f.status == "error")
check("probe ran ONCE per distinct service (not per item)",
      sorted(probe_calls) == ["radarr", "sonarr"])
check("comment learned via 'Deleted: ' fast path for processed items",
      ("Beta", "Deleted: seen it, boring") in analyzed
      and all(c.startswith("Deleted: ") for _, c in analyzed))
check("limbo item skipped comment learning",
      all(t != "Gamma" for t, _ in analyzed))
check("resolution log written for bulk-deleted item",
      _shared.query(CuratorResolutionLog).filter_by(title="Beta").count() == 1)
check("no resolution log for failed/limbo items",
      _shared.query(CuratorResolutionLog).filter(
          CuratorResolutionLog.title.in_(["Gamma", "FailMe"])).count() == 0)
check("unknown id ignored without crash", True)
check("run guard reset after run", recs._bulk_delete_running is False)
check("task finished with summary log",
      task.status.value == "done"
      and "1 deleted" in task.logs[-1].message
      and "1 limbo" in task.logs[-1].message)

# ── wiring asserts ───────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
src = (root / "src/routers/recommendations.py").read_text(encoding="utf-8")
check("endpoint registered admin-only",
      '@router.post("/deletions/bulk-approve")' in src
      and src.split('bulk-approve')[1].split("def _run_bulk")[0].count("require_admin") == 1)
check("single approve uses the shared helper",
      src.count("_delete_one_and_log(") >= 2)

html = (root / "frontend/index.html").read_text(encoding="utf-8")
for frag in ["del-cb", "del-bulk-btn", "del-select-all",
             "showBulkDeleteConfirmModal", "updateDelBulkCount", "bulkDelete()"]:
    check(f"frontend has {frag}", frag in html)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
