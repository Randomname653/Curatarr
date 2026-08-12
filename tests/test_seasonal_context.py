"""Pure tests for the seasonal/temporal context (Block 2).

    python tests/test_seasonal_context.py
"""
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.llm_utils import seasonal_context
from src.services.taste_vectors import compute_temporal_patterns
from src.services.recommendations_engine import _rhythm_line

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── seasonal_context windows ─────────────────────────────────────────────────

check("October is Halloween season",
      "Halloween" in seasonal_context(datetime(2026, 10, 20)))
check("mid-August is summer",
      "summer" in seasonal_context(datetime(2026, 8, 12)))
check("Dec 10 is the holiday season",
      "holiday" in seasonal_context(datetime(2026, 12, 10)))
check("Dec 28 is plain winter",
      "winter" in seasonal_context(datetime(2026, 12, 28)))
check("Nov 30 is the holiday season (window starts Nov 25)",
      "holiday" in seasonal_context(datetime(2026, 11, 30)))
check("April is spring", "spring" in seasonal_context(datetime(2026, 4, 15)))
check("September is autumn", "autumn" in seasonal_context(datetime(2026, 9, 20)))
check("month name included", "August" in seasonal_context(datetime(2026, 8, 12)))
check("single line, no availability wording",
      "\n" not in seasonal_context() and "stream" not in seasonal_context().lower())

# ── compute_temporal_patterns: dicts AND objects ─────────────────────────────

stamps = [datetime(2026, 8, 7, 21, 0),   # Friday evening
          datetime(2026, 8, 8, 22, 0),   # Saturday evening
          datetime(2026, 8, 9, 20, 30)]  # Sunday evening
as_dicts = [{"viewed_at": s} for s in stamps]
as_objs = [SimpleNamespace(viewed_at=s) for s in stamps]

d = compute_temporal_patterns(as_dicts)
o = compute_temporal_patterns(as_objs)
check("dict entries: all evening", d["time_of_day_dist"]["evening"] == 1.0)
check("object entries give identical result", d == o)
check("weekday distribution sums to ~1 (3-decimal rounding)",
      abs(sum(d["day_of_week_dist"].values()) - 1.0) < 0.01)
check("entry without viewed_at skipped, no crash",
      compute_temporal_patterns([{"title": "x"}])["time_of_day_dist"]["night"] == 0.0)
check("empty list -> zeroed dists",
      compute_temporal_patterns([])["time_of_day_dist"]["evening"] == 0.0)

# ── _rhythm_line thresholds ──────────────────────────────────────────────────

ts_strong = {"temporal": {
    "time_of_day_dist": {"night": 0.05, "morning": 0.05, "afternoon": 0.1, "evening": 0.8},
    "day_of_week_dist": {"0": 0.1, "1": 0.1, "2": 0.1, "3": 0.1, "4": 0.1, "5": 0.25, "6": 0.25},
}}
line = _rhythm_line(ts_strong)
check("pronounced pattern -> evening + weekend-heavy",
      "mostly evening" in line and "weekend-heavy" in line)

ts_flat = {"temporal": {
    "time_of_day_dist": {"night": 0.25, "morning": 0.25, "afternoon": 0.25, "evening": 0.25},
    "day_of_week_dist": {str(i): 1 / 7 for i in range(7)},
}}
check("flat distribution -> silent", _rhythm_line(ts_flat) == "")
check("missing temporal -> silent, no crash", _rhythm_line({}) == "")

# ── wiring asserts ───────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
te = (root / "src/services/taste_engine.py").read_text(encoding="utf-8")
check("taste_engine stores temporal in the result",
      '"temporal": compute_temporal_patterns(entries)' in te)
check("taste_engine persists temporal into type_summaries",
      '"temporal": res.get("temporal", {})' in te)
re_src = (root / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
check("both prompts carry the context line",
      re_src.count("{context_line}") == 2 and "seasonal_context()" in re_src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
