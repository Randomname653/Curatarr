"""A member sees their own tasks and which arr services exist — not the
server's task names, not the LAN addresses (audit residual, closed 2026-09-25).

    python tests/test_member_visibility.py
"""
import asyncio
import pathlib
import sys
from types import SimpleNamespace

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import src.routers.library as lib  # noqa: E402
import src.routers.tasks as tasks  # noqa: E402
from src.services.task_monitor import task_monitor  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


ADMIN = SimpleNamespace(id=1, is_admin=True)
MEMBER = SimpleNamespace(id=2, is_admin=False)

# ── tasks ────────────────────────────────────────────────────────────────────
task_monitor.create(name="Plex sync", category="sync", task_id="cust-plex_sync")
task_monitor.create(name="Deletion analysis: anime", category="curation", task_id="del-analysis-1")
task_monitor.create(name="Deletion analysis: movies", category="curation", task_id="del-analysis-2")
task_monitor.create(name="Memory extraction", category="memory", task_id="memx-2-general")
task_monitor.create(name="Recommendations", category="recs", task_id="recs-cache-2")
task_monitor.create(name="Taste vectors", category="taste", task_id="taste-all-1")

check("owner is read from the task id",
      [tasks._owner_of(t) for t in ("del-analysis-2", "memx-2-general", "recs-cache-2",
                                    "cust-plex_sync", "taste-all-1", "")] == [2, 2, 2, None, None, None])

admin_ids = {t["id"] for t in asyncio.run(tasks.get_tasks(user=ADMIN))["tasks"]}
member_ids = {t["id"] for t in asyncio.run(tasks.get_tasks(user=MEMBER))["tasks"]}
check("admin sees every task", {"cust-plex_sync", "del-analysis-1", "del-analysis-2", "taste-all-1"} <= admin_ids)
check("member sees only tasks carrying their id",
      member_ids == {"del-analysis-2", "memx-2-general", "recs-cache-2"})
check("member never sees another user's analysis or a server task",
      not ({"del-analysis-1", "cust-plex_sync", "taste-all-1"} & member_ids))
check("running list is filtered the same way",
      all(tasks._owner_of(t["id"]) == 2 for t in asyncio.run(tasks.get_running(user=MEMBER))["tasks"]))

task_monitor.last_runs["sync"] = {"name": "Plex sync", "status": "done", "at": "", "message": "", "elapsed_s": 1}
check("task history is the admin's (one entry per category can be anyone's run)",
      asyncio.run(tasks.get_task_history(user=MEMBER))["last_runs"] == {}
      and "sync" in asyncio.run(tasks.get_task_history(user=ADMIN))["last_runs"])

snapshot = task_monitor.get_all()
check("the stream applies the same filter to each snapshot",
      {t["id"] for t in tasks._visible(snapshot, 2, False)} == member_ids
      and tasks._visible(snapshot, 2, True) is snapshot)
src = (_ROOT / "src/routers/tasks.py").read_text(encoding="utf-8")
gen = src[src.index("async def stream_tasks"):]
check("wiring: the SSE generator filters both the first snapshot and every event",
      gen.count("_visible(") == 2 and "is_admin = _is_admin(user_id)" in gen)

# ── library status ───────────────────────────────────────────────────────────
lib.get_state = lambda k: "2026-09-25T10:00:00" if k.endswith(":last") else None
lib._get_arr_url_key = lambda svc: ("http://192.168.1.100:8989", "key") if svc != "lidarr" else (None, None)
lib._read_defaults = lambda svc: {"root_folder": "/storage/media/tv"}
a = asyncio.run(lib.library_status(_user=ADMIN))
m = asyncio.run(lib.library_status(_user=MEMBER))
check("admin gets the address, defaults and the last test",
      a["sonarr"]["url"].startswith("http://") and a["sonarr"]["defaults"] and a["sonarr"]["last_test"])
check("member gets configured + music source, nothing about the LAN",
      m["sonarr"]["configured"] is True and m["sonarr"]["url"] == "" and m["sonarr"]["defaults"] == {}
      and m["sonarr"]["last_test"] is None and "music_source" in m["lidarr"])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
