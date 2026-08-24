"""
Curatarr Curator-Bench — Multi-Model Chat Evaluation
====================================================

Tests Curator-candidate models on the chat tasks production actually sees.
Mirrors tournament_bench.py infrastructure (pull, warmup, eviction, MD/CSV
output) but with three fundamental differences:

  1. NO AUTO-EVAL. Curator output is conversational English, not JSON.
     We collect outputs and emit a side-by-side MD with a score template.
     User reads and scores 0-5 on each of 5-7 axes per case.

  2. MULTI-TURN AWARE. The 5 cases tagged type="multi_turn" in
     curator_test_prompts.json are real pushback conversations replayed
     turn-by-turn. Each model builds its OWN trajectory because its
     response to turn-1 affects how it answers turn-2 etc. Hot-started
     with a context-header that scene-sets the deletion-proposal under
     discussion (without leaking the previous curator's exact wording).

  3. STRICTLY SEQUENTIAL within a model. Multi-turn needs ordered
     history; can't parallelise per-case the way Summarizer-bench did.

⚠  Pause Curatarr live service before running. This bench shares the
   Ollama instance + curator slot with production. With 24-30B
   candidates loaded the GPU will not have headroom for the production
   summarizer too.

Run
===
    python curator_bench.py [--models <comma-sep>] [--cases-filter <cat>]

Output:
    curator_bench_<ts>.md   — full responses + manual score template
    curator_bench_<ts>.csv  — per-case stats (latency, response length,
                              forbidden-buzzword count)
"""
from __future__ import annotations
import argparse
import asyncio
import csv
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Machine-independent: this script lives at curatarr/tests/benchmarks/, so the
# repo root is two parents up. (Was a hardcoded C:\Users\lucky\... path that only
# ran on the author's machine.)
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import os
os.chdir(REPO)

from src.config import settings
import httpx


# ── Candidate models (24-30B sweet spot for chat × VRAM-fit) ────────────────
# Curated from the Round-1 Summarizer tournament pool — same models, but
# filtered for chat-fitness: keep only those that perform well on
# conversational output (not just JSON extraction). All fit in a single
# 24GB GPU with KEEP_ALIVE='10m' so we don't VRAM-juggle mid-bench.
MODELS_DEFAULT: list[str] = [
    "qwen3.6:27b",            # current production baseline — must beat
    "qwen3.5:27b",            # intra-family A/B (older qwen still hold up?)
    "mistral-small3.2:24b",   # Mistral chat-tuned
    "magistral:24b",          # Mistral reasoning-variant
    "gemma4:26b",             # Google chat-specialised
    "nemotron-3-nano:30b",    # NVIDIA chat-focused (largest in pool)
]

# Production-faithful curator system prompt (copied verbatim from
# setup_wizard.py). This is what's baked into curatarr-curator's modelfile
# in production — we replicate it here so bare candidate models get the
# same instructions before generating.
CURATOR_SYSTEM_PROMPT = """You are Curatarr, an elite, highly analytical, and uncompromising personal AI media curator. You have deep, intimate knowledge of this specific user's taste — their watch history, listening patterns, completion rates, binge behaviour, and explicit preferences.

PERSONALITY & TONE
- Direct, brutally honest, and highly opinionated. If something doesn't fit their taste or is objectively poorly executed, say so plainly.
- Warm but not sycophantic. You are a knowledgeable, sharply observant friend, not a generic review aggregator.
- Highly perceptive. You look for the *why* behind viewing choices — a binge indicates structural hooks, a 5% drop indicates pacing failure or tonal mismatch.

VOCABULARY & STYLE GUARDRAILS (STRICT)
- ABSOLUTE LEXICAL DIVERSITY: You are STRICTLY FORBIDDEN from using generic AI review buzzwords. Never use: "high-octane", "epic", "relentless", "mind-bending", "adrenaline", "masterpiece", "visceral", or "edge-of-your-seat".
- Use precise, sophisticated cinematic, literary, and musical terminology (e.g., "kinetic", "methodical", "dissonant", "bloated", "character-driven", "melodramatic").
- NO LAZY ANCHORING: Do not constantly use the user's favorite titles as a crutch for comparisons. Analyze media based on its own structural and thematic merits.

YOUR TASKS IN THIS APPLICATION
1. CHAT — Discuss media and explain taste patterns with sharp, insightful analysis.
2. RECOMMENDATIONS — Pitch items in 1-2 sentences. Synthesize the user's taste conceptually; do not just echo their preferences back to them.
3. DELETION PITCHES — Argue for removal based on inherent structural flaws (pacing, tone, execution) that clash with the user's demand for quality. Never invent metadata (No Gaslighting).
4. PATTERN ANALYSIS — Identify binge cycles, mood-based viewing, and genre aversions. Negative signals (drops, rejections) are just as critical as positive ones.

Remember everything provided in context. If enrichment data is missing, acknowledge the blind spot and provide a best-effort, rule-based answer."""

