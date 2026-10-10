"""Prompts fit the window on purpose; the profile and the bake agree.

    python tests/test_prompt_budget.py

From the 2026-10 pipeline audit:

* nothing enforced a token budget. Ollama truncates an over-long prompt
  silently, from the front — the system prompt — and the chat's watchdog
  only logged. A long debate overflowed principle extraction the same way.
* CURATOR_NUM_CTX and num_gpu=99 were constants: an 8-12 GB card could not
  be configured to work at all.
* the curator bake carried num_ctx 8192 under 16384 requests, and a create
  stream that ended without "success" was reported as built.
* the baked persona contradicted the call sites (pitch length, "the user's
  demand for quality", "a best-effort, rule-based answer").
"""
import asyncio
import json
import re
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

from src.config import settings
from src.services import llm_utils, pillars
from src.services import prompt_budget as pb
from src.services.prompt_budget import Section, est_tokens, fit, fit_messages, keep_tail

ROOT = Path(__file__).resolve().parents[1]

# ── estimates ───────────────────────────────────────────────────────────────

check("the estimate errs toward fitting (3.2 chars/token, not 4)",
      est_tokens("x" * 320) == 101)
check("the budget pays for the answer, the system text and the template",
      pb.budget_for(8192, 800, "x" * 320) == 8192 - 800 - 101 - 64)

# ── fit: what goes first ────────────────────────────────────────────────────


def sections():
    return [
        Section("title", "TITLE: Tokyo Story (1953)\n", 0),
        Section("household", "OTHER HOUSEHOLD USERS:\n  - Ana: completed\n", 0),
        Section("acclaim", "ACCLAIM & METADATA:\n<<<UNTRUSTED_SOURCE:metadata>>>\n"
                           + "acclaim " * 400 + "\n<<<END_UNTRUSTED_SOURCE>>>\n", 2, min_chars=600),
        Section("taste", "OWNER TASTE: " + "taste " * 150 + "\n", 1, min_chars=300),
        Section("signals", "OWNER SIGNAL: kept the franchise\n", 3),
        Section("tech", "TECH: 1080p h264, 9.1 GB\n", 4),
        Section("dialogue", "DIALOGUE: 41 words/min\n", 5),
    ]


full = "".join(s.text for s in sections())
text, touched = fit(sections(), est_tokens(full) + 10)
check("under budget nothing is touched", text == full and touched == [])

text, touched = fit(sections(), est_tokens(full) - 5)
check("a small overrun costs the least valuable section first", touched == ["dialogue"])

text, touched = fit(sections(), 900)
check("acclaim is trimmed before the taste line is touched",
      "acclaim" in touched and "taste" not in touched, touched)
check("...the trim is marked", "…[trimmed]" in text)
check("...and the fence it cut through is closed again",
      text.count("<<<UNTRUSTED_SOURCE:") == text.count("<<<END_UNTRUSTED_SOURCE>>>"))
check("...and every surviving section still starts its own line",
      all(f"\n{label}" in text for label in ("ACCLAIM & METADATA:", "OWNER TASTE:")
          if label in text) and "[trimmed]OWNER" not in text)
check("the result fits", est_tokens(text) <= 900, est_tokens(text))

text, touched = fit(sections(), 60)
check("priority 0 is never cut, whatever the budget",
      "TITLE: Tokyo Story" in text and "Ana: completed" in text)
check("past every floor, whole sections are dropped",
      "OWNER TASTE" not in text and "ACCLAIM" not in text)

check("a cut never leaves half a fence marker",
      not re.search(r"<<<(?!UNTRUSTED_SOURCE:|END_UNTRUSTED_SOURCE>>>)",
                    pb._trim("<<<UNTRUSTED_SOURCE:x>>>\nabc def <<<END_UNTRUSTED_SOURCE>>>", 40)))

# ── fit_messages: chat history ──────────────────────────────────────────────

