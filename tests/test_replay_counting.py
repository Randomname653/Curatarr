"""Replay counting: a replay must be a second real viewing.

The counter used to be ``total_plays - distinct_episodes`` over raw history
rows, which produced three kinds of phantom replay on live data:

* a resumed episode (logged once partway through, once when finished) —
  Psycho-Pass reported "4 replays" from four such pairs and zero rewatches;
* repeated ABANDONED starts — Valkyrie Drive reported "3 replays" for an
  episode the user never once finished, inverting the signal entirely;
* a re-import that shifted timestamps by a whole hour, and same-second
  double writes.

    python tests/test_replay_counting.py
"""
import sys
from datetime import datetime, timedelta
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


from src.services.series_progress import (
    _count_replays, _is_real_view, compute_watch_progress, count_real_views)

T0 = datetime(2026, 1, 1, 20, 0)


def ep(season, episode, when, completed=True, dur=None, off=None, mt="anime"):
    return {"media_type": mt, "series_title": "Show", "title": "Show",
            "season": season, "episode": episode, "viewed_at": when,
            "completed": completed, "duration_ms": dur, "view_offset_ms": off}


# ── _is_real_view ───────────────────────────────────────────────────────────
check("completed flag makes a real view", _is_real_view(ep(1, 1, T0)))
check("abandoned start is not a view",
      not _is_real_view(ep(1, 1, T0, completed=False, dur=1000, off=330)))
check("90% of runtime counts even without the flag",
      _is_real_view(ep(1, 1, T0, completed=False, dur=1000, off=900)))
check("89% does not",
      not _is_real_view(ep(1, 1, T0, completed=False, dur=1000, off=890)))
check("no completion data at all is not a view",
      not _is_real_view(ep(1, 1, T0, completed=False)))

# ── the resume artifact (the Psycho-Pass case) ──────────────────────────────
resumed = [ep(1, 1, T0, completed=False, dur=1000, off=898),
           ep(1, 1, T0 + timedelta(days=4))]
check("partial view + later finished view = one viewing, no replay",
      _count_replays(resumed) == 0)

# ── repeated abandonment (the Valkyrie Drive case) ──────────────────────────
abandoned = [ep(1, 1, T0 + timedelta(days=d), completed=False, dur=1000, off=460)
             for d in (0, 30, 160, 200)]
check("four abandoned starts are zero replays", _count_replays(abandoned) == 0)
prog = compute_watch_progress(abandoned, "Show")
check("...and are reported as abandoned starts instead",
      prog["abandoned_starts"] == 4 and prog["replays"] == 0)
check("the old formula would have called that 3 replays",
      prog["total_plays"] - prog["distinct_episodes"] == 3)

# ── genuine rewatches still count ───────────────────────────────────────────
genuine = [ep(1, 1, T0), ep(1, 1, T0 + timedelta(days=120)),
           ep(1, 2, T0), ep(1, 2, T0 + timedelta(days=120))]
check("same episodes finished again months later = 2 replays",
      _count_replays(genuine) == 2)

# ── duplicate writes of one viewing ─────────────────────────────────────────
check("two finished rows seconds apart are one viewing",
      _count_replays([ep(1, 5, T0), ep(1, 5, T0 + timedelta(seconds=34))]) == 0)
check("an hour-shifted re-import is one viewing",
      _count_replays([ep(1, 5, T0), ep(1, 5, T0 + timedelta(hours=1))]) == 0)
check("a rewatch the next day still counts",
      _count_replays([ep(1, 5, T0), ep(1, 5, T0 + timedelta(days=1))]) == 1)

# ── untagged libraries must not guess ───────────────────────────────────────
check("no episode numbers -> no replay claim",
      _count_replays([ep(None, None, T0), ep(None, None, T0 + timedelta(days=9))]) == 0)

# ── films ───────────────────────────────────────────────────────────────────
film = [ep(None, None, T0, mt="movie"),
        ep(None, None, T0 + timedelta(minutes=10), mt="movie"),
        ep(None, None, T0 + timedelta(days=200), mt="movie"),
        ep(None, None, T0 + timedelta(days=300), completed=False, dur=1000, off=100, mt="movie")]
check("film views: duplicates collapse, abandonment ignored",
      count_real_views(film) == 2)
check("no real views -> zero", count_real_views(
    [ep(None, None, T0, completed=False, dur=1000, off=10, mt="movie")]) == 0)

# ── the call sites actually use the corrected numbers ───────────────────────
pm = (Path(__file__).resolve().parents[1]
      / "src/services/proactive_messages.py").read_text(encoding="utf-8")
check("proactive rewatch detector reads prog['replays']",
      'replays = prog["replays"]' in pm
      and 'prog["total_plays"] - prog["distinct_episodes"]' not in pm)
check("film path counts real views, not rows",
      "count_real_views(plays)" in pm and "movie_counter[key] += 1" not in pm)

ps = (Path(__file__).resolve().parents[1]
      / "src/services/plex_sync.py").read_text(encoding="utf-8")
check("plex_sync promotes the unfinished row when the finished view arrives",
      "RESUME_WINDOW_DAYS" in ps and "_stale.completed = True" in ps)

# ── batch surfaces get a deterministic language ─────────────────────────────
from src.services.llm_utils import detect_user_language


class _ExplodingDB:
    def query(self, *a, **k):
        raise AssertionError("batch language detection must not read chat history")


check("no live message -> English, without consulting unrelated chat",
      detect_user_language(1, _ExplodingDB()) == "en")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
