"""Tests for evaluation package 3: two-centroid model, pre-norm diagnostic,
mean-centering, drop penalty, exploration quota.

    python tests/test_drop_centroid.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.taste_engine import weighted_mean_embedding

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── weighted_mean_embedding: positives only + pre-norm diagnostic ────────────

v, pn = weighted_mean_embedding([([1.0, 0.0], 2.0), ([0.0, 1.0], 2.0)])
check("mean of orthogonal unit vectors -> unit result",
      abs(sum(x * x for x in v) ** 0.5 - 1.0) < 1e-9)
check("pre-norm ~0.707 flags the multimodal (conflicting) taste",
      abs(pn - 0.7071) < 1e-3)

v, pn = weighted_mean_embedding([([1.0, 0.0], 1.0), ([1.0, 0.0], 3.0)])
check("aligned vectors -> pre-norm ~1.0 (unimodal, centroid trustworthy)",
      abs(pn - 1.0) < 1e-9)

v, pn = weighted_mean_embedding([([1.0, 0.0], -1.0), ([0.0, 1.0], 0.0)])
check("negative/zero weights are excluded (drops live in their own centroid)",
      v is None and pn == 0.0)
check("empty input -> (None, 0)", weighted_mean_embedding([]) == (None, 0.0))

# ── drop-penalty stretch math (mirrors the reader) ───────────────────────────

def stretch(drop_cos, lo, hi):
    return 20.0 * max(0.0, min(1.0, (drop_cos - lo) / (hi - lo)))

check("at/below the p10 anchor -> zero penalty", stretch(0.10, 0.1, 0.5) == 0.0)
check("at the p90 anchor -> full 20-point penalty", stretch(0.50, 0.1, 0.5) == 20.0)
check("above p90 stays capped", stretch(0.90, 0.1, 0.5) == 20.0)
check("midway -> half penalty", abs(stretch(0.30, 0.1, 0.5) - 10.0) < 1e-9)

# ── wiring asserts ───────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
te = (root / "src/services/taste_engine.py").read_text(encoding="utf-8")
check("old in-centroid subtraction gone (weighted_average_embedding removed)",
      "def weighted_average_embedding" not in te)
check("drops split into their own centroid at the mean step",
      "_neg = [(emb, -w) for emb, w in embeddings_weights if w < 0]" in te)
check("pre-norm logged as the multimodality diagnostic",
      "pre-norm" in te and '"pre_norm": res.get("pre_norm")' in te)
check("blob stores drop centroid + corpus mean + drop anchors",
      '"drop_embedding": res.get("drop_embedding")' in te
      and '"corpus_mean"' in te and '"drop_cal_p10"' in te)

re_src = (root / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
check("reader centers both sides only when the blob carries the mean",
      "_cmp_vec" in re_src and "corpus_mean" in re_src)
check("drop penalty applied in del_score, capped at 20 under mismatch*80",
      "+ drop_penalty" in re_src and "drop_penalty = 20.0 * max(0.0" in re_src)
check("penalty guarded on drop centroid AND its anchors",
      'p.get("drop_cos") is not None and drop_anchors' in re_src)
check("discovery prompt carries the 15% exploration quota",
      "EXPLORATION QUOTA" in re_src and "Exploration pick:" in re_src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
