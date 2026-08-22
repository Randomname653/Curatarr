"""Music deletion: listening-depth protection + spoken-word form guard.

Live failure 2026-08-18 (owner debate paste): Jochen Malmsheimer — German
Kabarett legend, 174 recorded plays — was pitched for deletion at 0.69
confidence. Data was CORRECT (Last.fm bio, kabarett/cabaret genres); the
failure was evaluation: (a) del_score has no listening term, so a
174-play artist ranked on pure taste-vector mismatch against the owner's
electronic music centroids (spoken word = maximal distance, worsened by
the known music cluster-gate collapse); (b) nothing told judge/pitcher
that a spoken-word artist must not be measured with music metrics — the
pitch called dense Kabarett "background noise".

    python tests/test_play_protection.py
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


from src.services.recommendations_engine import (
    _play_protection, _is_spoken_word, _get_vocab_guideline)

# ── _play_protection curve ───────────────────────────────────────────────────

check("0 plays -> no protection", _play_protection(0) == 0.0)
check("1-2 plays -> still 0 (a single old play stays a CUT signal)",
      _play_protection(1) == 0.0 and _play_protection(2) == 0.0)
check("3 plays -> small protection", 0.0 < _play_protection(3) < 10.0)
check("30 plays -> meaningful protection", 15.0 < _play_protection(30) < 25.0)
check("174 plays (Malmsheimer) -> capped 30",
      _play_protection(174) == 30.0)
check("monotonic non-decreasing",
      all(_play_protection(a) <= _play_protection(b)
          for a, b in [(3, 5), (5, 20), (20, 100), (100, 5000)]))
check("cap holds at absurd counts", _play_protection(10**6) == 30.0)

# ── spoken-word detection ────────────────────────────────────────────────────

check("kabarett/cabaret/audiobook detected",
      _is_spoken_word("kabarett, german")
      and _is_spoken_word("comedy, cabaret, german")
      and _is_spoken_word("german audiobook reader"))
check("plain music genres do NOT trigger",
      not _is_spoken_word("frenchcore, hardcore")
      and not _is_spoken_word("comedy")     # musical comedy stays music
      and not _is_spoken_word("hip hop, deutschrap"))
check("spoken-word vocab guideline is selected for cabaret music",
      "LANGUAGE performer" in _get_vocab_guideline("music", "comedy, cabaret, german"))
check("plain music keeps the music guideline",
      "NOT the audio" in _get_vocab_guideline("music", "frenchcore"))

# ── wiring ───────────────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
re_src = (root / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
check("del_score subtracts the protection",
      "+ drop_penalty - play_prot" in re_src)
check("plays come from ONE grouped watch_history query (name + mbid maps)",
      "_plays_by_name" in re_src and "_plays_by_mbid" in re_src
      and "group_by(_f.lower(_W.series_title)" in re_src)
check("protected artists are logged with counts",
      "listening-depth protection on" in re_src)

# ── skips are not listening depth ───────────────────────────────────────────
# 35,883 of the Spotify rows are tracks the user abandoned. Counting them as
# plays handed protection to artists they demonstrably keep skipping: on the
# live library 473 artists drew protection from skips alone, one of them 20.2
# points from 28 plays of which 25 were skipped. Heavy rotation is unaffected
# either way — log1p caps at 30 from ~148 plays on.
from src.services.recommendations_engine import REAL_LISTEN_MS

check("a real listen threshold exists and mirrors the importer's 2-minute rule",
      REAL_LISTEN_MS == 120_000)
check("the play map counts completed plays OR substantial ones, not every row",
      "_or(_W.completed == True" in re_src
      and "_W.view_offset_ms >= REAL_LISTEN_MS" in re_src)
check("...and still scopes to this user's music",
      "_W.media_type == \"music\"" in re_src and "_W.user_id == user_id" in re_src)

pl = (root / "src/services/pillars.py").read_text(encoding="utf-8")
check("judge evidence carries the FORM guard for spoken-word music",
      "FORM: spoken-word / cabaret" in pl and "+ form_line" in pl)
check("form guard also reads ENRICHED genres (lidarr genres are often empty)",
      "_get_cached_rating(item, category)" in pl)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
