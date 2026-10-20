"""The setup mini-bench: its verdict rules, its fixtures, its wiring.

    python tests/test_setup_minibench.py

After the bake the wizard runs the curator through production calls on two
fixed titles (src/services/setup_minibench.py). The verdict rules are pure
and pinned here; the fixtures must belong to the FICTIONAL owner Jane Doe
and never to a real user; the route and the wizard must actually use it.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services import setup_minibench as mb  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


ok_warm = {"ok": True, "verdict": "ok", "cpu_percent": 0}
good = [{"wall_s": 3.1, "valid": True}, {"wall_s": 2.9, "valid": True}]

# ── verdict rules ──────────────────────────────────────────────────────────
check("fast valid verdicts on a roomy card -> ok",
      mb.classify(ok_warm, good, 4000) == ("ok", ""))
check("the warm-up's CPU spill wins over everything",
      mb.classify({**ok_warm, "verdict": "cpu_spill", "cpu_percent": 40}, good, 4000)[0] == "spill")
check("a full card at the bench's speed is ok, with a 'nearly full' note (gemma4:31b on 24 GB)",
      mb.classify(ok_warm, good, 204, 38.6, 41.7)[0] == "ok"
      and "nearly full" in mb.classify(ok_warm, good, 204, 38.6, 41.7)[1])
check("a full card writing far below the bench's speed is a spill (Windows sysmem fallback)",
      mb.classify(ok_warm, good, 204, 9.0, 41.7)[0] == "spill")
check("an unknown free figure (remote Ollama) is not a spill",
      mb.classify(ok_warm, good, None)[0] == "ok")
check("no valid verdict at all -> unreliable",
      mb.classify(ok_warm, [{"wall_s": 3, "valid": False}] * 2, 4000)[0] == "unreliable")
check("over the comfort line per verdict -> slow, with the number",
      mb.classify(ok_warm, [{"wall_s": 31, "valid": True}, {"wall_s": 29, "valid": True}], 4000)
      == ("slow", "30 s per judge verdict (comfortable: under 20 s)"))
check("one of two verdicts invalid is still ok, but says so",
      mb.classify(ok_warm, [{"wall_s": 3, "valid": True}, {"wall_s": 3, "valid": False}], 4000)[0] == "ok"
      and "repair" in mb.classify(ok_warm, [{"wall_s": 3, "valid": True}, {"wall_s": 3, "valid": False}], 4000)[1])
check("the comfort line is the matrix's",
      mb.COMFORT_JUDGE_S == 20.0)

# ── fixtures: a fictional owner only ───────────────────────────────────────
facts = "\n".join(f for _, f in mb.FIXTURES)
check("two fixture titles, both carry Jane Doe's taste",
      len(mb.FIXTURES) == 2 and all(mb.JANE_DOE_TASTE in f for _, f in mb.FIXTURES))
check("no fixture claims the owner watched anything, and no household engagement",
      all("not watched by the owner" in f and "none have watched it" in f for _, f in mb.FIXTURES))
check("no OWNER SIGNAL lines (those are a real user's words)",
      "OWNER SIGNAL" not in facts)
check("one fixture is a bloated file (the flag path is exercised)",
      "(bloated)" in facts and "(normal)" in facts)

# ── options: the package's settings reach the calls ────────────────────────
check("a package's num_gpu -1 leaves the split to Ollama",
      "num_gpu" not in mb._options(16384, -1) and mb._options(16384, -1)["num_ctx"] == 16384)
check("an explicit num_gpu is passed through",
      mb._options(8192, 99) == {"num_ctx": 8192, "num_gpu": 99})
check("only a local Ollama has its free VRAM read off this host's nvidia-smi",
      mb._is_local("http://127.0.0.1:11434") and mb._is_local("http://localhost:11434")
      and not mb._is_local("http://192.168.0.189:30068"))

# ── a warm-up that fails ends the bench without raising ────────────────────
import src.services.setup_wizard as _sw  # noqa: E402
_orig = _sw.warmup_check


async def _fail(endpoint, model):
    return {"ok": False, "error": "Could not connect to Ollama"}
_sw.warmup_check = _fail
r = asyncio.run(mb.run_minibench("http://127.0.0.1:1", "curatarr-curator"))
_sw.warmup_check = _orig
check("an unreachable Ollama returns ok False with the warm-up's error",
      r["ok"] is False and "connect" in r["error"].lower())

# ── wiring ─────────────────────────────────────────────────────────────────
root = Path(__file__).resolve().parents[1]
router = (root / "src/routers/setup.py").read_text(encoding="utf-8")
js = (root / "frontend/js/setup.js").read_text(encoding="utf-8")
check("the setup router serves POST /minibench behind the admin/first-run gate",
      '@router.post("/minibench")' in router and "run_minibench" in router
      and "require_admin_or_first_run" in router.split('@router.post("/minibench")')[1][:400])
check("the wizard runs the mini-bench after the bake and renders it",
      "/api/setup/minibench" in js and "renderMinibench(" in js)
check("the wizard no longer shows agreement with the production model as a quality",
      "agree" not in js.split("export function renderHardwarePackage")[1].split("export function")[0])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
