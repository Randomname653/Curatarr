"""Pure tests for upgrade curation (Block 7).

    python tests/test_upgrade_candidates.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.upgrade_curation import _judge

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


LOVE = {"reason": "rewatched (3 plays)"}
P720 = {"title": "Old Favorite", "category": "movie", "resolution": "720",
        "size_mb": 2048, "mb_per_min": 20}
P4K = {"title": "Shiny", "category": "movie", "resolution": "4k",
       "size_mb": 40960, "mb_per_min": 90}
LEAN = {"verdict": "lean", "mb_per_min": 9.0, "median": 30.0}
NORMAL = {"verdict": "normal", "mb_per_min": 28.0, "median": 30.0}

r = _judge(P720, LOVE, None)
check("720p + rewatch -> candidate", r and "below 1080p" in r["weakness"])
check("row carries love reason + size",
      r["love_reason"] == LOVE["reason"] and r["size_gb"] == 2.0)

check("4K + rewatch -> NOT a candidate (file is fine)",
      _judge(P4K, LOVE, NORMAL) is None)

p1080 = dict(P4K, resolution="1080")
r = _judge(p1080, {"reason": "loved a Curatarr recommendation: great"}, LEAN)
check("1080p but lean bitrate + weighted feedback -> candidate",
      r and "lean bitrate" in r["weakness"])

check("lean file WITHOUT love -> None (love is the gate)",
      _judge(P720, None, LEAN) is None)
check("sd resolution counts as weak",
      _judge(dict(P720, resolution="sd"), LOVE, None) is not None)
check("normal 1080p + love -> None",
      _judge(p1080, LOVE, NORMAL) is None)

# ── wiring asserts ───────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
rec = (root / "src/routers/recommendations.py").read_text(encoding="utf-8")
check("upgrade endpoint admin-gated",
      '@router.get("/upgrade-candidates")' in rec
      and "require_admin" in rec.split("upgrade-candidates")[1][:300])
check("redundancy endpoint admin-gated + duplicate_report finally called",
      '@router.get("/redundancy")' in rec
      and "duplicate_report" in rec.split('"/redundancy"')[1][:600])

uc = (root / "src/services/upgrade_curation.py").read_text(encoding="utf-8")
check("no arr writes in upgrade curation",
      "httpx" not in uc and "post(" not in uc.lower())

html = (root / "frontend/index.html").read_text(encoding="utf-8")
for frag in ["loadUpgrades", "loadRedundancy", "upgrade-content", "redundancy-content"]:
    check(f"frontend has {frag}", frag in html)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