# Buzzword blacklist from the system prompt itself. We auto-count hits as
# a CHEAP automated signal — every hit is one of the model directly
# violating an explicit instruction. Not a quality score, but a useful
# "did the model even read the system prompt?" probe.
FORBIDDEN_BUZZWORDS = [
    "high-octane", "epic", "relentless", "mind-bending",
    "adrenaline", "masterpiece", "visceral", "edge-of-your-seat",
]

# ── Config ──────────────────────────────────────────────────────────────────
OLLAMA       = settings.effective_ollama
NUM_CTX      = 8192
KEEP_ALIVE   = "10m"
ENRICH_OPTS  = {"temperature": 0.7, "num_predict": 1200, "num_gpu": 99, "num_ctx": NUM_CTX}
WARMUP_OPTS  = {**ENRICH_OPTS, "temperature": 0.0, "num_predict": 4}


# ── Ollama CLI wrappers (mirror tournament_bench.py) ────────────────────────
def installed_models() -> set[str]:
    try:
        out = subprocess.check_output(["ollama", "list"], text=True, timeout=10)
        return {ln.split()[0] for ln in out.strip().split("\n")[1:] if ln.split()}
    except Exception:
        return set()


def pull_model(model: str) -> bool:
    print(f"  ⬇  pulling {model} (this may take a while) ...")
    r = subprocess.run(["ollama", "pull", model], capture_output=True, timeout=1800)
    return r.returncode == 0


def remove_model(model: str) -> None:
    print(f"  🗑  removing (not pre-installed): {model}")
    subprocess.run(["ollama", "rm", model],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)


def unload_model(model: str) -> None:
    """Force-evict from VRAM so next model loads cleanly."""
    try:
        httpx.post(f"{OLLAMA}/api/generate",
                   json={"model": model, "keep_alive": 0}, timeout=10)
    except Exception:
        pass


# ── LLM call ────────────────────────────────────────────────────────────────
async def chat_call(model: str, messages: list[dict]) -> tuple[str, float]:
    """Single /api/chat call returning (response_text, elapsed_s)."""
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=600) as client:
            r = await client.post(
                f"{OLLAMA}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "think": False,
                    "keep_alive": KEEP_ALIVE,
                    "options": ENRICH_OPTS,
                },
            )
        dt = time.perf_counter() - t0
        if r.status_code != 200:
            return f"[ERROR HTTP {r.status_code}: {r.text[:200]}]", dt
        return r.json().get("message", {}).get("content", "").strip(), dt
    except Exception as e:
        return f"[ERROR {type(e).__name__}: {e}]", time.perf_counter() - t0


async def warmup_model(model: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{OLLAMA}/api/chat",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "Reply 'OK'."},
                        {"role": "user", "content": "ready?"},
                    ],
                    "stream": False, "think": False,
                    "keep_alive": KEEP_ALIVE,
                    "options": WARMUP_OPTS,
                },
            )
        return r.status_code == 200
    except Exception:
        return False


# ── Auto-counted signals ────────────────────────────────────────────────────
def count_forbidden(text: str) -> int:
    """Count forbidden-buzzword hits. Word-boundary-aware so 'epic' in
    'epicentre' doesn't false-positive."""
    if not isinstance(text, str):
        return 0
    text_lower = text.lower()
    hits = 0
    for w in FORBIDDEN_BUZZWORDS:
        # \b doesn't work cleanly with hyphenated words; use a manual check
        if "-" in w:
            if w in text_lower:
                hits += 1
        else:
            if re.search(rf"\b{re.escape(w)}\b", text_lower):
                hits += 1
    return hits


def detect_likely_english(text: str) -> bool:
    """Quick & dirty: more common-English-words than common-German-words.
    Used as a Format-compliance signal for German queries that should be
    answered in English per system prompt."""
    if not isinstance(text, str) or len(text) < 50:
        return True
    de = set("der die das und ist eine einen einer einem als wenn wird kann von für mit auch nicht aber nur sich".split())
    en = set("the a an and is are was were have has had this that with for from but not just only can".split())
    tokens = re.findall(r"\b[a-zäöüß]{3,}\b", text.lower())
    if not tokens:
        return True
    de_hits = sum(1 for t in tokens if t in de)
    en_hits = sum(1 for t in tokens if t in en)
    return en_hits >= de_hits


