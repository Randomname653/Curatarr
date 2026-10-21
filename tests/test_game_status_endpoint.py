"""The UI's game badge reads the watcher's verdict instead of probing live.

2026-10-21: GET /api/processes/status took 2.3 s on every 30 s poll while
the deletion judge loaded the GPU. It ran nvidia-smi and a process walk per
call although the game watcher stores exactly that verdict (game_active)
every 30 s. The endpoint now reads the stored flag and probes live only
before the watcher's first tick.

    python tests/test_game_status_endpoint.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.routers.process_monitor as pmr  # noqa: E402
import src.services.app_state as app_state  # noqa: E402
import src.services.process_monitor as pm  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# The handler sits behind ttl_response; call the undecorated function.
handler = getattr(pmr.game_status, "__wrapped__", pmr.game_status)
probes = []
_real = (app_state.get_state, pm.is_game_running)


def _status(flag):
    state = {"game_active": flag, "llm_lane": "free", "llm_lane_reason": ""}
    app_state.get_state = lambda k: state.get(k)
    probes.clear()
    return handler(_user=None)


pm.is_game_running = lambda: probes.append(1) or True
try:
    on = _status("1")
    check("a stored '1' reads as a game, without a live probe", on["game_running"] is True and not probes)
    off = _status("0")
    check("a stored '0' reads as no game, without a live probe", off["game_running"] is False and not probes)
    early = _status(None)
    check("before the watcher's first tick the endpoint probes live", early["game_running"] is True and probes == [1])
    check("the lane comes from the watcher as before", on["lane"] == "free" and on["lane_reason"] == "")
finally:
    app_state.get_state, pm.is_game_running = _real

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
