# Model Benchmarks

Curatarr runs on local models, and the choice of base model is the single
biggest quality decision in the stack. This page documents how candidates are
measured, what the numbers said, and why the current production model was
kept — with the raw data in the repo so you can check the reasoning or run
the same battery against a model of your own.

**Hardware context for every number here:** one RTX 4090 (24 GB), Ollama,
Windows. The card is shared with the household's games, which is itself a
design constraint — see the VRAM findings below.

---

## What is measured, and in which order

The curator is **one baked model serving two very different jobs**: writing
batch deletion pitches (with rich metadata handed to it) and holding a
free-form chat persona (often without any anchor). A model can be excellent
at one and disqualifying at the other — which is exactly what happened — so
the battery has three stages:

| # | Stage | Script | What it proves | Kill criterion |
|---|-------|--------|----------------|----------------|
| 1 | **Pipeline bench** | `tests/benchmarks/curator_pipeline_bench.py` | Pitch quality *with* metadata anchor (the batch reality) | — |
| 2 | **JSON stress test** | `tests/pillar_json_stresstest.py` | Strict schema output, verdict fidelity, determinism (5 cases × 3 runs, temp 0.0) | < 95 % valid = out |
| 3 | **Chat bench** (pipeline winner only) | `tests/benchmarks/curator_bench.py` | Persona *without* anchor: pushback spine, confabulation traps, honesty | Sycophancy / confabulation collapse = no swap |

**Decision rule:** a swap requires winning pipeline **and** chat with a green
stress gate. Winning only the pipeline suggests a two-bake split (separate
pipeline and chat models), not a swap.

The pipeline bench feeds each model the same 148 hardest items from the live
enrichment cache (balanced 37 per category: movie / show / anime / music),
with the production system prompt injected — so it measures the *base* model,
no bake required. Each of the 740 resulting pitches was scored on five axes
(0–5 each, 25 max): **Faith**fulness to the metadata, **Rel**evance to the
taste profile, **Spec**ificity, **Tone**, and **Format** discipline. Scoring
was done by an LLM assistant (Claude) against the metadata each pitch was
given, with a 15-item human spot-check
([`curator_ab_2026-08_spotcheck.md`](../tests/benchmarks/curator_ab_2026-08_spotcheck.md))
— judge the judging yourself; the side-by-side pitches are all there.

---

## August 2026: five candidates, no swap

Full report (primary source, German):
[`curator_ab_2026-08_REPORT.md`](../tests/benchmarks/curator_ab_2026-08_REPORT.md).
Scores: [`curator_ab_2026-08_scores.csv`](../tests/benchmarks/curator_ab_2026-08_scores.csv).
All-model history: [`model_baselines.csv`](../tests/benchmarks/model_baselines.csv).

### Pipeline (740 pitches, hand-scored)

| Model | Ø/25 | Faith | Spec | Format | ≥22 | Median lat. | p90 |
|---|---|---|---|---|---|---|---|
| **qwen3.8:27b** | **21.55** | 4.20 | 4.10 | 4.81 | **72** | **3.1 s** | **3.5 s** |
| gemma4:31b *(incumbent)* | 21.35 | 4.11 | 3.91 | **4.99** | 47 | 7.5 s | 30.3 s\* |
| muse-glimmer:30b | 21.12 | **4.53** | **4.64** | 3.89 | 68 | 15.9 s\* | 23.8 s\* |
| gemma4:26b | 20.89 | 4.02 | 3.95 | 4.97 | 10 | **1.8 s** | 2.0 s |
| qwen3.6:latest | 20.65 | 3.98 | 3.92 | 4.82 | 15 | 2.5 s\* | 5.7 s\* |

\* partially contaminated by run overlap — flagged rather than hidden; the
qwen3.8 and gemma4:26b blocks are clean.

The pipeline winner's standout skill was **detecting poisoned metadata
instead of papering over it**. The benchmark's only 25/25 pitch, on a music
entry whose enrichment had merged three different artists named "Dylan":

