"""The judge's answer is parsed, validated and, once, repaired.

    python tests/test_verdict_parsing.py

From the 2026-10 pipeline audit:

* parse_llm_json ran the markup stripper over the RAW answer. A valid,
  schema-forced verdict with "<" in one finding and ">" in the next lost the
  whole field between them — and still passed, because only the verdict enum
  was checked.
* a CUT naming HOUSEHOLD as its protecting pillar was accepted and proposed
  for deletion.
* every failure — Ollama down, a model that could not load, a cut-off
  answer, bad JSON — became the same anonymous EVALUATE.
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

import src.services.llm_errors as le
from src.services import pillars
from src.services.llm_errors import LLMCallError, LLMFailure
from src.services.llm_utils import parse_llm_json

GOOD = {"pillar_3_household": "No other user watched it.",
        "pillar_2_custodian": "Competent, no documented stature.",
        "pillar_1_resonance": "Fails the Awe test.",
        "pillar_0_ego": "Nothing the owner's profile rewards.",
        "bitrate_note": "Sane bitrate.",
        "protecting_pillar": "NONE", "verdict": "CUT"}

# ── parse_llm_json: valid JSON is never cut apart ────────────────────────────

live = json.dumps({**GOOD,
                   "pillar_2_custodian": "RT score <Ninety but Metacritic is high",
                   "pillar_1_resonance": "pacing is generic>filler"})
parsed = parse_llm_json(live)
check("the live failure: every key survives '<' in one field and '>' in the next",
      set(parsed) == set(GOOD), sorted(parsed))
check("...and the text between them is kept",
      parsed["pillar_1_resonance"] == "pacing is generic>filler")
check("a code fence inside a string no longer breaks the parse",
      parse_llm_json(json.dumps({"verdict": "CUT", "note": "```json nope```"}))["verdict"] == "CUT")
check("real markup inside a value is still stripped (persisted text stays clean)",
      parse_llm_json(json.dumps({"a": "fine <script>alert(1)</script> film"}))["a"]
      == "fine alert(1) film")
check("nested values are cleaned too",
      parse_llm_json(json.dumps({"a": [{"b": "<b>x</b>"}]})) == {"a": [{"b": "x"}]})
check("a fenced answer still parses through the fallback",
      parse_llm_json('```json\n{"verdict": "CUT"}\n```') == {"verdict": "CUT"})
try:
    parse_llm_json("the model rambled instead")
    check("prose still raises", False)
except json.JSONDecodeError:
    check("prose still raises", True)

# ── Verdict: shape and self-consistency ─────────────────────────────────────


def _kind(data):
    try:
        pillars._parse_verdict(json.dumps(data) if not isinstance(data, str) else data)
        return None
    except LLMCallError as e:
        return e.kind


check("a well-formed CUT is accepted", _kind(GOOD) is None)
check("a missing pillar finding is a SCHEMA failure",
      _kind({k: v for k, v in GOOD.items() if k != "pillar_1_resonance"}) is LLMFailure.SCHEMA)
check("a blank finding is too", _kind({**GOOD, "pillar_0_ego": "  "}) is LLMFailure.SCHEMA)
check("a CUT that names HOUSEHOLD contradicts itself",
      _kind({**GOOD, "protecting_pillar": "HOUSEHOLD"}) is LLMFailure.SCHEMA)
check("a HARD_KEEP that names no pillar contradicts itself",
      _kind({**GOOD, "verdict": "HARD_KEEP"}) is LLMFailure.SCHEMA)
check("a keep with its pillar is fine",
      _kind({**GOOD, "verdict": "HARD_KEEP", "protecting_pillar": "CUSTODIAN"}) is None)
check("an unknown verdict is a SCHEMA failure",
      _kind({**GOOD, "verdict": "DELETE"}) is LLMFailure.SCHEMA)
check("prose is a PARSE failure", _kind("I think it should go.") is LLMFailure.PARSE)
check("a JSON list is not a verdict", _kind([GOOD]) is LLMFailure.SCHEMA)
v = pillars._parse_verdict(json.dumps({**GOOD, "pillar_0_ego": "x" * 5000}))
check("verbosity is trimmed, not rejected", len(v.pillar_0_ego) == 800)
check("a missing bitrate_note defaults to empty (older answers stay valid)",
      pillars._parse_verdict(json.dumps({k: x for k, x in GOOD.items()
                                         if k != "bitrate_note"})).bitrate_note == "")

# ── the schema: generation order and bounds ─────────────────────────────────

props = list(pillars.VERDICT_SCHEMA["properties"])
check("bitrate_note is generated BEFORE the decision fields",
      props.index("bitrate_note") < props.index("protecting_pillar") < props.index("verdict"))
check("...and is required, so the grammar cannot drop it",
      "bitrate_note" in pillars.VERDICT_SCHEMA["required"])
check("no extra properties, findings bounded",
      pillars.VERDICT_SCHEMA["additionalProperties"] is False
      and pillars.VERDICT_SCHEMA["properties"]["pillar_0_ego"]["maxLength"] == 400)

# ── adjudicate: one repair, never for an outage ─────────────────────────────

CALLS = []
SCRIPT = []


async def fake_post_chat(base_url, payload, *, read_timeout):
    CALLS.append(json.loads(json.dumps(payload)))
    step = SCRIPT.pop(0)
    if isinstance(step, Exception):
        raise step
    le.check_generation(step, payload.get("options") or {})
    return step


def answer(data, **meta):
    content = data if isinstance(data, str) else json.dumps(data)
    return {"message": {"content": content}, "done_reason": "stop", **meta}


_real_post_chat = le.post_chat
le.post_chat = fake_post_chat


def run(*steps):
    CALLS.clear()
    SCRIPT[:] = list(steps)
    return asyncio.run(pillars.adjudicate("TITLE: X", skip_priority=True))


out = run(answer(GOOD))
check("a valid first answer: one call, no repair flag",
      len(CALLS) == 1 and out["verdict"] == "CUT" and "_repaired" not in out)
check("the judge call pins temperature 0, a seed and no repeat penalty",
      CALLS[0]["options"]["temperature"] == 0.0 and CALLS[0]["options"]["seed"] == 7
      and CALLS[0]["options"]["repeat_penalty"] == 1.0)
check("the untrusted-data rule naming the fence markers reaches the judge",
      "<<<UNTRUSTED_SOURCE" in CALLS[0]["messages"][0]["content"])

out = run(answer({**GOOD, "protecting_pillar": "HOUSEHOLD"}),
          answer({**GOOD, "verdict": "STAGNANT"}))
check("a contradictory verdict gets one repair turn",
      len(CALLS) == 2 and out["verdict"] == "STAGNANT" and out.get("_repaired"))
rep = CALLS[1]["messages"]
check("...which shows the model its own answer and names the fault",
      rep[-2]["role"] == "assistant" and "HOUSEHOLD" in rep[-2]["content"]
      and rep[-1]["role"] == "user" and "protecting_pillar" in rep[-1]["content"])

out = run(answer({**GOOD, "protecting_pillar": "HOUSEHOLD"}),
          answer({**GOOD, "protecting_pillar": "HOUSEHOLD"}))
check("a second bad answer fails closed: EVALUATE, never a proposal",
      len(CALLS) == 2 and out["verdict"] == "EVALUATE" and out["_error_kind"] == "schema")

out = run(answer('{"pillar_3_household": "No other', done_reason="length"),
          answer(GOOD))
check("a cut-off answer is repaired with more room",
      out["verdict"] == "CUT" and out.get("_repaired")
      and CALLS[1]["options"]["num_predict"] > CALLS[0]["options"]["num_predict"])

out = run(LLMCallError(LLMFailure.INFRA_OFFLINE, "refused"))
check("Ollama offline: no repair attempt, kind recorded",
      len(CALLS) == 1 and out["_error_kind"] == "infra_offline" and out["verdict"] == "EVALUATE")

out = run(LLMCallError(LLMFailure.INFRA_LOAD, "requires more system memory"))
check("a model that cannot load is not retried either",
      len(CALLS) == 1 and out["_error_kind"] == "infra_load")

out = run(answer(GOOD, prompt_eval_count=16000))
check("a prompt that filled the window is CONTEXT_OVERFLOW, not a verdict",
      out["_error_kind"] == "context_overflow" and len(CALLS) == 1)

out = run(answer({**GOOD, "verdict": "EVALUATE"}))
check("a genuine EVALUATE carries no error",
      out["verdict"] == "EVALUATE" and "_error" not in out)

# ── post_chat: real transport failures, classified ──────────────────────────

_RealClient = httpx.AsyncClient
le.post_chat = _real_post_chat


def with_transport(handler):
    le.httpx.AsyncClient = lambda **kw: _RealClient(transport=httpx.MockTransport(handler), **kw)


def call():
    try:
        return asyncio.run(le.post_chat("http://ollama:11434", {
            "model": "m", "options": {"num_ctx": 16384, "num_predict": 800}},
            read_timeout=5))
    except LLMCallError as e:
        return e.kind


def refuse(req):
    raise httpx.ConnectError("Connection refused")


def slow(req):
    raise httpx.ReadTimeout("read timed out")


with_transport(refuse)
check("connection refused -> INFRA_OFFLINE", call() is LLMFailure.INFRA_OFFLINE)
with_transport(slow)
check("read timeout -> INFRA_TIMEOUT", call() is LLMFailure.INFRA_TIMEOUT)
with_transport(lambda r: httpx.Response(500, json={
    "error": "model requires more system memory (21.4 GiB) than is available (9.8 GiB)"}))
check("Ollama's memory error -> INFRA_LOAD", call() is LLMFailure.INFRA_LOAD)
with_transport(lambda r: httpx.Response(500, json={
    "error": "timed out waiting for llama runner to start"}))
check("a runner that never started -> INFRA_LOAD", call() is LLMFailure.INFRA_LOAD)
with_transport(lambda r: httpx.Response(404, json={"error": "model 'curatarr-pitcher' not found"}))
check("404 -> MODEL_MISSING", call() is LLMFailure.MODEL_MISSING)
with_transport(lambda r: httpx.Response(400, json={"error": "input length exceeds the context length"}))
check("a context rejection -> CONTEXT_OVERFLOW", call() is LLMFailure.CONTEXT_OVERFLOW)
with_transport(lambda r: httpx.Response(400, json={"error": "invalid format"}))
check("another 4xx -> REQUEST", call() is LLMFailure.REQUEST)
with_transport(lambda r: httpx.Response(200, json={
    "message": {"content": "{}"}, "done_reason": "stop", "prompt_eval_count": 900}))
check("a healthy answer comes back as data", isinstance(call(), dict))
le.httpx.AsyncClient = _RealClient

t = le.timeout_for(300)
check("connect fails fast; only the read may take long",
      t.connect == 10.0 and t.read == 300)
check("every kind has a sentence for the UI",
      all(k in le.DESCRIPTIONS for k in LLMFailure))
check("infra kinds are never repaired", not (le.INFRA & le.REPAIRABLE))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
