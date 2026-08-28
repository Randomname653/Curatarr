"""A declared intention to watch must ACT, not just talk.

    python tests/test_watchlist_hook.py

The School-Live! discussion validated the declaration doctrine ("I shall
put it on my watchlist" ends the proposal in favour of Keep) and exposed
two honesty gaps behind it: the curator's announced downscale flag never
reached the /downscale work list (discussion keeps wrote verdict=NULL,
the list filtered source=='judge'), and the watchlist existed only as
prose. This suite pins the fixes: the deterministic post-turn scanner
recognises declarations, the flag it writes is profile-driven and
verdict-gated into the work list, and the watchlist write goes to the
DISCUSSING user's own plex.tv account — backend code, never the chat LLM.
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


# ── the Discover matcher: unambiguous or nothing ───────────────────────────

from src.services.plex_watchlist import pick_discover_match

HITS = [
    {"title": "School-Live!", "year": 2015, "type": "show", "ratingKey": "a1"},
    {"title": "School Live", "year": 2019, "type": "movie", "ratingKey": "a2"},
]
m = pick_discover_match(HITS, "School-Live!", media_type="anime")
check("the right series is picked for an anime declaration",
      m is not None and m["ratingKey"] == "a1")
check("the live-action film of the same name is not confused in",
      pick_discover_match(HITS, "School Live", media_type="movie")["ratingKey"] == "a2")

REORDER = [{"title": "Thus Spoke Rohan Kishibe", "year": 2021, "type": "show",
            "ratingKey": "r1"}]
check("reordered romanised names still match (the Kishibe-Rohan lesson)",
      pick_discover_match(REORDER, "Thus Spoke Kishibe Rohan",
                          media_type="show") is not None)

TWINS = [
    {"title": "Good Boy", "year": 2025, "type": "movie", "ratingKey": "g1"},
    {"title": "Good Boy", "year": 2026, "type": "movie", "ratingKey": "g2"},
]
check("same-name different-year twins without a year are refused, not guessed",
      pick_discover_match(TWINS, "Good Boy", media_type="movie") is None)
check("...and the year disambiguates them",
      pick_discover_match(TWINS, "Good Boy", year=2026,
                          media_type="movie")["ratingKey"] == "g2")
check("different words never match",
      pick_discover_match(HITS, "Momoiro Sisters", media_type="anime") is None)

# ── the wiring: scanner → row → work list → watchlist ──────────────────────

_em = (Path(__file__).resolve().parents[1]
       / "src/services/episodic_memory.py").read_text(encoding="utf-8")
check("the scanner knows a watch-declaration is a keep-directive",
      "DECLARED INTENTION TO WATCH" in _em
      and "WATCHLIST: yes" in _em)
check("...and a preference or question still is not",
      "is it worth watching?" in _em and "still NOT a directive" in _em)
check("the parser reads the WATCHLIST field",
      'key == "WATCHLIST"' in _em and "watchlist_flag" in _em)
check("the downscale flag is decided by the tech profile, not the LLM",
      "is_bloated_by_title" in _em
      and '"KEEP_WITH_FLAG" if bloated else None' in _em)
check("a discussion keep names its source honestly",
      'source="discussion"' in _em)
check("an existing protection upgrades to the flag, never downgrades",
      "if bloated and not existing.verdict" in _em)
check("the watchlist write happens outside the db session, best-effort",
      "add_to_watchlist(user_id, wtitle" in _em
      and "watchlist_fails" in _em)

_rec = (Path(__file__).resolve().parents[1]
        / "src/routers/recommendations.py").read_text(encoding="utf-8")
check("the /downscale work list is verdict-gated, not source-gated",
      'ProtectedMedia.verdict == "KEEP_WITH_FLAG")' in _rec
      and 'ProtectedMedia.source == "judge",\n                ProtectedMedia.verdict' not in _rec)

_pw = (Path(__file__).resolve().parents[1]
       / "src/services/plex_watchlist.py").read_text(encoding="utf-8")
check("the watchlist uses the DISCUSSING user's own token, not the server's",
      "user.plex_token" in _pw and "settings.PLEX_TOKEN" not in _pw)
check("the token is never logged",
      all("token" not in ln.lower() for ln in _pw.splitlines()
          if "logger." in ln))

_ch = (Path(__file__).resolve().parents[1]
       / "src/routers/chat.py").read_text(encoding="utf-8")
check("the curator announces exactly what the backend does — no more",
      "the backend then REALLY acts" in _ch
      and "announce exactly that, and\nnothing beyond it" in _ch)
check("the chat LLM still executes no library actions itself",
      "no_library_actions_rule" in _ch)

# ── size_norms: the deterministic bloat probe ──────────────────────────────

_sn = (Path(__file__).resolve().parents[1]
       / "src/services/size_norms.py").read_text(encoding="utf-8")
check("the bloat probe exists and answers False on unknown data",
      "def is_bloated_by_title" in _sn
      and 'return False' in _sn.split("def is_bloated_by_title")[1][:900])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
