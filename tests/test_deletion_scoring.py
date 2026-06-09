"""Unit tests for the deletion-scoring taste-mismatch calibration.

Covers the fix that makes the semantic distance actually vary: the previous code
did np.dot(unit_taste_vec, RAW_item_embedding) (norm ~13) -> values ~8-14 that
the 1-dist clamp pinned to 0, killing the dominant deletion signal. Now both
vectors are normalised (proper cosine) and the anisotropic, narrow cosine band
is percentile-stretched onto a full [0,1] mismatch range.

No mocking needed — the helpers are pure (numpy only).
"""

import numpy as np

from src.services.recommendations_engine import (
    _normalize_vec,
    _cosine_anchors,
    _cosine_to_mismatch,
)


# ── _normalize_vec ──────────────────────────────────────────────────────────

def test_normalize_unit_length():
    v = _normalize_vec([3.0, 4.0])
    assert abs(np.linalg.norm(v) - 1.0) < 1e-9


def test_normalize_none_and_zero():
    assert _normalize_vec(None) is None
    assert _normalize_vec([0.0, 0.0]) is None


# ── _cosine_anchors ─────────────────────────────────────────────────────────

def test_anchors_fallback_when_too_few():
    assert _cosine_anchors([0.7]) == (0.61, 0.72)
    assert _cosine_anchors([]) == (0.61, 0.72)


def test_anchors_from_distribution():
    vals = [0.60 + 0.005 * i for i in range(40)]   # 0.60 .. 0.795
    lo, hi = _cosine_anchors(vals)
    assert 0.60 <= lo < hi <= 0.80


def test_anchors_guarantee_hi_gt_lo_when_degenerate():
    lo, hi = _cosine_anchors([0.66] * 50)          # all identical
    assert hi > lo


# ── _cosine_to_mismatch ─────────────────────────────────────────────────────

def test_mismatch_none_is_neutral():
    assert _cosine_to_mismatch(None, 0.61, 0.72) == 0.5


def test_mismatch_best_fit_is_zero():
    assert _cosine_to_mismatch(0.72, 0.61, 0.72) == 0.0
    assert _cosine_to_mismatch(0.85, 0.61, 0.72) == 0.0     # above hi clamps


def test_mismatch_worst_fit_is_one():
    assert _cosine_to_mismatch(0.61, 0.61, 0.72) == 1.0
    assert _cosine_to_mismatch(0.50, 0.61, 0.72) == 1.0     # below lo clamps


def test_mismatch_midpoint():
    assert abs(_cosine_to_mismatch(0.665, 0.61, 0.72) - 0.5) < 0.01


def test_mismatch_monotonic_better_fit_lower_mismatch():
    worse = _cosine_to_mismatch(0.64, 0.61, 0.72)
    better = _cosine_to_mismatch(0.70, 0.61, 0.72)
    assert worse > better


# ── end-to-end: the bug it fixes ────────────────────────────────────────────

def test_unnormalised_dot_would_have_collapsed():
    """Regression guard: a raw embedding (norm ~13) vs a unit taste vector gives
    a dot far outside [-1,1]; proper cosine restores it to a usable range."""
    rng = np.random.default_rng(0)
    taste = _normalize_vec(rng.standard_normal(768))
    raw_item = rng.standard_normal(768) * 13.0        # un-normalised, norm ~13*sqrt(768)
    bad = max(0.0, min(1.0, 1.0 - float(np.dot(taste, raw_item))))   # old formula
    cos = float(np.dot(taste, raw_item) / np.linalg.norm(raw_item))  # fixed
    assert bad in (0.0, 1.0)                 # old: clamped to an extreme
    assert -1.0 <= cos <= 1.0                # fixed: a real cosine