msgs = ([{"role": "system", "content": "S" * 400}]
        + [{"role": r, "content": f"{r} {i} " + "w" * 300}
           for i in range(6) for r in ("user", "assistant")]
        + [{"role": "user", "content": "the question now"}])
out, dropped = fit_messages(msgs, 400)
check("the system prompt and the current turn always stay",
      out[0]["role"] == "system" and out[-1]["content"] == "the question now")
check("the oldest turns go first", dropped > 0 and "user 5" in out[-3]["content"], dropped)
check("history never starts with an answer whose question was dropped",
      out[1]["role"] == "user")
check("a fitting chat is untouched", fit_messages(msgs, 100000) == (msgs, 0))
mid = ([{"role": "system", "content": "S"}, {"role": "user", "content": "u" * 2000},
        {"role": "assistant", "content": "a" * 2000},
        {"role": "system", "content": "context for the next turn"},
        {"role": "user", "content": "now"}])
out, _ = fit_messages(mid, 50)
check("nothing is reordered: a mid-history system message keeps its place",
      [m["role"] for m in out] == ["system", "system", "user"]
      and out[1]["content"] == "context for the next turn")

# ── keep_tail: the end of a debate is what it settled on ────────────────────

convo = "\n\n".join(f"USER: point {i}" for i in range(500))
tail = keep_tail(convo, 200)
check("a long transcript keeps its newest turns",
      tail.startswith("[earlier turns omitted]") and tail.endswith("USER: point 499"))
check("...within the budget", est_tokens(tail) <= 201)
check("a short one is untouched", keep_tail("USER: hi", 200) == "USER: hi")

# ── the judge's facts are budgeted against the judge's own system prompt ────

import src.database.connection as conn  # noqa: E402
import src.services.episodic_memory as em  # noqa: E402
import src.services.media_enricher as me  # noqa: E402
import src.services.size_norms as sn  # noqa: E402
import src.services.subtitle_signals as ss  # noqa: E402
from contextlib import contextmanager  # noqa: E402


@contextmanager
def _session():
    yield None


async def _none(*a, **k):
    return None


async def _empty(*a, **k):
    return []


async def _long_wiki(*a, **k):
    return "A landmark of postwar cinema. " * 600


conn.get_db_session = _session
pillars._read_db_facts = lambda *a, **k: {
    "ow": None, "ow_unknown": False, "taste_summary": "",
    "others": [{"name": "Ana", "views": 1, "distinct_episodes": 0,
                "completed": True, "last": None}],
    "fb_blob": None}
em.retrieve_considerations = _empty
me.ensure_verified_data = _none
me.fetch_wikipedia_summary = _long_wiki
ss.subtitle_facts = lambda *a, **k: ""
sn.tech_profile_for = lambda **k: None

_orig_ctx = pillars.CURATOR_NUM_CTX
pillars.CURATOR_NUM_CTX = 4096
try:
    ev = asyncio.run(pillars.build_evidence({"title": "Tokyo Story", "year": 1953}, 1, "movie"))
    room = pb.budget_for(4096, pillars._JUDGE_PREDICT_REPAIR + pillars._REPAIR_TURN_TOKENS,
                         pillars.judge_system_prompt(""))
    check("on a short window the facts are trimmed, and say so", ev["trimmed"] != [], ev["trimmed"])
    check("...to the room left by the judge's system prompt and a repair turn",
          est_tokens(ev["facts"]) <= room, (est_tokens(ev["facts"]), room))
    check("...keeping the title and the household line",
          "TITLE: Tokyo Story" in ev["facts"] and "Ana" in ev["facts"])
    check("...with fences balanced",
          ev["facts"].count("<<<UNTRUSTED_SOURCE:") == ev["facts"].count("<<<END_UNTRUSTED_SOURCE>>>"))
    check("the household claim survives budgeting", ev["flags"]["household_claim"] == "Ana")

    # Pre-flight: a caller that skipped build_evidence's budget is refused
    # before Ollama can truncate the constitution away.
    import src.services.llm_errors as le
    calls = []

    async def _never(*a, **k):
        calls.append(1)
        raise AssertionError("must not be called")
    _real_post = le.post_chat
    le.post_chat = _never
    try:
        out = asyncio.run(pillars.adjudicate("x" * 40000, skip_priority=True))
    finally:
        le.post_chat = _real_post
    check("an oversized prompt is CONTEXT_OVERFLOW before any call",
          out["_error_kind"] == "context_overflow" and calls == [])
