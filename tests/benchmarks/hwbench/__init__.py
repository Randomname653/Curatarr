"""Hardware-matrix benchmark: which curator runs how well on which card.

Two independent axes, measured separately because they do not depend on
each other:

* **performance** (model x context x KV cache x VRAM tier): does it fit,
  how much spills to the CPU, load time, prefill/generation tok/s, judge
  and monologue latency, RAM taken by a spill. A smaller card is simulated
  on the 4090 by a CUDA ballast that holds the difference (gpu.Ballast).
* **quality** (model x LLM_PROFILE): JSON gate on the production
  constitution, judge verdicts on frozen production evidence, monologues
  and chat answers for scoring. Quality does not change with the card, so
  it is measured once per model and profile, not once per tier.

Everything lands in ``bench_ledger.json`` (ledger.py): every model by its
weights digest, every run with its full settings and environment. A run
whose fingerprint is already in the ledger is skipped, so no model is
benched twice for the same question. ``matrix.py`` turns the ledger into
the decision matrix the setup wizard ships (src/services/model_matrix.json).

Entry point: ``python -m tests.benchmarks.hwbench.run --help`` from the
repo root. BENCHMARK_RUNBOOK.md has the protocol.
"""

# Bumped whenever a prompt, a probe or the evidence snapshot format changes
# in a way that makes old measurements incomparable. Part of every run
# fingerprint, so a bump re-measures instead of reusing stale rows.
BENCH_VERSION = "2026-10.1"