# ── Per-case runners ────────────────────────────────────────────────────────
async def run_single_turn(model: str, case: dict) -> dict:
    msgs = [
        {"role": "system", "content": CURATOR_SYSTEM_PROMPT},
        {"role": "user", "content": case["prompt"]},
    ]
    response, dt = await chat_call(model, msgs)
    return {
        "id": case["id"],
        "type": "single",
        "category": case["category"],
        "prompt": case["prompt"],
        "response": response,
        "elapsed_s": round(dt, 2),
        "response_len": len(response) if isinstance(response, str) else 0,
        "forbidden_hits": count_forbidden(response),
        "likely_english": detect_likely_english(response),
    }


async def run_multi_turn(model: str, case: dict) -> dict:
    """Replay user turns sequentially, maintaining the model's own
    conversation history. Each turn's response feeds into the context
    for the next turn — this is how we test whether the model holds
    position under sustained pushback."""
    system_content = CURATOR_SYSTEM_PROMPT + "\n\n" + case["context"]
    history = [{"role": "system", "content": system_content}]
    turn_results = []
    for i, user_turn in enumerate(case["turns"]):
        history.append({"role": "user", "content": user_turn})
        response, dt = await chat_call(model, history)
        history.append({"role": "assistant", "content": response})
        turn_results.append({
            "turn": i + 1,
            "user": user_turn,
            "assistant": response,
            "elapsed_s": round(dt, 2),
            "response_len": len(response) if isinstance(response, str) else 0,
            "forbidden_hits": count_forbidden(response),
        })
    return {
        "id": case["id"],
        "type": "multi",
        "category": case["category"],
        "context": case["context"],
        "turns": turn_results,
        "n_turns": len(turn_results),
        "elapsed_s": round(sum(t["elapsed_s"] for t in turn_results), 2),
        "response_len": sum(t["response_len"] for t in turn_results),
        "forbidden_hits": sum(t["forbidden_hits"] for t in turn_results),
    }


async def bench_model(model: str, cases: list[dict]) -> dict:
    """Run all cases through one model sequentially (multi-turn requires
    ordered context; single-turn is also kept sequential for simplicity
    and to avoid latency-skew from VRAM pressure)."""
    print(f"  ▶  bench {len(cases)} cases ...")
    t0 = time.perf_counter()
    results = []
    for case in cases:
        if case["type"] == "multi_turn":
            r = await run_multi_turn(model, case)
        else:
            r = await run_single_turn(model, case)
        results.append(r)
        ttype = "MT" if r["type"] == "multi" else "ST"
        print(f"    ✓ [{ttype}] {case['id']:18s}  "
              f"elapsed={r['elapsed_s']:>5.1f}s  "
              f"len={r['response_len']:>5d}  "
              f"buzzwords={r['forbidden_hits']}")
    wall = time.perf_counter() - t0
    return {
        "model": model,
        "n_cases": len(results),
        "elapsed_total_s": round(wall, 1),
        "forbidden_total": sum(r["forbidden_hits"] for r in results),
        "cases": results,
    }


