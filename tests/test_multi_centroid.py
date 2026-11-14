"""Tests for the multi-centroid taste model (evaluation package 3b).

    python tests/test_multi_centroid.py
"""
from _harness import check  # noqa: I001 - first: temp stores

from pathlib import Path
from types import SimpleNamespace


import numpy as np

import src.vector_store.chromadb_wrapper as cw
from src.services.taste_engine import _cluster_centroids, _corpus_calibration


def _unit(v):
    v = np.asarray(v, dtype=float)
    return list(v / np.linalg.norm(v))


rng = np.random.default_rng(3)


def _cloud(center, n, prefix):
    out = []
    for i in range(n):
        v = np.asarray(center, dtype=float) + rng.normal(0, 0.05, size=8)
        out.append((_unit(v), 1.0, f"{prefix}{i}"))
    return out

A = [1, 0, 0, 0, 0, 0, 0, 0]
B = [0, 1, 0, 0, 0, 0, 0, 0]

pos = _cloud(A, 30, "mecha") + _cloud(B, 20, "slice")
cl = _cluster_centroids(pos)
check("two separated interest clouds -> two clusters",
      cl is not None and len(cl) == 2)
check("shares reflect the cloud masses (0.6/0.4)",
      abs(cl[0]["share"] - 0.6) < 0.05 and abs(cl[1]["share"] - 0.4) < 0.05)
check("top titles come from the right cloud",
      all(t.startswith("mecha") for t in cl[0]["top_titles"])
      and all(t.startswith("slice") for t in cl[1]["top_titles"]))
check("centroids are unit vectors",
      all(abs(np.linalg.norm(c["embedding"]) - 1.0) < 1e-6 for c in cl))
check("deterministic (no random init) — second run identical",
      _cluster_centroids(pos) == cl)

noise = _cloud(A, 60, "main") + _cloud(B, 2, "noise")
cl2 = _cluster_centroids(noise)
check("a <8%-mass noise cloud never becomes a cluster -> falls back to None",
      cl2 is None)

check("unimodal input (one cloud) -> None (single centroid stays)",
      _cluster_centroids(_cloud(A, 50, "x")) is None)

# ── the taste build's gate: three interests of at least 15 % each ────────────
from src.services.taste_engine import RECALL_MIN_CLUSTERS, RECALL_MIN_SHARE  # noqa: E402

C = [0, 0, 1, 0, 0, 0, 0, 0]
gate = {"min_share": RECALL_MIN_SHARE, "min_clusters": RECALL_MIN_CLUSTERS}
check("the build's gate: two clear interests are not enough, the one centroid serves",
      _cluster_centroids(pos, **gate) is None)
three = _cloud(A, 40, "a") + _cloud(B, 35, "b") + _cloud(C, 25, "c")
cl3 = _cluster_centroids(three, **gate)
check("...three of at least 15 % each are clusters",
      cl3 is not None and len(cl3) == 3 and min(c["share"] for c in cl3) >= 0.15, cl3)
small = _cloud(A, 60, "a") + _cloud(B, 30, "b") + _cloud(C, 10, "c")
check("...a 10 % third interest is no cluster, and two left are not enough "
      "(the owner's anime split 89 % / 9 % on 11-08)",
      _cluster_centroids(small, **gate) is None)
check("...the wrapup's two-cluster story keeps the old floor",
      _cluster_centroids(small) is not None)

# ── calibration accepts a cluster list (max-over-clusters statistic) ─────────

fake_corpus = [list(rng.normal(0.4, 0.15, size=8)) for _ in range(200)]
cw.get_chroma_db = lambda: SimpleNamespace(
    embeddings_for_domain=lambda domain, limit=30000: fake_corpus)

clusters = [{"embedding": _unit(A), "weight": 3.0},
            {"embedding": _unit(B), "weight": 2.0}]
calib = _corpus_calibration(clusters, None, "anime")
check("calibration over a cluster list works (max-over-clusters anchors)",
      calib is not None and calib["cal"][0] < calib["cal"][1])
single = _corpus_calibration(_unit(A), None, "anime")
check("max-over-clusters anchors differ from single-centroid anchors",
      calib["cal"] != single["cal"])
both = _corpus_calibration(clusters, None, "anime", single=_unit(A))
check("a cluster blob also carries the one centroid's anchors (deletion reads them)",
      both["cal"] == calib["cal"] and both["single_cal"] == single["cal"], both)
check("...a single-centroid blob needs none: its cal_* are those",
      _corpus_calibration(_unit(A), None, "anime", single=_unit(A))["single_cal"] is None)

# ── deletion reads the one centroid ──────────────────────────────────────────
from src.services.recommendations_engine import _deletion_taste_basis  # noqa: E402

blob = {"embedding": _unit(A), "cluster_centroids": clusters, "cal_p10": 0.2, "cal_p90": 0.6,
        "single_cal_p10": 0.1, "single_cal_p90": 0.5}
check("deletion scores a cluster blob against the one centroid, with its own anchors "
      "(kept vs deleted AUC 0.672 vs 0.620 films, 0.665 vs 0.616 series)",
      _deletion_taste_basis(blob, (0.2, 0.6)) == (None, (0.1, 0.5)))
old = {k: v for k, v in blob.items() if not k.startswith("single_")}
check("...a blob from before the single anchors keeps scoring as it was calibrated",
      _deletion_taste_basis(old, (0.2, 0.6)) == (clusters, (0.2, 0.6)))
check("...a single-centroid blob is untouched",
      _deletion_taste_basis({"embedding": _unit(A)}, (0.2, 0.6)) == (None, (0.2, 0.6)))

# ── wiring asserts ───────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
te = (root / "src/services/taste_engine.py").read_text(encoding="utf-8")
check("clustering gated on measured multimodality + enough items",
      "if multimodal and len(_pos) >= 40" in te)
check("blob stores cluster_centroids",
      '"cluster_centroids": res.get("cluster_centroids")' in te)
check("calibration uses clusters when present",
      'res.get("cluster_centroids") or res.get("embedding")' in te)

re_src = (root / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
check("deletion scoring can still read max over centroids (a blob from before single_cal_*)",
      "cosine = max(float(np.dot(u, iv_c)) for u in user_cmps)" in re_src)
check("library-lane ranking is max over centroids",
      "return max(float(np.dot(u, emb_n)) for u in user_vecs_n)" in re_src)
check("prompts surface the clusters (serve all, not a blend)",
      "serve them ALL, not a blend" in re_src)

check.done()
