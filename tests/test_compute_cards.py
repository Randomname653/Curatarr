"""What each card is doing right now (2026-10-25, ROADMAP "Two cards,
visible"; src/services/compute_cards.py, GET /api/hosts/cards).

The lane's holder and queue, the PC's state (free / busy / which game /
going away / away) and VRAM, the container's memory; drawn under each card
in Settings -> Maintenance.

    python tests/test_compute_cards.py
"""
import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services import compute_cards as cc, llm_priority as lp  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── the lane ────────────────────────────────────────────────────────────────
lp._reset_lanes()
check("a lane nobody used: idle, nobody waiting",
      cc.lane_view("http://nas:11434") == {"holder": "", "active": 0, "queued": 0,
                                            "queued_interactive": 0, "warm": False})


async def _queue():
    ln = lp.lane("http://pc:11434/")
    ln.active, ln.owner, ln.warm = 1, "deletion scan: anime", True
    loop = asyncio.get_running_loop()
    ln.gate._free = 0
    ln.gate._heap += [(lp.INTERACTIVE, 1, loop.create_future()), (lp.BACKGROUND, 2, loop.create_future())]
    return cc.lane_view("http://pc:11434")


v = asyncio.run(_queue())
check("a held lane: its holder, two waiting, one of them a chat",
      v == {"holder": "deletion scan: anime", "active": 1, "queued": 2, "queued_interactive": 1, "warm": True})
ln = lp.lane("http://pc:11434")
ln.active = 0
check("a holder label left over without an active holder is not shown",
      cc.lane_view("http://pc:11434")["holder"] == "")
lp._reset_lanes()

# ── VRAM ────────────────────────────────────────────────────────────────────
check("nvidia-smi's reading becomes used / total / util",
      cc.vram((17500.0, 24564.0, 90.0)) == {"used_mb": 17500, "total_mb": 24564, "util": 90}
      and cc.vram((100.0, 8000.0, None))["util"] is None)
check("no card, a timeout or an error: no reading",
      cc.vram(None) is None and cc.vram("timeout") is None and cc.vram("missing") is None)
calls = []
cc._smi_cache.clear()
r1 = cc._local_smi(now=100.0, reader=lambda: calls.append(1) or (1.0, 2.0, 3.0))
r2 = cc._local_smi(now=105.0, reader=lambda: calls.append(1) or (9.0, 9.0, 9.0))
r3 = cc._local_smi(now=111.0, reader=lambda: calls.append(1) or (9.0, 9.0, 9.0))
check("nvidia-smi runs at most every 10 s", r1 == r2 == (1.0, 2.0, 3.0) and r3[0] == 9.0 and len(calls) == 2)
cc._smi_cache.clear()

# ── the container's memory ──────────────────────────────────────────────────


class _Proc:
    def memory_info(self):
        return type("M", (), {"rss": 1536 * 1024 * 1024})()


tmp = Path(tempfile.mkdtemp())
v2 = tmp / "v2"
v2.mkdir()
(v2 / "memory.current").write_text(f"{2048 * 1024 * 1024}\n")
(v2 / "memory.max").write_text(f"{8192 * 1024 * 1024}\n")
m = cc.container_memory(v2, proc=_Proc(), free_mb=lambda: 7800)
check("cgroup v2: the container's use and limit, Curatarr's RSS, the host's free RAM",
      m == {"rss_mb": 1536, "used_mb": 2048, "limit_mb": 8192, "free_mb": 7800})
(v2 / "memory.max").write_text("max\n")
check("...no limit set: none shown", cc.container_memory(v2, proc=_Proc(), free_mb=lambda: 1)["limit_mb"] is None)
v1 = tmp / "v1"
(v1 / "memory").mkdir(parents=True)
(v1 / "memory" / "memory.usage_in_bytes").write_text(str(1024 * 1024 * 1024))
(v1 / "memory" / "memory.limit_in_bytes").write_text(str(9223372036854771712))
m = cc.container_memory(v1, proc=_Proc(), free_mb=lambda: None)
check("cgroup v1: the use, and its huge 'no limit' number read as none",
      m["used_mb"] == 1024 and m["limit_mb"] is None and m["free_mb"] is None)
