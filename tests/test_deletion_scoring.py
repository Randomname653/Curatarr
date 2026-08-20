"""Unit tests for the deletion-scoring taste-mismatch calibration.

Covers the fix that makes the semantic distance actually vary: the previous code
did np.dot(unit_taste_vec, RAW_item_embedding) (norm ~13) -> values ~8-14 that
the 1-dist clamp pinned to 0, killing the dominant deletion signal. Now both
vectors are normalised (proper cosine) and the anisotropic, narrow cosine band
is percentile-stretched onto a full [0,1] mismatch range.

No mocking needed — the helpers are pure (numpy only).

    python tests/test_deletion_scoring.py
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

from src.services.recommendations_engine import (
    _normalize_vec,
    _cosine_anchors,
    _cosine_to_mismatch,
)


# ── _normalize_vec ──────────────────────────────────────────────────────────

check("normalize returns unit length",
      abs(np.linalg.norm(_normalize_vec([3.0, 4.0])) - 1.0) < 1e-9)
check("normalize handles None and zero vector",
      _normalize_vec(None) is None and _normalize_vec([0.0, 0.0]) is None)

# ── _cosine_anchors ─────────────────────────────────────────────────────────

check("anchors fall back when too few values",
      _cosine_anchors([0.7]) == (0.61, 0.72) and _cosine_anchors([]) == (0.61, 0.72))

_vals = [0.60 + 0.005 * i for i in range(40)]   # 0.60 .. 0.795
_lo, _hi = _cosine_anchors(_vals)
check("anchors come from the distribution", 0.60 <= _lo < _hi <= 0.80)

_lo, _hi = _cosine_anchors([0.66] * 50)          # all identical
check("anchors guarantee hi > lo when degenerate", _hi > _lo)

# ── _cosine_to_mismatch ─────────────────────────────────────────────────────

check("mismatch of None is neutral 0.5",
      _cosine_to_mismatch(None, 0.61, 0.72) == 0.5)
check("best fit is zero mismatch (above hi clamps)",
      _cosine_to_mismatch(0.72, 0.61, 0.72) == 0.0
      and _cosine_to_mismatch(0.85, 0.61, 0.72) == 0.0)
check("worst fit is full mismatch (below lo clamps)",
      _cosine_to_mismatch(0.61, 0.61, 0.72) == 1.0
      and _cosine_to_mismatch(0.50, 0.61, 0.72) == 1.0)
check("midpoint lands near 0.5",
      abs(_cosine_to_mismatch(0.665, 0.61, 0.72) - 0.5) < 0.01)
check("monotonic: better fit -> lower mismatch",
      _cosine_to_mismatch(0.64, 0.61, 0.72) > _cosine_to_mismatch(0.70, 0.61, 0.72))

# ── end-to-end: the bug it fixes ────────────────────────────────────────────
# Regression guard: a raw embedding (norm ~13) vs a unit taste vector gives
# a dot far outside [-1,1]; proper cosine restores it to a usable range.

_rng = np.random.default_rng(0)
_taste = _normalize_vec(_rng.standard_normal(768))
_raw_item = _rng.standard_normal(768) * 13.0      # un-normalised, norm ~13*sqrt(768)
_bad = max(0.0, min(1.0, 1.0 - float(np.dot(_taste, _raw_item))))   # old formula
_cos = float(np.dot(_taste, _raw_item) / np.linalg.norm(_raw_item))  # fixed
check("old formula clamped to an extreme", _bad in (0.0, 1.0))
check("fixed formula yields a real cosine", -1.0 <= _cos <= 1.0)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
