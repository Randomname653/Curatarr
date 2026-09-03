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

# ── harvest sweep: the other dropped fields ────────────────────────────────

out = format_verified_block({"title": "X", "rating": 6.2, "scored_by": 48187})
check("MAL scored_by is the vote fallback", "Rating: 6.2/10 (48,187 votes)" in out)

out = format_verified_block({"title": "X", "rating": 6.2, "vote_count": 100,
                             "scored_by": 48187})
check("TMDB/OMDb votes win over scored_by", "(100 votes)" in out)

out = format_verified_block({"title": "X", "rating": 6.2,
                             "anilist_popularity": 45231})
check("AniList popularity gets honest list wording, not fake votes",
      "Rating: 6.2/10 (on 45,231 AniList lists)" in out)

out = format_verified_block({"title": "X", "rating": 6.2, "scored_by": 48187,
                             "anilist_popularity": 45231})
check("real votes beat list-adds", "(48,187 votes)" in out
      and "AniList lists" not in out)

out = format_verified_block({"title": "X", "runtime_min": 122})
check("movie runtime finally prints", "Runtime: 122 min" in out)

out = format_verified_block({"title": "X", "runtime_min": 24,
                             "episodes_total": 12})
check("episodic titles keep the Format line, no duplicate Runtime",
      "12 episodes x 24 min" in out and "Runtime:" not in out)

out = format_verified_block({"title": "X", "listeners": 61234,
                             "playcount": 5470000})
check("artist playcount rides the Community line",
      "Community: 61,234 Last.fm listeners, 5,470,000 plays" in out)

out = format_verified_block({"title": "X", "listeners": 61234})
check("no playcount -> listeners only",
      "Community: 61,234 Last.fm listeners" in out and "plays" not in out)

out = format_verified_block({"title": "X", "disambiguation": "UK rock band"})
check("MB disambiguation renders", "Disambiguation: UK rock band" in out)

out = format_verified_block({"title": "X", "rating": 7.4,
                             "content_rating": "R"})
check("OMDb Rated renders next to the numeric score",
      "Rating: 7.4/10" in out and "Content rating: R" in out)

src = (Path(__file__).resolve().parents[1]
       / "src/services/media_enricher.py").read_text(encoding="utf-8")
check("build_verified_data passes vote_count through",
      '"vote_count":     pick(raw.get("vote_count")' in src)
check("fetchers harvest the sweep fields",
      '"anilist_popularity": media.get("popularity")' in src
      and '"content_rating": _na(d.get("Rated"))' in src
      and '"playcount":       data.get("playcount")' in src
      and '"disambiguation": data.get("disambiguation", "")' in src)
check("chat fast profile reads the real runtime key",
      '"runtime":       raw.get("runtime_min")' in src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
