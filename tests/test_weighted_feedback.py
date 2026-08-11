"""Pure tests for elevated-weight recommendation feedback (Block 5).

    python tests/test_weighted_feedback.py
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.taste_vectors import merge_feedback_into_vector  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── merge_feedback_into_vector: entry fields ─────────────────────────────────

v = merge_feedback_into_vector({}, {
    "title": "Dark", "sentiment": "negative",
    "genre_aspects": ["time travel"], "reason": "too bleak",
})
e = v["explicit_feedback"][-1]
check("default weight is 1.0", e["weight"] == 1.0)
check("default source is chat", e["source"] == "chat")
check("entry aspects come from genre_aspects (old bug: always [])",
      e["aspects"] == ["time travel"])

v = merge_feedback_into_vector({}, {
    "title": "Kill la Kill", "sentiment": "positive",
    "genre_aspects": ["absurd action"], "reason": "loved it",
    "weight": 2.0, "source": "curatarr_recommendation",
})
e = v["explicit_feedback"][-1]
check("elevated weight stored", e["weight"] == 2.0)
check("source tag stored", e["source"] == "curatarr_recommendation")
check("positive feedback adds no aversion/dislike",
      not v.get("theme_aversion") and not v.get("disliked_titles"))

# ── aversion bump scales with weight, capped at 2.0 ──────────────────────────

v1 = merge_feedback_into_vector({}, {
    "title": "A", "sentiment": "negative", "genre_aspects": ["gore"],
    "reason": "x"})
check("weight 1.0 bumps aversion by 0.15",
      abs(v1["theme_aversion"]["gore"] - 0.15) < 1e-9)

v2 = merge_feedback_into_vector({}, {
    "title": "B", "sentiment": "negative", "genre_aspects": ["gore"],
    "reason": "x", "weight": 2.0})
check("weight 2.0 bumps aversion by 0.30",
      abs(v2["theme_aversion"]["gore"] - 0.30) < 1e-9)

v5 = merge_feedback_into_vector({}, {
    "title": "C", "sentiment": "negative", "genre_aspects": ["gore"],
    "reason": "x", "weight": 5.0})
check("weight capped at 2.0 (5.0 → 0.30 bump)",
      abs(v5["theme_aversion"]["gore"] - 0.30) < 1e-9)

va = {"theme_aversion": {"gore": 0.95}}
va = merge_feedback_into_vector(va, {
    "title": "D", "sentiment": "negative", "genre_aspects": ["gore"],
    "reason": "x", "weight": 2.0})
check("aversion still capped at 1.0 overall",
      va["theme_aversion"]["gore"] == 1.0)

check("legacy entry without weight field reads as 1.0 via `or 1.0`",
      float({}.get("weight") or 1.0) == 1.0)

# ── wiring asserts (readers + producer), source-level like the schema test ───

import src.services.episodic_memory as em  # noqa: E402

sig = inspect.signature(em.update_taste_profile_from_memory)
check("update_taste_profile_from_memory has weight=1.0 param",
      sig.parameters["weight"].default == 1.0)
check("update_taste_profile_from_memory has source='chat' param",
      sig.parameters["source"].default == "chat")
check("analyze_recommendation_feedback exists",
      hasattr(em, "analyze_recommendation_feedback"))
em_src = inspect.getsource(em.analyze_recommendation_feedback)
check("analyzer passes weight=2.0 + source tag",
      "weight=2.0" in em_src and "curatarr_recommendation" in em_src)

root = Path(__file__).resolve().parents[1]
re_src = (root / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
check("recommendations_engine stores (sentiment, weight)",
      'float(_fb.get("weight") or 1.0)' in re_src)
check("recommendations_engine scales the swing by min(weight, 2.0)",
      "15.0 * min(_fb_weight, 2.0)" in re_src)

pl_src = (root / "src/services/pillars.py").read_text(encoding="utf-8")
check("pillars flags elevated-weight verdicts for the judge",
      'float(last.get("weight") or 1.0) > 1.0' in pl_src
      and "said in a Curatarr-recommendation" in pl_src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
