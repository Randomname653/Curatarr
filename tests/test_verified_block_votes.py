"""Verified block: vote mass rides the Rating line.

TMDB vote_count / OMDb imdbVotes were harvested since forever but never
reached the judge — 7.4/10 read identically at 51 votes and at 500k.
Contract: numeric rating + positive votes -> "7.4/10 (24,193 votes)";
no votes / garbage votes -> plain "7.4/10"; content-rating strings stay
on their own line untouched.

    python tests/test_verified_block_votes.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


from src.services.media_enricher import format_verified_block

out = format_verified_block({"title": "X", "rating": 7.4, "vote_count": 24193})
check("votes fold into the Rating line", "Rating: 7.4/10 (24,193 votes)" in out)

out = format_verified_block({"title": "X", "rating": 7.4})
check("no votes -> plain score", "Rating: 7.4/10" in out and "votes" not in out)

out = format_verified_block({"title": "X", "rating": 7.4, "vote_count": 0})
check("zero votes -> no empty suffix", "Rating: 7.4/10" in out and "votes" not in out)

out = format_verified_block({"title": "X", "rating": 7.4, "vote_count": "N/A"})
check("garbage vote_count -> plain score", "Rating: 7.4/10" in out and "votes" not in out)

out = format_verified_block({"title": "X", "rating": "PG-13 - Teens",
                             "vote_count": 24193})
check("content-rating strings stay untouched",
      "Content rating: PG-13 - Teens" in out and "votes" not in out)

src = (Path(__file__).resolve().parents[1]
       / "src/services/media_enricher.py").read_text(encoding="utf-8")
check("build_verified_data passes vote_count through",
      '"vote_count":     pick(raw.get("vote_count")' in src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
