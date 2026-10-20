"""Setup mini-bench: does the chosen curator really run well on THIS machine?

The hardware matrix (model_matrix.json) was measured on the bench card and
other cards are an estimate: a busy home server measured 2-3.7x slower
judge verdicts than the bandwidth ratio predicts (RTX PRO 2000, 2026-10-19).
So after the bake the wizard runs the curator through the same calls
production makes, on two fixed titles, for about a minute:

* the warm-up (load, GPU residency, ``setup_wizard.warmup_check``);
* two deletion-judge verdicts: the production system prompt and JSON schema
  (``pillars.judge_system_prompt`` / ``VERDICT_SCHEMA``), validated like
  production (``pillars._parse_verdict``);
* one card note (the monologue path).

The titles belong to a FICTIONAL owner, Jane Doe: no user's taste, plays or
signals ever reach this check. One title is real (public metadata only), one
is invented — the timing is what is measured, not the verdict.

Settings: the bench runs with the package's num_ctx / num_gpu when the
matrix names one for this card, so a 6 GB card is measured with the layer
split it will really use. The KV-cache type is an Ollama SERVER setting the
wizard cannot change from here; the result names it when the package wants
one.
"""
from __future__ import annotations

import logging
import statistics
import time

import httpx

logger = logging.getLogger(__name__)

# A recommendation is comfortable below this many seconds per judge verdict
# (hwbench.matrix.COMFORT_JUDGE_S — the same line the matrix draws).
COMFORT_JUDGE_S = 20.0
# Less than this left free on the card after the load: the card is full. That
# alone is no spill - gemma4:31b leaves ~200 MiB on a 24 GB card at full
# speed (2026-10-20) - but with the speed well under what the bench measured
# it is: the driver is paging into shared memory (Windows "sysmem fallback").
CARD_FULL_MIB = 384
SPILL_SPEED_RATIO = 0.6

JANE_DOE_TASTE = (
    "OWNER TASTE: Jane Doe likes slow-burn horror and social satire, values sharp, "
    "economical filmmaking over spectacle, and has little patience for formulaic sequels "
    "or long-running sitcoms.")

FIXTURES = [
    ("movie:Night of the Living Dead", """TITLE: Night of the Living Dead (1968) — movie, Horror
OWNER: not watched by the owner.
OTHER HOUSEHOLD USERS:
  none have watched it.
ACCLAIM & METADATA:
[VERIFIED DATA — reason ONLY from the facts below; do NOT add plot points, people, awards, year, or franchise context not listed here]
<<<UNTRUSTED_SOURCE:metadata>>>
  Type: Film
  Title: Night of the Living Dead
  Year: 1968
  Genres: Horror
  Director: George A. Romero
  Cast: Duane Jones, Judith O'Dea
  Themes: siege, social breakdown, survival, racial tension
  Mood: tense, bleak
  Runtime: 96 min
  Significance: Selected for the US National Film Registry; widely credited with founding the modern zombie film. The film is in the public domain.
  Plot: Strangers take shelter in a rural farmhouse while the recently dead rise and lay siege to it.
<<<END_UNTRUSTED_SOURCE>>>
""" + JANE_DOE_TASTE + """
TECH: 1080 h264, 6.9 GB, 72 MB/min, 1.31x class median (normal)"""),
    ("movie:The Example Sequel", """TITLE: The Example Sequel (2014) — movie, Comedy
OWNER: not watched by the owner.
OTHER HOUSEHOLD USERS:
  none have watched it.
ACCLAIM & METADATA:
[VERIFIED DATA — reason ONLY from the facts below; do NOT add plot points, people, awards, year, or franchise context not listed here]
<<<UNTRUSTED_SOURCE:metadata>>>
  Type: Film
  Title: The Example Sequel
  Year: 2014
  Genres: Comedy
  Themes: road trip, reunion, slapstick
  Mood: light-hearted
  Runtime: 101 min
  Rating: 4.9/10
  Community reception: Reviewers call it a by-the-numbers sequel that repeats the first film's jokes.
<<<END_UNTRUSTED_SOURCE>>>
""" + JANE_DOE_TASTE + """
TECH: 1080 h264, 14.2 GB, 140 MB/min, 2.40x class median (bloated)"""),
]


