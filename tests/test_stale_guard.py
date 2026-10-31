"""Implausible-mass-staleness guard (SoulSync port, MIT — their #828).

An arr that is down contributes ZERO items to the audit's ground truth,
so every one of its chroma docs would classify as "gone" and the zombie
walk would mass-rebuild against stale prefetch data. The guard vetoes a
whole service when its gone-share is implausible: infrastructure failure
is the likelier explanation than mass deletion.

    python tests/test_stale_guard.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _checks import check  # noqa: E402


from src.services.stale_guard import is_implausible_mass_stale as g

check("small sets never blocked (a 3-doc service may really lose all 3)",
      not g(3, 3) and not g(10, 19))
check("over half of a big set -> blocked", g(600, 1000) and g(11, 20))
check("exactly half passes (strict >)", not g(10, 20) and not g(500, 1000))
check("zero/negative inputs safe", not g(0, 100) and not g(5, 0))

en = (Path(__file__).resolve().parents[1] / "src/routers/enrichment.py").read_text(encoding="utf-8")
check("zombie walk: classify first, guard PER SERVICE, then act",
      "is_implausible_mass_stale(len(bucket" in en
      and "guard_skipped_" in en
      and en.index("_cand.setdefault") < en.index("is_implausible_mass_stale(len"))
check("guarded skip is loud and counted in the result",
      "implausible (arr down?)" in en)

check.done()
