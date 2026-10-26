"""The PC agent and what the NAS does with it (2026-10-22, NAS move phase 2).

Curatarr on the NAS borrows the gaming PC's card. Game detection used to
read the machine Curatarr runs on - on the NAS that is the NAS. The PC's
tray now runs an agent reporting processes and GPU; the NAS polls it, keeps
the PC "available" through one dropped poll but not two, takes it away at
once on a shutdown notice, and classifies games from the PC's processes.

Real HTTP on 127.0.0.1 for the agent; the NAS side with injected fetches.

    python tests/test_pc_agent.py
"""
import asyncio
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import pc_agent  # noqa: E402
from src.config import settings  # noqa: E402
from src.services import pc_host  # noqa: E402
from src.services import process_monitor as pm  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


TOKEN = "t" * 24
PROCS = [{"name": "Fallout4.exe", "rss_mb": 9000}, {"name": "chrome.exe", "rss_mb": 800}]

# ── the agent ───────────────────────────────────────────────────────────────
posted = []
agent = pc_agent.Agent(TOKEN, processes=lambda: PROCS, gpu=lambda: [20000.0, 24564.0, 97.0],
                       notify_url="http://nas:30000/",
                       post=lambda url, body, tok: posted.append((url, body, tok)))
try:
    pc_agent.Agent("short")
    check("a short or missing token refuses to start", False)
except ValueError:
    check("a short or missing token refuses to start", True)

server = agent.serve(host="127.0.0.1", port=0)
port = server.server_address[1]


