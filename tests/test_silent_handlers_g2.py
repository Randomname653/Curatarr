"""Silent-handler sweep, group 2 (docs/SILENT_FAILURE_SWEEP_2026_10.md).

The embedding migration's neighborhood eval gate decides whether the v2
corpus is flipped live. A pair whose lookup RAISED was skipped at DEBUG, so
the gate scored only the pairs that survived: 20 broken pairs + 10 good ones
read as 10/10 = 100% and the flip went ahead. An errored pair now counts as
tried and missed, and the first one is logged at WARNING.

    python tests/test_silent_handlers_g2.py
"""
import logging
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.services.embedding_migration as em  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _vec(n):
    return [math.cos(n * 0.1), math.sin(n * 0.1)]


class _Src:
    """Old space: each doc's nearest neighbour is its twin ``nb{n}``; docs
    with n >= ``broken_from`` make the query raise."""
    def __init__(self, broken_from):
        self.broken_from = broken_from

    def query(self, query_embeddings, n_results):
        n = int(query_embeddings[0][2])
        if n >= self.broken_from:
            raise RuntimeError("hnsw index segment unreadable")
        return {"ids": [[f"d{n}", f"nb{n}"]]}


class _Dst:
    """New space: a doc and its twin share a vector, other docs differ."""
    def __init__(self, none_for=()):
        self.none_for = set(none_for)

    def get_by_id(self, doc_id):
        n = int(doc_id.lstrip("dnb"))
        if doc_id in self.none_for:
            return {"embedding": None}
        return {"embedding": _vec(n)}


sample = [(f"d{n}", _vec(n) + [n]) for n in range(30)]

cap = _Capture()
em.logger.addHandler(cap)
em.logger.setLevel(logging.DEBUG)
try:
    hits, tried = em._neighborhood_eval(_Src(broken_from=10), _Dst(), sample)
finally:
    em.logger.removeHandler(cap)

rate = hits / tried if tried else 0.0
check("10 good pairs all hit", hits == 10)
check("20 raising pairs count as tried (30 total, not 10)", tried == 30)
check("the eval gate now refuses the flip",
      tried < 10 or rate < em._EVAL_GATE)
warns = [r for r in cap.records if r.levelno == logging.WARNING]
check("first eval failure is a WARNING, the rest stay at DEBUG",
      len(warns) == 1 and sum(r.levelno == logging.DEBUG for r in cap.records) == 19)

# Healthy corpus: every pair evaluates and the gate still passes.
hits, tried = em._neighborhood_eval(_Src(broken_from=99), _Dst(), sample)
check("healthy corpus passes the gate",
      tried == 30 and hits == 30 and hits / tried >= em._EVAL_GATE)

# A pair that raises AFTER it was counted (None vector on the neighbour)
# is one miss, not two.
em.logger.addHandler(cap)
em.logger.propagate = False
try:
    hits, tried = em._neighborhood_eval(_Src(broken_from=99),
                                        _Dst(none_for={"nb0"}), sample[:8])
finally:
    em.logger.removeHandler(cap)
    em.logger.propagate = True
check("a pair failing after it was counted is counted once",
      tried == 8 and hits == 7)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
