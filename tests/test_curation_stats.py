"""Tests for the curation report aggregation (Block 8).

    python tests/test_curation_stats.py
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.curation_stats import _month_range, aggregate_resolutions

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


now = datetime(2026, 8, 13)

keys = _month_range(3, now)
check("month range chronological incl. year rollover handling",
      keys == ["2026-06", "2026-07", "2026-08"])
check("12-month range crosses the year boundary",
      _month_range(12, datetime(2026, 2, 1))[0] == "2025-03")

ROWS = [
    ("deleted", "consensus", datetime(2026, 8, 2)),
    ("deleted", "override", datetime(2026, 8, 5)),
    ("kept", "override", datetime(2026, 7, 20)),
    ("kept", "consensus", datetime(2026, 6, 1)),
    ("deleted", "consensus", datetime(2025, 1, 1)),   # outside the window
    ("deleted", "consensus", None),                   # garbage timestamp
]
out = aggregate_resolutions(ROWS, months=3, now=now)
check("three buckets, oldest first",
      [b["month"] for b in out] == ["2026-06", "2026-07", "2026-08"])
aug = out[2]
check("august counts 2 deleted (1 consensus + 1 override)",
      aug["deleted"] == 2 and aug["consensus"] == 1 and aug["overrides"] == 1)
jul = out[1]
check("july counts the override keep", jul["kept"] == 1 and jul["overrides"] == 1)
check("out-of-window and None rows ignored",
      sum(b["deleted"] + b["kept"] for b in out) == 4)
check("quiet month stays present, zeroed",
      out[0]["kept"] == 1 and out[0]["deleted"] == 0)

# ── wiring asserts ───────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
main = (root / "src/main.py").read_text(encoding="utf-8")
check("stats router registered at /api/stats",
      'prefix="/api/stats"' in main)

st = (root / "src/routers/stats.py").read_text(encoding="utf-8")
check("both endpoints admin-gated", st.count("require_admin") >= 3)
check("narrative cached per user+year", "curation_narrative:" in st)

html = (root / "frontend/index.html").read_text(encoding="utf-8")
check("'report' is in the admin view gate",
      "'reclassify','report'" in html)
for frag in ["report-view", "loadReport", "writeYearlyReview",
             "Stubbornness Index", "_repBar"]:
    check(f"frontend has {frag}", frag in html)
check("no chart library referenced",
      "chart.js" not in html.lower() and "echarts" not in html.lower())

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