def get(path, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers={pc_agent.TOKEN_HEADER: token} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None


check("no token, no report", get("/status")[0] == 401)
check("a wrong token, no report", get("/status", "x" * 24)[0] == 401)
check("only /status exists", get("/", TOKEN)[0] == 404)
code, rep = get("/status", TOKEN)
check("the report: processes with memory, the GPU reading, not shutting down",
      code == 200 and rep["v"] == 1 and rep["processes"] == PROCS
      and rep["gpu"] == [20000.0, 24564.0, 97.0] and rep["shutting_down"] is False)

agent.announce_shutdown()
agent.announce_shutdown()                  # twice: one notice
check("shutdown: one goodbye to the NAS, with the token",
      posted == [("http://nas:30000/api/hosts/pc/goodbye", {"reason": "shutdown"}, TOKEN)])
check("...and the next report says so", get("/status", TOKEN)[1]["shutting_down"] is True)
agent.stop()

broken = pc_agent.Agent(TOKEN, processes=lambda: 1 / 0, gpu=lambda: 1 / 0)
snap = broken.snapshot()
check("a failing process walk or nvidia-smi reports none/error, never kills the agent",
      snap["processes"] == [] and snap["gpu"] == "error")
check("start_from_settings without a token: no agent",
      pc_agent.start_from_settings(SimpleNamespace(PC_AGENT_TOKEN=None)) is None)

# ── the NAS: polling, hysteresis, goodbye ───────────────────────────────────
saved = (settings.PC_AGENT_URL, settings.PC_AGENT_TOKEN)
settings.PC_AGENT_URL, settings.PC_AGENT_TOKEN = "http://pc:11435", TOKEN
pc_host._state.update(available=False, draining=False, misses=0, snapshot=None)


def poll(result):
    async def fetch(url, tok):
        assert url == "http://pc:11435/status" and tok == TOKEN
        if isinstance(result, BaseException):
            raise result
        return result
    asyncio.run(pc_host.poll_once(fetch))


try:
    report = {"v": 1, "processes": PROCS, "gpu": [1000.0, 24564.0, 3.0], "shutting_down": False}
    poll(report)
    check("one good answer: available, with its report", pc_host.available() and pc_host.snapshot() == report)
    poll(OSError("timed out"))
    check("one missed poll does not flip it", pc_host.available())
    poll(OSError("timed out"))
    check("two in a row: unavailable, report dropped",
          not pc_host.available() and pc_host.snapshot() is None)
    poll(report)
    check("back as soon as it answers", pc_host.available())
    misses = pc_host._state["misses"]
    poll(asyncio.CancelledError())
    check("a poll cancelled by the app stopping is no missed poll and raises nothing",
          pc_host.available() and pc_host._state["misses"] == misses)
    poll({**report, "shutting_down": True})
    check("a report that says shutting down: gone at once, draining",
          not pc_host.available() and pc_host.status()["draining"])
    poll(report)
    pc_host.goodbye("shutdown")
    check("the goodbye notice: gone at once, draining",
          not pc_host.available() and pc_host.status()["draining"])

    # ── game detection reads the PC ─────────────────────────────────────────
    pm._cached_targets, pm._cached_time = frozenset({"fallout4.exe"}), 10 ** 12
    pm._gpu_cache.update(at=0.0, busy=False, since=0.0, reason="")
    poll({**report, "processes": PROCS})
    check("a classified game on the PC is a game", pm.game_process_running())
    poll({**report, "processes": [{"name": "chrome.exe", "rss_mb": 800}]})
    check("no game on the PC, quiet card: not busy", not pm.is_game_running())
    pc_host.goodbye()
    check("PC unreachable: fails closed (nobody can say its card is free)",
          pm.is_game_running() and pm.pc_unreachable())
    check("...and runs no game", not pm.game_process_running())
    check("the agent's GPU states map onto nvidia-smi's",
          pm._smi_from_report([1.0, 2.0, 3.0]) == (1.0, 2.0, 3.0)
          and pm._smi_from_report("missing") is pm.SMI_MISSING
          and pm._smi_from_report("timeout") == pm.SMI_TIMEOUT
          and pm._smi_from_report("junk") == pm.SMI_ERROR)

    # "is this a game?" asks about the PC's programs
    pm._cached_classified, pm._cached_classified_time = (frozenset(), frozenset()), 10 ** 12
    poll({**report, "processes": [{"name": "NewGame.exe", "rss_mb": 4000},
                                  {"name": "tiny.exe", "rss_mb": 1}]})
    names = [p["name"] for p in pm.get_unknown_processes()]
    check("unknown programs come from the PC (big ones only)", names == ["NewGame.exe"])

    # ── the goodbye endpoint ────────────────────────────────────────────────
    from fastapi import HTTPException
    from src.routers import hosts
    poll(report)
    try:
        hosts.pc_goodbye(hosts.Goodbye(), x_agent_token="wrong" * 5)
        check("the goodbye needs the agent token", False)
    except HTTPException as e:
        check("the goodbye needs the agent token", e.status_code == 401 and pc_host.available())
    hosts.pc_goodbye(hosts.Goodbye(reason="logoff"), x_agent_token=TOKEN)
    check("with it, the PC is taken away at once", not pc_host.available())
finally:
    settings.PC_AGENT_URL, settings.PC_AGENT_TOKEN = saved
    pm._cached_targets = pm._cached_classified = None

check("without an agent configured, this machine is read as before",
      not pc_host.configured() and pm._pc_report() is pm._LOCAL and not pm.pc_unreachable())

# ── wiring ──────────────────────────────────────────────────────────────────
tray = (ROOT / "src/tray_app.py").read_text(encoding="utf-8")
check("the tray starts the agent and announces a real shutdown (menu, Windows logoff)",
      "pc_agent.start_from_settings(settings, on_updated=_restart_after_update)" in tray
      and 'pystray.MenuItem("Shutdown", lambda: _stop_for_good())' in tray
      and "_install_endsession_hook(_stop_for_good, server_thread)" in tray)
sched = (ROOT / "src/services/scheduler.py").read_text(encoding="utf-8")
check("the NAS polls every few seconds when an agent is configured, and a PC that is off "
      "is not asked to evict",
      "pc_host.poll_once" in sched and "if running and not pc_unreachable():" in sched)
main_src = (ROOT / "src/main.py").read_text(encoding="utf-8")
check("the goodbye route is mounted", 'prefix="/api/hosts"' in main_src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