finally:
    pillars.CURATOR_NUM_CTX = _orig_ctx

# ── profile: context, constitution, placement ───────────────────────────────

_saved = (settings.LLM_PROFILE, settings.CURATOR_NUM_CTX, settings.CURATOR_NUM_GPU)
try:
    settings.LLM_PROFILE, settings.CURATOR_NUM_CTX = "large", 0
    check("large profile: the benchmarked 16k", llm_utils._resolve_num_ctx() == 16384)
    settings.LLM_PROFILE = "small"
    check("small profile: 8k", llm_utils._resolve_num_ctx() == 8192)
    settings.CURATOR_NUM_CTX = 12288
    check("an explicit CURATOR_NUM_CTX wins", llm_utils._resolve_num_ctx() == 12288)
    settings.CURATOR_NUM_CTX = 1024
    check("...but never below what the judge needs", llm_utils._resolve_num_ctx() == 4096)
    settings.LLM_PROFILE = "tiny"
    check("an unknown profile is the large one", llm_utils.llm_profile() == "large")

    settings.LLM_PROFILE = "small"
    small = pillars.judge_system_prompt("")
    settings.LLM_PROFILE = "large"
    large = pillars.judge_system_prompt("")
    check("the small profile's judge gets the compact constitution",
          small.startswith(pillars.PILLAR_CONSTITUTION_COMPACT[:60])
          and large.startswith(pillars.PILLAR_CONSTITUTION[:60]))
    check("...at well under half the tokens",
          est_tokens(pillars.PILLAR_CONSTITUTION_COMPACT) < est_tokens(pillars.PILLAR_CONSTITUTION) / 2)
    cc = pillars.PILLAR_CONSTITUTION_COMPACT
    check("...keeping the law: pillar order, the household rule, the litmus, the NONE rule",
          cc.index("HOUSEHOLD (Pillar III") < cc.index("CUSTODIAN (Pillar II")
          < cc.index("RESONANCE (Pillar I)") < cc.index("EGO (Pillar 0")
          and "2 of 12 episodes" in cc and all(w in cc for w in ("INTENT", "AWE", "RIGOR"))
          and "NONE for CUT, STAGNANT and EVALUATE" in cc
          and "Never argue for deletion because the metadata is wrong" in cc)
    check("both carry the untrusted-data rule",
          "<<<UNTRUSTED_SOURCE" in small and "<<<UNTRUSTED_SOURCE" in large)

    settings.CURATOR_NUM_GPU = 99
    check("the curator is pinned to the card by default",
          llm_utils.curator_options()["options"]["num_gpu"] == 99)
    settings.CURATOR_NUM_GPU = -1
    o = llm_utils.curator_options()["options"]
    check("CURATOR_NUM_GPU=-1 leaves the split to Ollama", "num_gpu" not in o and "num_thread" not in o)
    check("...and the judge follows the same setting", pillars.curator_gpu() == {})
finally:
    settings.LLM_PROFILE, settings.CURATOR_NUM_CTX, settings.CURATOR_NUM_GPU = _saved

# ── the bake ────────────────────────────────────────────────────────────────

import src.services.embed_service as es  # noqa: E402
import src.services.setup_wizard as sw  # noqa: E402

CREATES = []
FINISH = {"success": True}


def _handler(request):
    body = json.loads(request.content)
    CREATES.append(body)
    lines = [{"status": "creating"}] + ([{"status": "success"}] if FINISH["success"] else [])
    return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines))