m = cc.container_memory(tmp / "none", proc=_Proc(), free_mb=lambda: 5)
check("outside a container: only the RSS and the free RAM",
      m == {"rss_mb": 1536, "used_mb": None, "limit_mb": None, "free_mb": 5})

# ── the PC's state ──────────────────────────────────────────────────────────
snap = {"processes": [{"name": "Fallout4.exe", "rss_mb": 9000}, {"name": "ollama.exe"}]}
targets = frozenset({"fallout4.exe"})
s = cc.pc_state({"available": True, "busy": True, "busy_reason": "Fallout4.exe running",
                 "changed_at": 1.0, "seen_at": 2.0}, snap, targets)
check("busy with a game: the game named", s["state"] == "busy" and s["game"] == "Fallout4.exe"
      and s["reason"] == "Fallout4.exe running")
check("free", cc.pc_state({"available": True}, {}, targets)["state"] == "free")
check("going away", cc.pc_state({"available": True, "draining": True}, snap, targets)
      == {"state": "going away", "reason": "", "game": None, "since": None, "seen_at": None})
check("away, with its reason", cc.pc_state({"available": False, "reason": "no answer"}, snap, targets)["state"] == "away"
      and cc.pc_state({"available": False, "reason": "no answer"}, snap, targets)["game"] is None)

# ── the endpoint and the view ───────────────────────────────────────────────
hosts = (ROOT / "src/routers/hosts.py").read_text(encoding="utf-8")
check("GET /api/hosts/cards is admin-only and reads off the event loop",
      '@router.get("/cards")' in hosts and "cards_now(user: User = Depends(require_admin))" in hosts
      and "await run_read(cards)" in hosts)
JS = (ROOT / "frontend/js/nodes.js").read_text(encoding="utf-8")
check("no inline style in the node view", 'style="' not in JS and ".style." not in JS)
node = shutil.which("node")
if node:
    body = re.sub(r"^import .*$", "", JS, flags=re.M).replace("export async function", "async function") \
        .replace("export function", "function")
    harness = r"""
const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const escAttr = esc; const act = () => ''; const _errHtml = () => ''; const _errMsg = e => String(e);
const _fmtRel = () => ''; const btnBusy = () => {}; const btnDone = () => {}; const toast = () => {};
const loadUnreadMessages = () => {}; const api = () => ({});
""" + body + """
const lane = {holder: 'deletion scan: anime', queued: 2, queued_interactive: 1};
console.log(JSON.stringify({
  nas: _nasNow({lane, vram: {used_mb: 17510, total_mb: 24576, util: 90}, mode: 'cpu',
                memory: {used_mb: 2048, limit_mb: 8192, rss_mb: 1536, free_mb: 7800}}),
  idle: _laneLine({holder: '', queued: 0}),
  pc: _pcNow({state: 'busy', game: 'Fallout4.exe', lane: {holder: '', queued: 1, queued_interactive: 0}, vram: null}),
  away: _pcNow({state: 'away'}),
}));
"""
    hp = Path(tempfile.mkdtemp()) / "h.js"
    hp.write_text(harness, encoding="utf-8")
    r = subprocess.run([node, str(hp)], capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        print(r.stderr[:1500])
    res = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {}
    check("this server: the holder, the queue with its chats, the card, the lane mode",
          "Working: deletion scan: anime · 2 waiting (1 chat)" in res.get("nas", "")
          and "card 17.1 of 24.0 GB, 90 %" in res.get("nas", "")
          and "summaries on the CPU" in res.get("nas", ""))
    check("...and the memory: container of its limit, Curatarr, free",
          "container 2.0 of 8.0 GB · Curatarr 1.5 GB · 7.6 GB free" in res.get("nas", ""))
    check("idle and nobody waiting", res.get("idle") == "Idle · nobody waiting")
    check("the PC: its queue and the game that holds it",
          "Idle · 1 waiting" in res.get("pc", "") and "Fallout4.exe" in res.get("pc", "")
          and "Away" in res.get("away", ""))
else:
    print("  (node not installed - checked statically)")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
