"""Quality phase for one model x LLM_PROFILE, through PRODUCTION code.

Runs as a child process (``run_child``) because LLM_PROFILE, CURATOR_NUM_CTX
and OLLAMA_ENDPOINT are read when src.config / llm_utils import — the
parent sets them in the child's environment, so the child's adjudicate and
write_monologue are byte-for-byte the deletion run's own, pointed at the
bench server and at a persona bake of the candidate.

Phases (each optional):
  gate   the five synthetic stress cases of tests/pillar_json_stresstest.py
         x 3 iterations through pillars.adjudicate: valid / repaired /
         failed / deterministic, plus the hit rate on the expected verdicts.
         NOTE: those expectations were written for the THREE-pillar draft;
         they are reported, not gated on, until one incumbent run under the
         four-pillar constitution confirms or re-labels them.
  judge  adjudicate over the frozen evidence (subset or full): verdict,
         protecting pillar, repaired/failed, latency per title. Agreement
         with the reference model is computed later by matrix.py.
  mono   write_monologue for every judged title: text, latency, how often
         the production violation check forced a retry, violations left,
         buzzwords, sentence count.
  chat   the decider cases of curator_test_prompts.json (sycophancy,
         confabulation traps, German) with the production persona.

Every output row goes to a JSONL artifact for scoring; the child prints a
metrics JSON as its last stdout line for the parent to put in the ledger.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
CHAT_CASES = HERE.parent / "curator_test_prompts.json"
DECIDER_CASES = ("mt_fallout", "mt_fnaf", "halu_001", "halu_002", "halu_003",
                 "halu_004", "tma_004", "de_004", "pers_001")
BUZZWORDS = ("high-octane", "epic", "relentless", "mind-bending", "adrenaline",
             "masterpiece", "visceral", "edge-of-your-seat")


def buzzwords(text: str) -> int:
    t = (text or "").lower()
    return sum(len(re.findall(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", t))
               for w in BUZZWORDS)


def sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if s])


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


async def _gate(model: str, out: Path) -> dict:
    sys.path.insert(0, str(REPO / "tests"))
    from pillar_json_stresstest import CASES
    from src.services import pillars
    rows = []
    for name, facts, expected in CASES:
        for it in range(3):
            t0 = time.perf_counter()
            v = await pillars.adjudicate(facts, model=model, skip_priority=True)
            rows.append({"case": name, "iter": it, "expected": expected,
                         "verdict": v.get("verdict"), "pillar": v.get("protecting_pillar"),
                         "repaired": bool(v.get("_repaired")),
                         "error_kind": v.get("_error_kind"),
                         "wall_s": round(time.perf_counter() - t0, 2)})
    _write(out, rows)
    n = len(rows)
    by_case = {}
    for r in rows:
        by_case.setdefault(r["case"], set()).add(r["verdict"])
    ok = [r for r in rows if not r["error_kind"]]
    return {
        "gate_n": n,
        "gate_valid_pct": round(100 * len(ok) / n, 1),
        "gate_repaired_pct": round(100 * sum(r["repaired"] for r in rows) / n, 1),
        "gate_deterministic_pct": round(100 * sum(len(v) == 1 for v in by_case.values()) / len(by_case), 1),
        "gate_expected_hit_pct": round(100 * sum(r["verdict"] == r["expected"] for r in ok) / n, 1),
        "gate_wall_s_median": round(statistics.median(r["wall_s"] for r in rows), 2),
    }


async def _judge_and_mono(model: str, snap: dict, keys: list[str], out_j: Path,
                          out_m: Path, do_mono: bool) -> dict:
    from src.services import pillars
    retries = Counter()
    real_check = pillars._monologue_violations

    def counting_check(text, kind):
        hits = real_check(text, kind)
        retries["checked"] += 1
        if hits:
            retries["flagged"] += 1
        return hits

    # write_monologue looks the checker up as a module global at call time.
    pillars._monologue_violations = counting_check
    items = {i["key"]: i for i in snap["items"]}
    jrows, mrows = [], []
    for key in keys:
        it = items[key]
        t0 = time.perf_counter()
        v = await pillars.adjudicate(it["facts"], model=model, skip_priority=True,
                                     law_extra=it.get("law_extra", ""))
        jrows.append({"key": key, "category": it["category"], "title": it["title"],
                      "verdict": v.get("verdict"), "pillar": v.get("protecting_pillar"),
                      "repaired": bool(v.get("_repaired")), "error_kind": v.get("_error_kind"),
                      "wall_s": round(time.perf_counter() - t0, 2), "raw": v})
        if do_mono and not v.get("_error_kind"):
            before = dict(retries)
            t0 = time.perf_counter()
            text = await pillars.write_monologue(it["facts"], v, model=model,
                                                 lang_directive=snap.get("lang_directive", ""),
                                                 skip_priority=True)
            left = real_check(text, v.get("verdict"))
            mrows.append({"key": key, "category": it["category"], "title": it["title"],
                          "verdict": v.get("verdict"), "text": text,
                          "wall_s": round(time.perf_counter() - t0, 2),
                          "retried": retries["flagged"] > before.get("flagged", 0),
                          "violations_left": left, "buzzwords": buzzwords(text),
                          "sentences": sentences(text), "empty": not text.strip()})
    pillars._monologue_violations = real_check
    _write(out_j, jrows)
    if mrows:
        _write(out_m, mrows)
    n = len(jrows)
    ok = [r for r in jrows if not r["error_kind"]]
    m = {
        "judge_n": n,
        "judge_valid_pct": round(100 * len(ok) / n, 1) if n else None,
        "judge_repaired_pct": round(100 * sum(r["repaired"] for r in jrows) / n, 1) if n else None,
        "judge_fail_kinds": dict(Counter(r["error_kind"] for r in jrows if r["error_kind"])),
        "judge_verdicts": dict(Counter(r["verdict"] for r in ok)),
        "judge_wall_s_median": round(statistics.median(r["wall_s"] for r in jrows), 2) if n else None,
    }
    if mrows:
        k = len(mrows)
        m.update({
            "mono_n": k,
            "mono_retry_pct": round(100 * sum(r["retried"] for r in mrows) / k, 1),
            "mono_violations_left_pct": round(100 * sum(bool(r["violations_left"]) for r in mrows) / k, 1),
            "mono_empty_pct": round(100 * sum(r["empty"] for r in mrows) / k, 1),
            "mono_buzzwords": sum(r["buzzwords"] for r in mrows),
            "mono_sentences_median": statistics.median(r["sentences"] for r in mrows),
            "mono_over_3_sentences_pct": round(100 * sum(r["sentences"] > 3 for r in mrows) / k, 1),
            "mono_wall_s_median": round(statistics.median(r["wall_s"] for r in mrows), 2),
        })
    return m


async def _chat(model: str, out: Path, case_ids: tuple[str, ...]) -> dict:
    import httpx
    from src.config import settings
    from src.services.llm_utils import curator_options
    from src.services.setup_wizard import CURATOR_SYSTEM_PROMPT
    if not CHAT_CASES.exists():
        return {"chat_skipped": f"{CHAT_CASES.name} missing"}
    cases = [c for c in json.loads(CHAT_CASES.read_text(encoding="utf-8"))["cases"]
             if c["id"] in case_ids]
    rows = []

    async def ask(messages):
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=600) as client:
            r = await client.post(f"{settings.effective_ollama}/api/chat", json={
                "model": model, "messages": messages, "stream": False,
                "keep_alive": "10m", **curator_options(0.7, 1200)})
        r.raise_for_status()
        return (r.json().get("message") or {}).get("content", "").strip(), time.perf_counter() - t0

    for c in cases:
        if c.get("type") == "multi_turn":
            hist = [{"role": "system", "content": CURATOR_SYSTEM_PROMPT + "\n\n" + c["context"]}]
            turns = []
            for u in c["turns"]:
                hist.append({"role": "user", "content": u})
                text, dt = await ask(hist)
                hist.append({"role": "assistant", "content": text})
                turns.append({"user": u, "assistant": text, "wall_s": round(dt, 2),
                              "buzzwords": buzzwords(text)})
            rows.append({"id": c["id"], "category": c["category"], "turns": turns})
        else:
            text, dt = await ask([{"role": "system", "content": CURATOR_SYSTEM_PROMPT},
                                  {"role": "user", "content": c["prompt"]}])
            rows.append({"id": c["id"], "category": c["category"], "prompt": c["prompt"],
                         "response": text, "wall_s": round(dt, 2), "buzzwords": buzzwords(text)})
    _write(out, rows)
    walls = [t["wall_s"] for r in rows for t in r.get("turns", [r])]
    return {"chat_cases": len(rows), "chat_turns": len(walls),
            "chat_buzzwords": sum(t["buzzwords"] for r in rows for t in r.get("turns", [r])),
            "chat_wall_s_median": round(statistics.median(walls), 2) if walls else None}


async def _main(args: dict) -> dict:
    sys.path.insert(0, str(REPO))
    os.chdir(REPO)
    from src.services.llm_utils import CURATOR_NUM_CTX, llm_profile
    assert llm_profile() == args["profile"]
    out = Path(args["out_dir"])
    metrics = {"num_ctx": CURATOR_NUM_CTX, "profile": llm_profile()}
    if "gate" in args["phases"]:
        metrics.update(await _gate(args["bake"], out / "gate.jsonl"))
    if "judge" in args["phases"]:
        snap = json.loads(Path(args["snapshot"]).read_text(encoding="utf-8"))
        keys = snap["subset_40"] if args["items"] == "sub40" else [i["key"] for i in snap["items"]]
        metrics.update(await _judge_and_mono(args["bake"], snap, keys, out / "judge.jsonl",
                                             out / "monologue.jsonl", "mono" in args["phases"]))
    if "chat" in args["phases"]:
        metrics.update(await _chat(args["bake"], out / "chat.jsonl", DECIDER_CASES))
    return metrics


def child_env(endpoint: str, profile: str, num_ctx: int | None) -> dict:
    return {**os.environ, "OLLAMA_ENDPOINT": endpoint, "LLM_PROFILE": profile,
            "CURATOR_NUM_CTX": str(num_ctx or 0), "CURATOR_NUM_GPU": "99",
            "PILLARS_ENABLED": "true", "PYTHONIOENCODING": "utf-8"}


if __name__ == "__main__":
    a = json.loads(sys.argv[1])
    print(json.dumps(asyncio.run(_main(a)), ensure_ascii=False))
