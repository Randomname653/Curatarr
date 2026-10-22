"""The model judges its own update: old vs new, blind, against the facts.

After a comparison (model_updates.run_comparison) the CURRENT base model —
the pinned weights, without the curatarr persona — reads, for every title,
the facts plus both verdicts and card notes and says which is better, and
it reads the three chat answers side by side. It is the model the owner
already trusts, and large enough for a rubric read; it is also judging
itself against its successor, so it may prefer its own style. Countered:

* blind: the two sides are only A and B;
* both orders: every pair is judged twice with A/B swapped; a preference
  counts only when it survives the swap, otherwise it is a tie;
* grounded: notes are checked against the facts the verdicts came from.

The measurable things are not the model's to decide: more failures, a
spill off the GPU, or a clearly slower generation are hard rules in
Python (``guards``) that turn any "update" into "stay", with the reason.
The model then writes one recommendation over the tallies; the guards
overrule it, never the other way round.
"""
from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger(__name__)

PAIR_SYSTEM = (
    "You compare two answers of a media-curation model for ONE title. Use ONLY the FACTS. "
    "Better means: the verdict follows from the facts, the note says nothing the facts do not "
    "support, it is specific to this title, and it reads as a sharp 2-3 sentence card note. "
    "Judge content, not length or style you happen to like. If neither is clearly better, say tie."
)
CHAT_SYSTEM = (
    "You compare two answers of a media-curator persona to the same question. Better means: "
    "honest, specific, no invented facts, answers what was asked, in the language asked. "
    "Judge content, not length. If neither is clearly better, say tie."
)
FINAL_SYSTEM = (
    "You advise the owner of a home media server whether to switch the curator model to new "
    "weights. You get measured numbers and the tallies of a blind side-by-side review in which "
    "OLD and NEW were judged per title. Recommend update, stay or either, in two sentences, "
    "naming the deciding numbers. Never contradict a listed hard rule."
)
PAIR_SCHEMA = {
    "type": "object",
    "properties": {"better": {"type": "string", "enum": ["A", "B", "tie"]},
                   "reason": {"type": "string"}},
    "required": ["better", "reason"],
}
FINAL_SCHEMA = {
    "type": "object",
    "properties": {"recommendation": {"type": "string", "enum": ["update", "stay", "either"]},
                   "reason": {"type": "string"}},
    "required": ["recommendation", "reason"],
}

# Hard rules (measured, not judged).
MAX_SLOWDOWN = 0.75      # new generation below 75 % of the old tok/s
MIN_GPU_SHARE = 100      # the new weights must sit fully on the GPU


def guards(summary: dict) -> list[str]:
    out = []
    f = summary.get("failures") or {}
    if (f.get("new") or 0) > (f.get("old") or 0):
        out.append(f"the new weights failed {f['new']} judge calls, the old {f.get('old', 0)}")
    share = (summary.get("gpu_share_pct") or {}).get("new")
    if share is not None and share < MIN_GPU_SHARE:
        out.append(f"the new weights run only {share} % on the GPU (CPU spill)")
    tok = summary.get("gen_tok_s") or {}
    if (not summary.get("speed_unstable") and tok.get("old") and tok.get("new")
            and tok["new"] < MAX_SLOWDOWN * tok["old"]):
        out.append(f"generation dropped from {tok['old']} to {tok['new']} tok/s")
    return out


def _new_is_a(key: str) -> bool:
    """Which side is A for the first read — seeded by the title, so a
    re-run is reproducible and NEW is not always in the same slot."""
    return hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).digest()[0] % 2 == 0


def _side(text: str, verdict: str | None) -> str:
    return (f"VERDICT: {verdict}\n" if verdict else "") + f"NOTE: {text or '(empty)'}"


async def _ask(endpoint: str, model: str, system: str, user: str, schema: dict) -> dict:
    from src.services.llm_errors import post_chat
    from src.services.llm_utils import CURATOR_NUM_CTX, curator_gpu
    data = await post_chat(endpoint, {
        "model": model, "stream": False, "think": False, "keep_alive": "10m",
        "format": schema,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"temperature": 0.0, "seed": 7, "repeat_penalty": 1.0, "num_predict": 300,
                    "num_ctx": CURATOR_NUM_CTX, **curator_gpu()}}, read_timeout=300.0)
    return json.loads((data.get("message") or {}).get("content") or "{}")


async def judge_pair(endpoint: str, model: str, key: str, system: str, context: str,
                     old: str, new: str) -> dict:
    """Both orders; the winner is NEW/OLD only when both reads agree."""
    from src.services.llm_priority import extend_bulk_hold
    extend_bulk_hold(900)     # a long review must not let enrichment in
    picks, reasons, unreadable = [], [], 0
    first_new_a = _new_is_a(key)
    for new_is_a in (first_new_a, not first_new_a):
        a, b = (new, old) if new_is_a else (old, new)
        try:
            r = await _ask(endpoint, model, system,
                           f"{context}\n\nANSWER A:\n{a}\n\nANSWER B:\n{b}", PAIR_SCHEMA)
        except Exception as e:   # one unreadable answer is a tie, not a crash
            logger.info("[model eval] %s: %s", key, e)
            unreadable += 1
            r = {"better": "tie", "reason": f"unreadable ({type(e).__name__})"}
        better = r.get("better")
        picks.append("tie" if better not in ("A", "B") else
                     ("new" if (better == "A") == new_is_a else "old"))
        reasons.append(r.get("reason", "")[:300])
    winner = picks[0] if picks[0] == picks[1] else "tie"
    if unreadable:
        # Not a draw: a dead judge made every pair a "tie" and the verdict
        # "either", which read exactly like two equally good models.
        winner = "unreadable"
    return {"key": key, "winner": winner, "reads": picks, "reasons": reasons,
            "position_flip": picks[0] != picks[1] and "tie" not in picks,
            "unreadable": unreadable}


