"""Tests for the score-sanity fixes (evaluation package 1, commit B):
rank flags instead of flat bonuses, global calibration anchors,
genre fallback for missing embeddings, cosine-space assertion.

    python tests/test_score_sanity.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.vector_store.chromadb_wrapper as cw
from src.services.recommendations_engine import (_cosine_anchors,
                                                 _cosine_to_mismatch)
from src.services.taste_engine import _global_cosine_anchors

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── mismatch mapping still sane ──────────────────────────────────────────────

check("cos at p90 -> mismatch 0", _cosine_to_mismatch(0.72, 0.61, 0.72) == 0.0)
check("cos at p10 -> mismatch 1", _cosine_to_mismatch(0.61, 0.61, 0.72) == 1.0)
check("missing cosine -> neutral 0.5", _cosine_to_mismatch(None, 0.61, 0.72) == 0.5)
check("small batch falls back to model constants",
      _cosine_anchors([0.65] * 5) == (0.61, 0.72))

# ── global anchors (faked corpus) ────────────────────────────────────────────

import numpy as np

rng = np.random.default_rng(7)
fake_corpus = [list(v) for v in rng.normal(0.5, 0.1, size=(200, 8))]
centroid = list(rng.normal(0.5, 0.1, size=8))

cw.get_chroma_db = lambda: SimpleNamespace(
    embeddings_for_domain=lambda domain, limit=30000: fake_corpus)

cal = _global_cosine_anchors(centroid, "movie")
check("global anchors computed from the corpus",
      cal is not None and -1.0 <= cal[0] < cal[1] <= 1.0)

cw.get_chroma_db = lambda: SimpleNamespace(
    embeddings_for_domain=lambda domain, limit=30000: fake_corpus[:10])
check("tiny corpus (<50) -> None (batch fallback stays)",
      _global_cosine_anchors(centroid, "movie") is None)
check("no embedding -> None", _global_cosine_anchors(None, "movie") is None)

# ── wiring asserts ───────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
re_src = (root / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
check("flat monitored/dislike bonus removed from ranking",
      "_monitored_bonus" not in re_src)
check("rank tuple: dislike group -> cosine -> monitored tie-break",
      "(_dislike_rank(item), vs, _monitored_rank(item))" in re_src)
check("global anchors preferred over batch anchors",
      "cal_anchors" in re_src and '_anchor_src = "global"' in re_src)
check("genre fallback for missing embeddings, damped band",
      'taste_src = "genre"' in re_src and "0.85 - 0.7 * min(1.0, _overlap)" in re_src)
check("taste_source recorded on scored candidates",
      '"taste_source": taste_src' in re_src)

te = (root / "src/services/taste_engine.py").read_text(encoding="utf-8")
check("rebuild stores cal_p10/cal_p90 beside the vector",
      '"cal_p10": cal[0] if cal else None' in te)

cw_src = (root / "src/vector_store/chromadb_wrapper.py").read_text(encoding="utf-8")
check("startup asserts hnsw:space == cosine (loud failure)",
      "RuntimeError" in cw_src and 'space != "cosine"' in cw_src)
check("embeddings_for_domain exists for the anchor computation",
      "def embeddings_for_domain" in cw_src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
