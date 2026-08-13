"""Pure tests for the trending/zeitgeist discovery ingredient (Block 4).

    python tests/test_trending.py
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.recommendations_engine import _trending_fresh, _trending_line

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


now = datetime(2026, 8, 13, 12, 0)

check("fresh cache (1h old) is fresh",
      _trending_fresh({"fetched_at": (now - timedelta(hours=1)).isoformat()}, now))
check("stale cache (25h old) is not fresh",
      not _trending_fresh({"fetched_at": (now - timedelta(hours=25)).isoformat()}, now))
check("missing fetched_at -> not fresh", not _trending_fresh({}, now))
check("garbage fetched_at -> not fresh, no raise",
      not _trending_fresh({"fetched_at": "not-a-date"}, now))

check("empty titles -> empty line (prompt line drops out)",
      _trending_line([]) == "")
line = _trending_line([f"Movie {i} (202{i % 10})" for i in range(15)])
check("line caps at 10 titles", line.count("Movie") == 10)
check("line carries the ONLY-if-fit guard",
      "ONLY those that genuinely fit the taste profile" in line)
check("single line, no availability wording",
      "\n" not in line and "stream" not in line.lower())

# ── wiring asserts ───────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
src = (root / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
check("discovery branch fetches trending",
      "_trending_line(await _get_trending(cat))" in src)
check("prompt carries the optional trend block",
      "{context_line}{trend_block}" in src)
check("music is excluded", 'if cat == "music":\n        return []' in src)
check("anime uses the JP-animation discover path",
      '"with_origin_country": "JP"' in src)
check("cache key + TTL present",
      'f"tmdb_trending:{cat}"' in src and "_TRENDING_TTL_H = 24" in src)
check("no soulsync request endpoint anywhere in the engine",
      "/api/v1/request" not in src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