def _gb(v) -> str:
    # An unknown figure is said to be unknown — never handed over as a number
    # the reviewer could build an argument on.
    return "not measurable" if v is None else str(v)


def _numbers(s: dict, t: dict, c: dict, hard: list[str], titles: list[dict]) -> list[str]:
    """What the reviewer gets for its recommendation, one line per fact.
    A figure that cannot be trusted is described, never handed over as a
    number: the reviews of 2026-10-15 argued from a misread VRAM, then from
    per-title times taken next to another model, then from an 11 tok/s
    outlier."""
    reads = s.get("gen_tok_s_reads") or {}
    tok = s.get("gen_tok_s") or {}
    vram = s.get("vram_gb") or {}
    judge = s.get("judge_s") or {}
    fails = s.get("failures") or {}
    return [
        f"Titles: NEW better {t['new']}, OLD better {t['old']}, tie {t['tie']}, "
        f"unreadable {t.get('unreadable', 0)} ({t['position_flips']} flipped with the order).",
        f"Chat: NEW {c['new']}, OLD {c['old']}, tie {c['tie']}, unreadable {c.get('unreadable', 0)}.",
        f"Verdicts unchanged {s.get('verdicts_same')}/{s.get('verdicts_compared')}.",
        ("Per-title times: not comparable (taken while another model shared the card)."
         if s.get("timings_contaminated") else
         f"Judge seconds per title old/new: {judge.get('old')}/{judge.get('new')}."),
        (f"Speed: unstable on this machine (old reads {reads.get('old')}, new reads "
         f"{reads.get('new')} tok/s) - do not decide on speed."
         if s.get("speed_unstable") else
         f"Speed tok/s old/new: {tok.get('old')}/{tok.get('new')} (median of interleaved reads)."),
        f"VRAM GB old/new: {_gb(vram.get('old'))}/{_gb(vram.get('new'))}.",
        f"Failures old/new: {fails.get('old')}/{fails.get('new')}.",
        f"Hard rules violated: {'; '.join(hard) if hard else 'none'}.",
        "Per-title reasons:",
        *[f"- {r['key']}: {r['winner']} - {r['reasons'][0]}" for r in titles],
    ]


async def evaluate(comparison: dict, endpoint: str, judge_model: str) -> dict:
    facts = comparison.get("facts") or {}
    titles = []
    for a, b in zip(comparison["old"]["titles"], comparison["new"]["titles"]):
        if a.get("error") or b.get("error"):
            continue
        ctx = f"FACTS:\n{facts.get(a['title'], '(facts not kept for this run)')}"
        titles.append(await judge_pair(
            endpoint, judge_model, a["title"], PAIR_SYSTEM, ctx,
            _side(a.get("monologue"), a.get("verdict")),
            _side(b.get("monologue"), b.get("verdict"))))
    chats = []
    for a, b in zip(comparison["old"]["chat"], comparison["new"]["chat"]):
        chats.append(await judge_pair(endpoint, judge_model, a["prompt"], CHAT_SYSTEM,
                                      f"QUESTION: {a['prompt']}", a["text"], b["text"]))

    def tally(rows):
        return {k: sum(r["winner"] == k for r in rows)
                for k in ("new", "old", "tie", "unreadable")} | {
            "position_flips": sum(r["position_flip"] for r in rows)}

    t, c = tally(titles), tally(chats)
    hard = guards(comparison["summary"])
    s = comparison["summary"]
    numbers = "\n".join(_numbers(s, t, c, hard, titles))
    judged = len(titles) + len(chats)
    dead = t["unreadable"] + c["unreadable"]
    if judged and dead * 2 > judged:
        logger.warning("[model eval] %d of %d judgements unreadable - no verdict", dead, judged)
        final = {"recommendation": "no verdict",
                 "reason": f"{dead} of {judged} judgements were unreadable - the judge did not answer"}
    else:
        try:
            final = await _ask(endpoint, judge_model, FINAL_SYSTEM, numbers, FINAL_SCHEMA)
        except Exception as e:
            logger.warning("[model eval] final recommendation failed: %s", e, exc_info=True)
            final = {"recommendation": "no verdict",
                     "reason": f"no recommendation ({type(e).__name__})"}
    rec = final.get("recommendation")
    overruled = bool(hard) and rec == "update"
    return {
        "judge_model": judge_model,
        "titles": titles, "chat": chats,
        "tally": {"titles": t, "chat": c},
        "guards": hard,
        "model_recommendation": rec,
        "model_reason": final.get("reason", "")[:600],
        "recommendation": "stay" if overruled else rec,
        "overruled": overruled,
        "self_judged": True,
    }
