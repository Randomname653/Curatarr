"""Cluster-separation gate: centered vs raw cosines.

External eval catch: text embeddings share an anisotropy cone, so two
GENUINELY different taste clusters still read ~0.94 raw cosine — the
gate rejected them as 'one cloud split artificially' and the pair fell
back to a single centroid (music: five real listening lanes lost, among
them the owner's 174-play Kabarett lane — root of the Malmsheimer
mis-pitch). The gate now centers centroids against the CORPUS mean for
the parallelism test; self-centering was rejected by design (k=2
centroids centered by their own mean are ALWAYS antiparallel — the gate
would never fire and unimodal detection would break).

    python tests/test_cluster_gate.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


import numpy as np
from src.services.taste_engine import _cluster_centroids

DIM = 8


def _unit(v):
    v = np.asarray(v, dtype=float)
    return (v / np.linalg.norm(v)).tolist()


def _cone_point(direction_dim: int, i: int):
    """A point on the shared anisotropy cone: dominant base axis 0 plus a
    small cluster-specific direction and deterministic jitter."""
    v = np.zeros(DIM)
    v[0] = 10.0                      # the cone every embedding shares
    v[direction_dim] = 1.0           # the actual (small) taste signal
    v[3 + (i % 2)] = 0.05 if i % 4 < 2 else -0.05   # deterministic jitter
    return _unit(v)


# Two REAL clusters (20 items each) whose raw cosine is ~0.99.
bimodal = ([( _cone_point(1, i), 1.0, f"A{i}") for i in range(20)]
           + [(_cone_point(2, i), 1.0, f"B{i}") for i in range(20)])
corpus_mean = np.zeros(DIM)
corpus_mean[0] = 1.0                 # the corpus centers on the cone axis

res_old = _cluster_centroids(bimodal)
check("raw gate rejects the two REAL clusters (the documented failure)",
      res_old is None)

res = _cluster_centroids(bimodal, corpus_mean=corpus_mean)
check("centered gate keeps them", res is not None and len(res) == 2)
if res:
    check("both clusters carry ~half the mass",
          all(0.35 <= c["share"] <= 0.65 for c in res))
    tops = " ".join(t for c in res for t in c["top_titles"])
    check("clusters separate the two title families",
          any(t.startswith("A") for t in tops.split())
          and any(t.startswith("B") for t in tops.split()))

# One tight cloud (unimodal) — k-means will split it; the centered gate
# must still reject the artificial split.
unimodal = [(_cone_point(1, i), 1.0, f"U{i}") for i in range(40)]
check("centered gate still rejects an artificially split unimodal cloud",
      _cluster_centroids(unimodal, corpus_mean=corpus_mean) is None)

# Degenerate reference: centroid sitting AT the corpus mean must not crash
# (falls back to the raw-cone test).
at_mean = [( _unit(corpus_mean), 1.0, f"M{i}") for i in range(20)] \
          + [(_cone_point(2, i), 1.0, f"B{i}") for i in range(20)]
try:
    _ = _cluster_centroids(at_mean, corpus_mean=corpus_mean)
    check("centroid at the corpus mean does not crash the gate", True)
except Exception as e:
    check(f"centroid at the corpus mean does not crash the gate ({e})", False)

# Wiring: compute passes the corpus mean; helper exists.
te = (Path(__file__).resolve().parents[1] / "src/services/taste_engine.py").read_text(encoding="utf-8")
check("compute call site feeds _domain_corpus_mean(category) into the gate",
      "corpus_mean=_domain_corpus_mean(category)" in te)
check("stored centroids stay RAW (readers center via the blob)",
      "stored raw" in te)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