def _is_local(endpoint: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(endpoint).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1")


def _options(num_ctx: int | None, num_gpu: int | None) -> dict:
    from src.services.llm_utils import CURATOR_NUM_CTX, curator_gpu
    opts = {"num_ctx": num_ctx or CURATOR_NUM_CTX}
    if num_gpu is None:
        opts.update(curator_gpu())
    elif num_gpu >= 0:
        opts["num_gpu"] = num_gpu
    # num_gpu -1: leave the split to Ollama (the package's offloaded tiers).
    return opts


async def _timed_chat(client: httpx.AsyncClient, endpoint: str, payload: dict) -> dict:
    t0 = time.monotonic()
    r = await client.post(f"{endpoint}/api/chat", json={**payload, "stream": False, "think": False})
    r.raise_for_status()
    d = r.json()
    out = {"wall_s": round(time.monotonic() - t0, 2),
           "content": (d.get("message") or {}).get("content", "") or ""}
    if d.get("eval_count") and d.get("eval_duration"):
        out["gen_tok_s"] = round(d["eval_count"] / (d["eval_duration"] / 1e9), 1)
    if d.get("prompt_eval_count") and d.get("prompt_eval_duration"):
        out["prefill_tok_s"] = round(d["prompt_eval_count"] / (d["prompt_eval_duration"] / 1e9))
    return out


def classify(warm: dict, judges: list[dict], card_free_mib: int | None,
             gen_tok_s: float | None = None, expected_gen_tok_s: float | None = None
             ) -> tuple[str, str]:
    """(verdict, one-line reason) from the measurements. Pure, so the rules
    are testable without a model: spill > unreliable > slow > ok."""
    if warm.get("verdict") == "cpu_spill":
        return "spill", f"{warm.get('cpu_percent')} % of the curator runs on the CPU"
    full = card_free_mib is not None and card_free_mib < CARD_FULL_MIB
    if full and gen_tok_s and expected_gen_tok_s and gen_tok_s < SPILL_SPEED_RATIO * expected_gen_tok_s:
        return "spill", (f"the card is full ({card_free_mib} MiB left) and it writes "
                         f"{gen_tok_s} tok/s where the bench expects ~{expected_gen_tok_s} - "
                         "the driver is spilling into system RAM")
    valid = [j for j in judges if j.get("valid")]
    if not valid:
        return "unreliable", "no judge verdict passed the schema check"
    walls = [j["wall_s"] for j in judges if "wall_s" in j]
    med = statistics.median(walls) if walls else None
    if med is not None and med > COMFORT_JUDGE_S:
        return "slow", f"{med:.0f} s per judge verdict (comfortable: under {COMFORT_JUDGE_S:.0f} s)"
    if len(valid) < len(judges):
        return "ok", "one of two verdicts needed the schema repair production would run"
    if full:
        return "ok", (f"the card is nearly full ({card_free_mib} MiB left) - fine at this speed, "
                      "but a game or a longer context will push it into system RAM")
    return "ok", ""


async def run_minibench(endpoint: str, model: str, *, num_ctx: int | None = None,
                        num_gpu: int | None = None,
                        expected_gen_tok_s: float | None = None) -> dict:
    """Measure ``model`` (the baked curator) on this Ollama. Never raises."""
    from src.services.pillars import VERDICT_SCHEMA, _parse_verdict, judge_system_prompt
    from src.services.setup_wizard import detect_gpu, tls_context, warmup_check
    warm = await warmup_check(endpoint, model)
    if not warm.get("ok"):
        return {"ok": False, "error": warm.get("error") or "the warm-up did not run",
                "warmup": warm}
    opts = _options(num_ctx, num_gpu)
    judges, note = [], {}
    try:
        timeout = httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=10.0)
        async with httpx.AsyncClient(verify=tls_context(), timeout=timeout) as client:
            for key, facts in FIXTURES:
                j = await _timed_chat(client, endpoint, {
                    "model": model, "format": VERDICT_SCHEMA, "keep_alive": "5m",
                    "messages": [{"role": "system", "content": judge_system_prompt()},
                                 {"role": "user", "content": "FACTS:\n" + facts}],
                    "options": {**opts, "temperature": 0.0, "seed": 7,
                                "repeat_penalty": 1.0, "num_predict": 800}})
                try:
                    j["verdict"] = _parse_verdict(j.pop("content")).verdict
                    j["valid"] = True
                except Exception as e:      # a schema miss is a RESULT here
                    j["valid"], j["error"] = False, str(e)[:120]
                j["title"] = key
                judges.append(j)
            note = await _timed_chat(client, endpoint, {
                "model": model, "keep_alive": "5m",
                "messages": [{"role": "user", "content":
                              "Write the 2-3 sentence card note for this title in your own "
                              "voice. No headers, no lists.\n\n" + FIXTURES[0][1]}],
                "options": {**opts, "temperature": 0.7, "num_predict": 300}})
            note.pop("content", None)
    except Exception as e:
        logger.info("[setup] mini-bench failed: %s: %s", type(e).__name__, e)
        return {"ok": False, "error": "The mini-bench did not complete - the log has the detail",
                "warmup": warm, "judges": judges}
    # nvidia-smi sees the Curatarr host's card: only meaningful when Ollama
    # runs on this machine too.
    free_mib = None
    if _is_local(endpoint):
        gpu = await detect_gpu()
        if gpu.get("ok") and len(gpu.get("gpus") or []) == 1:
            free_mib = int(gpu["gpus"][0]["free_gb"] * 1024)
    gens = [x["gen_tok_s"] for x in judges + [note] if x.get("gen_tok_s")]
    gen = round(statistics.median(gens), 1) if gens else None
    verdict, reason = classify(warm, judges, free_mib, gen, expected_gen_tok_s)
    walls = [j["wall_s"] for j in judges]
    return {"ok": True, "verdict": verdict, "reason": reason, "warmup": warm,
            "judges": judges, "note": note, "options": opts,
            "judge_s_median": round(statistics.median(walls), 1) if walls else None,
            "gen_tok_s": gen,
            "card_free_mib": free_mib}
