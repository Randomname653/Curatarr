"""Tests for the recommendation_followup proactive trigger (Block 4).

Fakes app_state.get_state (the detector imports it lazily per call), so no
DB is touched: detector peek semantics, asked-subject suppression, runner
ordering and the disabled-toggle path.

    python tests/test_recommendation_followup.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.services.app_state as app_state  # noqa: E402
import src.services.proactive_messages as pm  # noqa: E402

# The SAME normalizer the detector and _load_asked_subjects use
# (series_progress variant — keeps apostrophes, unlike library_memory's).
normalize_title = pm.normalize_title

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


QUEUE = [
    {"rec_id": 7, "title": "The 'Burbs", "category": "movie",
     "watched_at": "2026-08-10T20:00:00"},
    {"rec_id": 9, "title": "Kill la Kill", "category": "anime",
     "watched_at": "2026-08-11T21:00:00"},
]
_state = {"rec_watch_hits:user_id=1": json.dumps(QUEUE)}
app_state.get_state = lambda key, default=None: _state.get(key, default)

# ── detector ─────────────────────────────────────────────────────────────────

hit = pm.detect_recommendation_followup(1, {})
check("oldest unasked hit is returned first",
      hit and hit["title"] == "The 'Burbs" and hit["rec_id"] == 7)
check("payload carries trigger_type + category",
      hit["trigger_type"] == "recommendation_followup"
      and hit["category"] == "movie")

hit2 = pm.detect_recommendation_followup(1, {})
check("peek semantics: second call returns the same hit (never consumes)",
      hit2 and hit2["rec_id"] == 7)

asked = {"titles": {normalize_title("The 'Burbs")}, "series": {}}
hit3 = pm.detect_recommendation_followup(1, asked)
check("asked title is skipped -> next hit", hit3 and hit3["rec_id"] == 9)

asked_series = {"titles": set(),
                "series": {normalize_title("The 'Burbs"): None,
                           normalize_title("Kill la Kill"): 3}}
check("asked series suppress too (show/anime index there)",
      pm.detect_recommendation_followup(1, asked_series) is None)

check("empty queue -> None (user 2 has no hits)",
      pm.detect_recommendation_followup(2, {}) is None)

_state["rec_watch_hits:user_id=3"] = "{not json"
check("corrupt queue state -> None, no raise",
      pm.detect_recommendation_followup(3, {}) is None)

# ── runner ordering + toggle (all other types marked recently_fired, so no
#    other detector body ever runs -> no DB access) ──────────────────────────

ALL_TYPES = {t["type"] for t in pm.TRIGGER_TYPES}
others = ALL_TYPES - {"recommendation_followup"}
now = datetime(2026, 8, 11, 12, 0, 0)

res = pm._run_all_triggers([], now, 1, recently_fired=others,
                           disabled=set(), asked_subjects={})
check("runner returns the followup FIRST (before any other detector)",
      res and res["trigger_type"] == "recommendation_followup")

res = pm._run_all_triggers([], now, 1, recently_fired=others,
                           disabled={"recommendation_followup"},
                           asked_subjects={})
check("settings toggle (disabled) suppresses it", res is None)

res = pm._run_all_triggers([], now, 1,
                           recently_fired=others | {"recommendation_followup"},
                           disabled=set(), asked_subjects={})
check("1-day type cooldown (recently_fired) suppresses it", res is None)

check("TRIGGER_TYPES lists recommendation_followup first (settings UI)",
      pm.TRIGGER_TYPES[0]["type"] == "recommendation_followup")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
