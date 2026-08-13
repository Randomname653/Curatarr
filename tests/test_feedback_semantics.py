"""Tests for the feedback-semantics fixes (evaluation package 1, commit A):
dislike ratchet, latest-statement-wins scoring, asymmetric elevation,
aversion decay/cap, mood_aversion removal, pillar scores.

    python tests/test_feedback_semantics.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.taste_vectors import merge_feedback_into_vector

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── the ratchet is gone: praise undoes a dislike ─────────────────────────────

v = merge_feedback_into_vector({}, {
    "title": "Dark", "sentiment": "negative",
    "genre_aspects": ["bleakness", "time travel"], "reason": "too bleak"})
check("negative adds dislike + aversions",
      v["disliked_titles"] == ["Dark"]
      and abs(v["theme_aversion"]["bleakness"] - 0.15) < 1e-9)

v = merge_feedback_into_vector(v, {
    "title": "Dark", "sentiment": "positive",
    "genre_aspects": ["bleakness"], "reason": "grew on me"})
check("later praise removes the title from disliked_titles",
      v["disliked_titles"] == [])
check("praise walks the NAMED aversion back (0.15 - 0.15 -> key dropped)",
      "bleakness" not in v["theme_aversion"])
check("unnamed aversions stay untouched",
      abs(v["theme_aversion"]["time travel"] - 0.15) < 1e-9)

v2 = merge_feedback_into_vector(
    {"theme_aversion": {"gore": 0.5}},
    {"title": "A", "sentiment": "positive", "genre_aspects": ["gore"],
     "reason": "x", "weight": 2.0})
check("weighted praise lowers by 0.30",
      abs(v2["theme_aversion"]["gore"] - 0.20) < 1e-9)

v3 = merge_feedback_into_vector(
    {"theme_aversion": {"gore": 0.25}},
    {"title": "B", "sentiment": "positive", "genre_aspects": ["gore"],
     "reason": "x", "weight": 2.0})
check("floor: score <= 0.01 deletes the key",
      "gore" not in v3["theme_aversion"])

# ── key cap ──────────────────────────────────────────────────────────────────

v4 = {"theme_aversion": {f"aspect{i}": 0.1 + i * 0.01 for i in range(25)}}
v4 = merge_feedback_into_vector(v4, {
    "title": "C", "sentiment": "negative", "genre_aspects": ["newest"],
    "reason": "x"})
check("negative merge caps theme_aversion at 20 strongest keys",
      len(v4["theme_aversion"]) == 20
      and "aspect24" in v4["theme_aversion"]      # strongest survives
      and "aspect0" not in v4["theme_aversion"])  # weakest culled

# ── wiring asserts ───────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
re_src = (root / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
i_neg = re_src.find('if _sent == "negative":\n            feedback_swing')
i_pos = re_src.find('elif _sent == "positive":', i_neg)
i_dis = re_src.find("elif _tl in disliked_lc:", i_pos)
check("scorer order: negative -> positive -> bare dislike list (latest wins)",
      -1 < i_neg < i_pos < i_dis)
check("bare-dislike branch no longer ORed with sentiment",
      '_sent == "negative" or _tl in disliked_lc' not in re_src)

em = (root / "src/services/episodic_memory.py").read_text(encoding="utf-8")
check("elevation is asymmetric (x2 only for negative verdicts)",
      'weight=2.0 if sentiment == "negative" else 1.0' in em)

te = (root / "src/services/taste_engine.py").read_text(encoding="utf-8")
check("carry-over decays theme_aversion (60d half-life)",
      "0.5 ** (days / 60.0)" in te)
check("carry-over drops <0.05 scores and caps at 20",
      ">= 0.05" in te and "[:20]" in te)
check("mood_aversion removed from the blob write",
      '"mood_aversion": old_feedback' not in te)

pl = (root / "src/services/pillars.py").read_text(encoding="utf-8")
check("pillars aversion line carries scores",
      '({v:.2f})' in pl and "(0-1)" in pl)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
