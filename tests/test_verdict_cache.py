"""The deletion verdict reserve (2026-10-22, NAS move phase 2, PR 4c).

A verdict is stored under a hash of everything that decides it - bake,
weights, the judging host's profile and window, the system prompt with its
principles, the evidence - and reused while that hash holds: temperature 0
and a fixed seed give the same answer, so paying the GPU again bought
nothing. Reused verdicts do not spend the run's cap, so a run reaches
further down the ranking; on a split install (NAS + gaming PC) runs are
three times deeper on top, building a reserve that lasts while the PC is off.

A throwaway cache file; no model.

    python tests/test_verdict_cache.py
"""
import asyncio
import dataclasses
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from src.config import settings  # noqa: E402

_tmp = tempfile.mkdtemp()
settings.ENRICHMENT_CACHE = str(Path(_tmp) / "enrichment.db")   # never the real cache

from src.services import verdict_cache as vc  # noqa: E402
from src.services.ollama_hosts import OllamaHost  # noqa: E402

from _checks import check  # noqa: E402


PC = OllamaHost(url="http://pc:11434", profile="large", num_ctx=16384, gpu={"num_gpu": 99})
FACTS = "TITLE: Some Film (2001) — movie, Drama\nOWNER: never watched.\n"
k = vc.key(FACTS, "", "curatarr-pitcher", "abc123", PC)

# ── what the identity covers ────────────────────────────────────────────────
check("the same inputs give the same identity", k == vc.key(FACTS, "", "curatarr-pitcher", "abc123", PC))
check("a changed fact (a play, a rating, a household watch) is a new verdict",
      k != vc.key(FACTS.replace("never watched", "watched once"), "", "curatarr-pitcher", "abc123", PC))
check("a new principle is a new verdict",
      k != vc.key(FACTS, "PRINCIPLE: keep cult classics", "curatarr-pitcher", "abc123", PC))
check("other weights (an update under the same name) are a new verdict",
      k != vc.key(FACTS, "", "curatarr-pitcher", "def456", PC))
check("another bake is a new verdict", k != vc.key(FACTS, "", "curatarr-curator", "abc123", PC))
check("another host profile or window is a new verdict (small model, compact constitution)",
      k != vc.key(FACTS, "", "curatarr-pitcher", "abc123", dataclasses.replace(PC, profile="small"))
      and k != vc.key(FACTS, "", "curatarr-pitcher", "abc123", dataclasses.replace(PC, num_ctx=8192)))

# ── store and reuse ─────────────────────────────────────────────────────────
check("nothing stored yet", vc.get_verdict(k) is None)
keep = {"verdict": "KEEP_WITH_FLAG", "protecting_pillar": "EGO", "pillar_0_ego": "fits"}
check("a real verdict is stored", vc.put_verdict(k, keep))
got = vc.get_verdict(k)
check("...and comes back marked as reused", got and got["verdict"] == "KEEP_WITH_FLAG" and got["_cached"])
check("an outage is never stored",
      not vc.put_verdict("other", {"verdict": "EVALUATE", "_error": "x", "_error_kind": "infra_offline"})
      and vc.get_verdict("other") is None)
check("an empty answer is never stored", not vc.put_verdict("other", {}) and not vc.put_verdict("other", None))
vc.put_verdict(k, got)
check("re-storing a reused verdict does not store the marker", "_cached" not in vc._get(f"verdict:{k}"))

vc.put_pitch(k, "lang:de|", "Ein bissiger Satz.")
check("a pitch comes back for the same language and guard", vc.get_pitch(k, "lang:de|") == "Ein bissiger Satz.")
check("...not for another language or a floored CUT",
      vc.get_pitch(k, "lang:en|") is None and vc.get_pitch(k, "lang:de|household_floor") is None)
vc.put_pitch(k, "lang:fr|", "  ")
check("an empty pitch is never stored", vc.get_pitch(k, "lang:fr|") is None)

# ── the bake's digest ───────────────────────────────────────────────────────
import src.services.pc_guard as pg  # noqa: E402
_real = pg.ollama_client


def _tags(request):
    return httpx.Response(200, json={"models": [
        {"name": "curatarr-pitcher:latest", "digest": "22130167c4c2deadbeef"},
        {"name": "gemma4:31b", "digest": "34095ebb7f49"}]})


pg.ollama_client = lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(_tags))
try:
    check("the digest of the bake is read from /api/tags (short)",
          asyncio.run(vc.bake_digest("http://pc:11434", "curatarr-pitcher")) == "22130167c4c2deadbeef"[:16])
    check("an unknown bake has no digest", asyncio.run(vc.bake_digest("http://pc:11434", "nope")) == "")
finally:
    pg.ollama_client = _real

# ── the run ─────────────────────────────────────────────────────────────────
from src.services import recommendations_engine as re_  # noqa: E402
saved = settings.OLLAMA_PC_ENDPOINT
try:
    settings.OLLAMA_PC_ENDPOINT = ""
    check("not split: the classic 10 proposals / 60 verdicts", re_._run_caps() == (10, 60))
    settings.OLLAMA_PC_ENDPOINT = "http://pc:11434"
    check("split: three times deeper while the PC is free - the reserve", re_._run_caps() == (30, 180))
finally:
    settings.OLLAMA_PC_ENDPOINT = saved

src = (ROOT / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
loop = src[src.index("async def generate_deletion_proposals("):]
check("the stored verdict is consulted before the model",
      loop.index("verdict_cache.get_verdict, _vkey") < loop.index("verdict = await adjudicate("))
check("...a reused one does not spend the cap (up to REUSE_FACTOR x)",
      "if reused <= judge_cap * REUSE_FACTOR:\n                            judged -= 1" in loop)
check("...a new one is stored", loop.index("verdict = await adjudicate(")
      < loop.index("verdict_cache.put_verdict, _vkey, verdict"))
check("the pitch is reused per language and guard",
      "verdict_cache.get_pitch, _vkey, _variant" in loop
      and "_variant = f\"{lang_directive_str}|{verdict.get('_guard') or ''}\"" in loop)
check("the weights' digest is read once per run", loop.count("verdict_cache.bake_digest(") == 1)
check("every title the run can reach has its keep-value scored, and it never goes further",
      "_consider_slice = scored_candidates[:_reach]" in loop and "or _ci >= _reach):" in loop)

check.done()