async def _exists(*a, **k):
    return True


_RealClient = httpx.AsyncClient
es.effective_embedding_model = lambda: ""
sw.model_exists = _exists
sw.httpx.AsyncClient = lambda **kw: _RealClient(transport=httpx.MockTransport(_handler), **kw)
try:
    res = asyncio.run(sw.build_ollama_models("http://ollama:11434", "base-c", "base-s",
                                             base_pitcher="base-p"))
    by = {c["model"]: c for c in CREATES}
    check("the curator bake carries the request window",
          by["curatarr-curator"]["parameters"]["num_ctx"] == llm_utils.CURATOR_NUM_CTX)
    check("...and so does the pitcher",
          by["curatarr-pitcher"]["parameters"]["num_ctx"] == llm_utils.CURATOR_NUM_CTX)
    check("the summariser keeps its 8192 (its calls never pass num_ctx)",
          by["curatarr-summarizer"]["parameters"]["num_ctx"] == 8192)
    check("both the current and the legacy create field are sent",
          all(c["model"] == c["name"] for c in CREATES))
    check("a finished stream is a build", res["curator"] and res["summarizer"] and res["pitcher"])
    FINISH["success"] = False
    res = asyncio.run(sw.build_ollama_models("http://ollama:11434", "base-c", "base-s"))
    check("a stream that never said success is NOT a build", res["curator"] is False)
finally:
    sw.httpx.AsyncClient = _RealClient

persona = sw.CURATOR_SYSTEM_PROMPT
check("the persona no longer fixes a pitch length the call sites contradict",
      "1-2 sentences" not in persona)
check("...nor attributes demands to the user (the monologue's recitation rule)",
      "demand for quality" not in persona and "Never attribute standards" in persona)
check("...nor invites a 'best-effort' answer on missing data",
      "rule-based answer" not in persona and "never fill the gap with invented detail" in persona)

# ── the VRAM fit warning ────────────────────────────────────────────────────

from src.services.model_check import vram_fit_warning  # noqa: E402

GB = 1024 ** 3
check("the measured 4090 setup (19.9 GB at 16k on 24 GB) passes",
      vram_fit_warning("c", int(19.9 * GB), 24564, 16384) == "")
w = vram_fit_warning("curatarr-curator", int(19.9 * GB), 12288, 16384)
check("the same curator on a 12 GB card is warned about, with the ways out",
      "12.0 GB" in w and "LLM_PROFILE=small" in w and "CURATOR_NUM_GPU=-1" in w, w)
check("an 8B Q4 at 8k on an 8 GB card passes",
      vram_fit_warning("c", int(4.9 * GB), 8192, 8192) == "")
check("no reading, no warning", vram_fit_warning("c", None, 8192, 8192) == ""
      and vram_fit_warning("c", int(GB), None, 8192) == "")
bm = (ROOT / "build_models.py").read_text(encoding="utf-8")
check("build_models --check prints the warning", "curator_fit_warning()" in bm)

# ── chat ────────────────────────────────────────────────────────────────────

chat = (ROOT / "src/routers/chat.py").read_text(encoding="utf-8")
check("the chat fits its messages before streaming",
      chat.index("fit_messages(messages, budget_for(CURATOR_NUM_CTX, _CHAT_PREDICT))")
      < chat.index('"messages": messages,\n                        "stream": True'))
check("the answer's room shrinks with the window",
      "_CHAT_PREDICT = min(4096, CURATOR_NUM_CTX // 4)" in chat
      and "num_predict=_CHAT_PREDICT" in chat)
cp = (ROOT / "src/services/curator_principles.py").read_text(encoding="utf-8")
check("principle extraction keeps the newest turns of a long debate",
      cp.index("keep_tail(convo") < cp.index('_curator_json(_EXTRACT_SYS, "DEBATE:\\n" + convo'))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