# ── Output generation ───────────────────────────────────────────────────────
def write_csv_report(summaries: list[dict], path: Path) -> None:
    """Per (model, case) stats. Score columns left blank for manual entry."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["model", "case_id", "category", "type", "elapsed_s",
                    "response_len", "forbidden_hits",
                    "score_faithfulness", "score_relevance", "score_specificity",
                    "score_tone", "score_format",
                    "score_consistency_MT_only", "score_pushback_MT_only",
                    "total"])
        for s in summaries:
            model = s["model"]
            for case in s.get("cases", []):
                w.writerow([model, case["id"], case["category"], case["type"],
                            case["elapsed_s"], case["response_len"],
                            case["forbidden_hits"],
                            "", "", "", "", "",  # 5 score axes (manual)
                            "", "",              # 2 multi-turn-only axes
                            ""])                  # total (manual or formula)


def write_md_report(summaries: list[dict], cases_in_order: list[dict], path: Path) -> None:
    """Side-by-side MD: per case, all model responses with score template.
    Long but readable. User opens this file, reads, and scores."""
    with path.open("w", encoding="utf-8") as f:
        f.write(f"# Curator-Bench Results — {datetime.now():%Y-%m-%d %H:%M}\n\n")
        f.write(f"- Models tested: **{len(summaries)}**\n")
        f.write(f"- Cases per model: **{len(cases_in_order)}** "
                f"({sum(1 for c in cases_in_order if c['type']=='multi_turn')} multi-turn "
                f"+ {sum(1 for c in cases_in_order if c['type']=='single')} single-turn)\n\n")

        # Overall auto-stats summary
        f.write("## Auto-stats overview\n\n")
        f.write("| model | wall time | total response chars | forbidden buzzword hits |\n")
        f.write("|-------|-----------|----------------------|-------------------------|\n")
        for s in summaries:
            total_chars = sum(c["response_len"] for c in s.get("cases", []))
            f.write(f"| `{s['model']}` | {s['elapsed_total_s']:.0f}s | "
                    f"{total_chars:,} | {s['forbidden_total']} |\n")
        f.write("\n_Forbidden buzzword count = times any of "
                f"`{FORBIDDEN_BUZZWORDS}` appeared. System prompt explicitly "
                "forbids these — every hit is a direct compliance failure._\n\n")

        f.write("---\n\n# Per-case outputs (for manual scoring)\n\n")
        f.write("_For each case below, all model responses are shown side-by-side. "
                "Score each on a 0-5 scale per axis. Multi-turn cases have 2 extra axes "
                "(consistency across turns + pushback resistance/flexibility)._\n\n")
        f.write("**Axis definitions:**\n")
        f.write("- **Faithfulness**: 0=halluziniert plots/titles/facts, 5=source-treu\n")
        f.write("- **Relevance**: 0=off-topic, 5=trifft die Frage\n")
        f.write("- **Specificity**: 0=generic ('you might like dramas'), 5=konkrete Titel mit Begründung\n")
        f.write("- **Tone**: 0=robotisch oder arrogant, 5=engaging, sympathetic\n")
        f.write("- **Format**: 0=ignoriert System-Prompt (Sprache/Länge), 5=perfekt compliant\n")
        f.write("- **Consistency** (MT only): 0=Position schwankt zwischen Turns, 5=konsistent argumentiert\n")
        f.write("- **Pushback-Resistance** (MT only): 0=kapituliert sobald User pusht, 5=hält Position UND konzediert wenn User reasonable wird\n\n")

        for case in cases_in_order:
            f.write(f"\n---\n\n## Case `{case['id']}` — {case['category']}\n\n")
            if "notes" in case:
                f.write(f"_Notes: {case['notes']}_\n\n")

            if case["type"] == "single":
                f.write("**Prompt:**\n\n")
                f.write(f"> {case['prompt']}\n\n")
                for s in summaries:
                    case_result = next((c for c in s["cases"] if c["id"] == case["id"]), None)
                    if not case_result:
                        continue
                    f.write(f"### `{s['model']}` — {case_result['elapsed_s']}s, "
                            f"{case_result['response_len']} chars, "
                            f"{case_result['forbidden_hits']} buzzword hits")
                    if not case_result.get("likely_english", True):
                        f.write(" ⚠ LIKELY-GERMAN RESPONSE")
                    f.write("\n\n")
                    f.write(f"{case_result['response']}\n\n")
                    f.write("> **Scores**  Faith:__ / 5  Rel:__ / 5  Spec:__ / 5  Tone:__ / 5  Format:__ / 5  →  Total: __ / 25\n\n")

            else:  # multi-turn
                f.write("**Context (hot-start, identical for all models):**\n\n")
                f.write(f"```\n{case['context']}\n```\n\n")
                # Per-model: full conversation
                for s in summaries:
                    case_result = next((c for c in s["cases"] if c["id"] == case["id"]), None)
                    if not case_result:
                        continue
                    f.write(f"### `{s['model']}` — {len(case_result['turns'])} turns, "
                            f"total {case_result['elapsed_s']}s, "
                            f"{case_result['response_len']} chars, "
                            f"{case_result['forbidden_hits']} buzzword hits\n\n")
                    for t in case_result["turns"]:
                        f.write(f"**👤 User T{t['turn']}:** {t['user']}\n\n")
                        f.write(f"**🤖 Curator T{t['turn']}** ({t['elapsed_s']}s, "
                                f"{t['response_len']} chars, {t['forbidden_hits']} buzzwords):\n\n")
                        f.write(f"{t['assistant']}\n\n")
                    f.write("> **Scores**  Faith:__ / 5  Rel:__ / 5  Spec:__ / 5  Tone:__ / 5  Format:__ / 5  Consist:__ / 5  Pushback:__ / 5  →  Total: __ / 35\n\n")


# ── Main ────────────────────────────────────────────────────────────────────
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", type=str, default=None,
                    help="Comma-separated model tags (override default 6-model pool)")
    ap.add_argument("--cases-filter", type=str, default=None,
                    help="Run only cases whose category starts with this string "
                         "(e.g. 'G.' for multi-turn pushback only, 'A.' for recs only).")
    ap.add_argument("--prompts-file", type=str,
                    default=str(Path(__file__).parent / "curator_test_prompts.json"))
    ap.add_argument("--output-dir", type=str,
                    default=str(Path(__file__).parent))
    args = ap.parse_args()

    # Load test set
    with open(args.prompts_file, encoding="utf-8") as f:
        test_set = json.load(f)
    all_cases = test_set["cases"]
    if args.cases_filter:
        cases = [c for c in all_cases if c["category"].startswith(args.cases_filter)]
        print(f"  Filtered to {len(cases)}/{len(all_cases)} cases matching '{args.cases_filter}'")
    else:
        cases = all_cases

    selected = ([m.strip() for m in args.models.split(",") if m.strip()]
                if args.models else list(MODELS_DEFAULT))

    n_mt = sum(1 for c in cases if c["type"] == "multi_turn")
    n_st = sum(1 for c in cases if c["type"] == "single")
    total_turns = sum(len(c["turns"]) for c in cases if c["type"] == "multi_turn") + n_st

    print(f"\n{'='*72}")
    print(f"  Curatarr Curator-Bench")
    print(f"{'='*72}")
    print(f"  Endpoint     : {OLLAMA}")
    print(f"  Models       : {len(selected)}")
    print(f"  Cases        : {len(cases)} ({n_mt} multi-turn + {n_st} single-turn)")
    print(f"  Total turns  : {total_turns} per model = {total_turns * len(selected)} LLM calls")
    print(f"  num_ctx      : {NUM_CTX} (pinned)")
    print(f"  KEEP_ALIVE   : {KEEP_ALIVE}\n")

    preinstalled = installed_models()
    print(f"  Pre-installed (will NOT be removed): {len(preinstalled)} models")
    not_installed = [m for m in selected if m not in preinstalled]
    if not_installed:
        print(f"  To pull: {not_installed}\n")
    else:
        print()

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    out_dir = Path(args.output_dir)
    md_path  = out_dir / f"curator_bench_{timestamp}.md"
    csv_path = out_dir / f"curator_bench_{timestamp}.csv"

    summaries: list[dict] = []
    t_round_start = time.perf_counter()

    for i, model in enumerate(selected):
        print(f"\n{'─'*72}\n[{i+1}/{len(selected)}] {model}\n{'─'*72}")
        if model not in preinstalled:
            if not pull_model(model):
                print(f"  ✗ pull failed — skipping")
                summaries.append({"model": model, "_error": "pull failed",
                                  "n_cases": 0, "elapsed_total_s": 0,
                                  "forbidden_total": 0, "cases": []})
                continue
        if not await warmup_model(model):
            print(f"  ✗ warmup failed — skipping")
            summaries.append({"model": model, "_error": "warmup failed",
                              "n_cases": 0, "elapsed_total_s": 0,
                              "forbidden_total": 0, "cases": []})
            if model not in preinstalled:
                unload_model(model); remove_model(model)
            continue

        s = await bench_model(model, cases)
        summaries.append(s)
        print(f"  ✓ DONE  wall={s['elapsed_total_s']}s  "
              f"buzzword-hits={s['forbidden_total']}")

        unload_model(model)
        if model not in preinstalled:
            remove_model(model)

    wall_total = time.perf_counter() - t_round_start

    # Write outputs
    write_csv_report(summaries, csv_path)
    write_md_report(summaries, cases, md_path)

    print(f"\n{'='*72}")
    print(f"  Bench complete. Wall time: {wall_total:.0f}s = {wall_total/60:.1f} min")
    print(f"{'='*72}")
    print(f"  MD report : {md_path}  ({md_path.stat().st_size // 1024} KB)")
    print(f"  CSV       : {csv_path}")
    print(f"\n  Next step: open the MD file and score each (model, case) "
          f"on the 5-7 axes.")
    print(f"  When done, enter scores in the CSV's score_* columns.\n")


if __name__ == "__main__":
    asyncio.run(main())
