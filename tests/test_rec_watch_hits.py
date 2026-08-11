"""Pure tests for the rec-watch matching core (_match_hits_to_recs).

    python tests/test_rec_watch_hits.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.plex_sync import _match_hits_to_recs  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


RECS = [
    {"id": 1, "user_id": 1, "title": "Kill la Kill", "category": "anime",
     "tmdb_id": None, "plex_rating_key": "677173"},
    {"id": 2, "user_id": 1, "title": "The 'Burbs", "category": "movie",
     "tmdb_id": 11974, "plex_rating_key": None},
    {"id": 3, "user_id": 2, "title": "Dark", "category": "show",
     "tmdb_id": 70523, "plex_rating_key": "12345"},
]

# episode row carries the SERIES rating key as identity_key
m = _match_hits_to_recs(
    [{"user_id": 1, "identity_key": "677173", "title": "Episode 4",
      "series_title": "Kill la Kill", "tmdb_id": None}], RECS)
check("episode hit matches series rec via rating key",
      len(m) == 1 and m[0]["rec_id"] == 1 and m[0]["category"] == "anime")

# tmdb match with a retitled item
m = _match_hits_to_recs(
    [{"user_id": 1, "identity_key": "999999", "title": "Burbs, The (1989)",
      "series_title": None, "tmdb_id": 11974}], RECS)
check("tmdb id match despite retitle", len(m) == 1 and m[0]["rec_id"] == 2)

# normalized-title fallback (no ids at all)
m = _match_hits_to_recs(
    [{"user_id": 1, "identity_key": None, "title": "kill-la-kill",
      "series_title": None, "tmdb_id": None}], RECS)
check("normalized title fallback", len(m) == 1 and m[0]["rec_id"] == 1)

# user scoping: user 1's play must not match user 2's rec
m = _match_hits_to_recs(
    [{"user_id": 1, "identity_key": "12345", "title": "Dark",
      "series_title": "Dark", "tmdb_id": 70523}], RECS)
check("cross-user rec is not matched", len(m) == 0)

# non-recommended play matches nothing
m = _match_hits_to_recs(
    [{"user_id": 1, "identity_key": "555", "title": "Some Other Film",
      "series_title": None, "tmdb_id": 42}], RECS)
check("unrelated play matches nothing", len(m) == 0)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