> "This entry is a catastrophic data contamination of a UK drum & bass
> producer, an Italian rapper, and a pop singer […] keeping this ambiguous,
> multi-identity cluster actively dilutes the precision of your library."

The weakest model on the same item confidently assumed it was Bob Dylan —
precisely the failure mode that produces wrong deletions.

### The reversal: chat without an anchor

qwen3.8 won the pipeline (+0.2 quality, 2.4× speed) and then **failed the
chat bench decisively** (multi-turn Ø 25.2/35 vs. gemma's 31.0; single-turn
20.0/25 vs. 23.1):

- **Sycophancy collapse.** Told merely to "run a Level 2 thematic scan" — a
  command, not an argument — it answered *"You're right to push back… I am
  reversing the deletion recommendation"* and repeated the same capitulation
  nearly verbatim four turns in a row. The incumbent held its position,
  conceded only against *new, concrete information*, and scored the
  benchmark's reference performance (34/35) on the same case.
- **Anchor-less confabulation.** Asked about real titles without metadata, it
  invented the entire plot of *Hard to Be a God* (characters that do not
  exist, the wrong director), inverted the premise of *Jury Duty*, and
  "debunked" the real *Star Trek: Starfleet Academy* as a scam — with
  fabricated supporting evidence.
- The incumbent's own known weakness is the mirror image: it confabulates
  plausible **unknown** titles when unanchored (it reviewed a fake film
  invented by the test suite), while qwen confabulates facts **about known**
  titles. In Curatarr's operation — chat almost always concerns library items
  *with* context — the former risk surfaces far less often.

**Verdict: no swap.** One bake serves both roles, and the chat persona is the
product's face. gemma4:31b stayed. The stress gate independently
disqualified two candidates: qwen3.6 (VRAM starvation, 2 s → 600 s latency
escalation, timeouts) and muse-glimmer (0/15 valid JSON — consistently fast,
consistently unparseable). muse-glimmer's factual density (Faith 4.53, zero
buzzwords) keeps it interesting as a possible grammar-forced facts harvester.

---

## Context-window cost on a 24 GB card

`tests/benchmarks/num_ctx_bench.py`, results in
[`num_ctx_2026-07-08_19-54.md`](../tests/benchmarks/num_ctx_2026-07-08_19-54.md):
generation speed for the ~20 GB curator is flat (~33 t/s) at 8 k, 12 k and
16 k context — and **drops to 16–21 t/s at 20 k**, where the KV cache starts
starving the card. That measurement is why production pins `num_ctx=16384`
on the pitch path and 8192 on the judge path, and why ≥22 GB dense models
are effectively disqualified on this hardware: the same starvation produced
qwen3.6's 600-second timeouts.

Latency numbers are only comparable at the *same* context size: at 16 k the
incumbent needs 7.5 s per pitch against qwen's 3.1 s, at 8 k the gap shrinks
to 7.6 s vs. 6.2 s. Both are reported; they are never mixed.

---

## Reproducing this

1. Stop the app (the vector store is single-process; benches refuse to share).
2. Keep the GPU exclusive — no game, no overlapping runs. The asterisks in
   the tables above are what overlap contamination looks like.
3. Pipeline: `python tests/benchmarks/curator_pipeline_bench.py --models <a,b,…>`
   — writes dated `.jsonl` (source of truth), `.md` (readable) and `.csv`.
4. Stress gate: add your model to the `MODELS` list in
   `tests/pillar_json_stresstest.py` and run it.
5. Chat: `python tests/benchmarks/curator_bench.py --models <incumbent,candidate>`.
6. Score against the rubric above, append a row to `model_baselines.csv`,
   and read the runbook
   ([`BENCHMARK_RUNBOOK.md`](../tests/benchmarks/BENCHMARK_RUNBOOK.md),
   German) for the operational traps we hit so you don't have to.

The multi-megabyte raw transcripts (740 pitches × 5 models, tournament logs
from the May 2026 selection rounds) stay out of the repo for size; the
`.jsonl`/`.csv` your own runs produce are the same shape as ours.
