"""A deletion run stops for a game, stops for an outage, and says why.

    python tests/test_deletion_run_resilience.py

From the 2026-10 pipeline audit:

* a game started mid-run: the watcher unloaded the pitcher every 30 s and
  the loop loaded it straight back into the game's VRAM, once per title.
* an Ollama that could not answer cost the full judge timeout per title, up
  to the 60-title cap — and every cause read "the model did not answer".
* Pillar III lived only in the prompt: a model that missed it could CUT a
  show another household member had watched.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = FAIL = 0


def check(name, cond, info=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  [{info}]" if info is not None else ""))


import httpx

import src.services.llm_lane as lane
import src.services.process_monitor as pm
import src.services.recommendations_engine as eng
from src.services import pillars
from src.services.llm_errors import INFRA, LLMFailure

ROOT = Path(__file__).resolve().parents[1]

# ── the lane check the loop runs before every verdict ───────────────────────

pm.is_game_running = lambda: False
check("a free GPU lets the next verdict run", eng._judge_lane() == (True, ""))

pm.is_game_running = lambda: True
lane.gpu_reason = lambda: "17467/24564 MB, 90 %"
ok, why = eng._judge_lane()
check("a game closes the lane", ok is False)
check("...and says which program took what",
      "another program took the graphics card" in why and "17467/24564 MB" in why, why)


def _boom():
    raise RuntimeError("nvidia-smi missing")


pm.is_game_running = _boom
check("a detection failure counts as free (never silently stops every run)",
      eng._judge_lane() == (True, ""))

# ── what counts as an outage ────────────────────────────────────────────────

check("offline, load failures, timeouts and a missing model are outages",
      {LLMFailure.INFRA_OFFLINE, LLMFailure.INFRA_LOAD, LLMFailure.INFRA_TIMEOUT,
       LLMFailure.MODEL_MISSING} <= INFRA)
check("a bad answer is not an outage (it does not stop the run)",
      not ({LLMFailure.PARSE, LLMFailure.SCHEMA, LLMFailure.OUTPUT_TRUNCATED,
            LLMFailure.CONTEXT_OVERFLOW, LLMFailure.EVIDENCE} & INFRA))
check("two outages in a row stop the run", eng._OUTAGE_STOP_AFTER == 2)

# ── the run summary names the cause ─────────────────────────────────────────

eng.DELETION_RUN_SUMMARY.clear()
run = eng._record_deletion_run("movie", judged=7, flagged=2, deferred=1,
                               fail_kinds={"infra_offline": 2, "parse": 1, "schema": 0},
                               stopped="Ollama is not answering")
check("failed keeps its meaning: titles without a verdict", run["failed"] == 3)
check("zero counts are dropped", run["failed_by_kind"] == {"infra_offline": 2, "parse": 1})
check("the reason names the causes, most frequent first",
      run["failed_reason"] == "Ollama is not answering (2); the model's answer was not readable (1)",
      run["failed_reason"])
check("an unknown kind still reads as a sentence",
      eng._failure_sentence({"unknown": 1}) == "an unexpected error (1)")
eng._record_deletion_run("show", judged=0, stopped="another program took the graphics card")
allr = eng.deletion_run_summary()
check("the All tab merges the causes", allr["failed_by_kind"] == {"infra_offline": 2, "parse": 1})
check("...and lists every reason a category stopped",
      "Ollama is not answering" in allr["stopped"]
      and "another program took the graphics card" in allr["stopped"])
check("a run stopped before it started is recorded, with nothing judged",
      eng.deletion_run_summary("show")["judged"] == 0
      and eng.deletion_run_summary("show")["failed"] == 0)
eng.DELETION_RUN_SUMMARY.clear()

# ── the loop, in order ──────────────────────────────────────────────────────

src = (ROOT / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
block = src.split("if getattr(settings, \"PILLARS_ENABLED\", False):")[1]
block = block.split("# ── LEGACY taste-mismatch pitch path")[0]
loop = block.split("for cand in scored_candidates:")[1]

check("a closed lane is seen BEFORE the warm-up (which can load the summariser)",
      block.index("_judge_lane()") < block.index("PRE-JUDGE SIGNIFICANCE WARM-UP"))
check("...and before the gate is taken",
      block.index("_judge_lane()") < block.index("await curator_start(_gate_label"))
check("the loop re-checks after yielding to a chat, before the next verdict",
      loop.index("if gate_contested():") < loop.index("_judge_lane()")
      < loop.index("await build_evidence("))
check("the outage breaker is checked before any new work",
      loop.index("consecutive_outages >= _OUTAGE_STOP_AFTER") < loop.index("await build_evidence("))
outage = loop.split("if outage:")[1].split("else:")[0]
check("an outage does not spend the 60-title cap", "judged -= 1" in outage
      and "consecutive_outages += 1" in outage)
check("a real verdict resets the outage streak",
      "consecutive_outages = 0\n                v = (verdict or {}).get(\"verdict\")" in loop)
check("the household floor runs before the CUT becomes a proposal",
      loop.index('if v == "CUT" and _claim:') < loop.index('if v in ("CUT", "STAGNANT"):'))
check("...and turns it into the owner's call, not a keep",
      '"verdict": "STAGNANT"' in loop and '"_guard": "household_floor"' in loop)
check("the gate is still released by the finally after any break",
      "if _holds_gate:" in block.split("finally:")[1])

# ── the pitch for a floored CUT ─────────────────────────────────────────────

PROMPTS = []
_Real = httpx.AsyncClient


def _handler(request):
    PROMPTS.append(json.loads(request.content)["messages"][-1]["content"])
    return httpx.Response(200, json={"message": {"content": "A sharp note."}})


httpx.AsyncClient = lambda **kw: _Real(transport=httpx.MockTransport(_handler), **kw)
try:
    floored = {"verdict": "STAGNANT", "_guard": "household_floor", "_guard_detail": "Ana",
               "pillar_0_ego": "x", "protecting_pillar": "NONE"}
    asyncio.run(pillars.write_monologue("TITLE: X", floored, skip_priority=True))
    check("a floored CUT is pitched as the household's title, not as 'merely fine'",
          "Another member of the household has genuinely watched" in PROMPTS[-1]
          and "merely 'fine'" not in PROMPTS[-1])
    asyncio.run(pillars.write_monologue("TITLE: X", {**floored, "_guard": None},
                                        skip_priority=True))
    check("an ordinary STAGNANT keeps its own stance", "merely 'fine'" in PROMPTS[-1])
finally:
    httpx.AsyncClient = _Real

# ── the banner shows it ─────────────────────────────────────────────────────

ui = (ROOT / "frontend/js/deletions.js").read_text(encoding="utf-8")
banner = ui.split("export function _renderRunBanner")[1].split("\n}\n")[0]
check("the banner shows the server's cause, escaped",
      "esc(run.failed_reason" in banner)
check("...and why a run stopped early", "esc(run.stopped)" in banner)
check("a run that only stopped early still gets a banner",
      "run.failed || run.deferred || run.stopped" in banner)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
