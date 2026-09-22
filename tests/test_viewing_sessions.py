"""Sittings from timestamps, "binge" only when one sitting earns it, subject
memory for every trigger type, and a bell that rotates instead of repeating.

2026-09-22, owner: the curator asked why he binged six episodes he had
watched an episode or two at a time over several days; and the same three
trigger types (new_genre "latin" eight times, low_completion nine times,
night_owl nine times) had filled the bell for three weeks. Real modules,
no mocks: these tests need the real settings and the real detectors.

    python tests/test_viewing_sessions.py
"""
import pathlib
import random
import sys
from datetime import datetime, timedelta

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.services import viewing_sessions as vs  # noqa: E402
from src.services import proactive_messages as pm  # noqa: E402
from src.config import settings  # noqa: E402

EP_MS = 1_422_083          # a 23.7-minute episode, like the owner's rows


def _play(series, ep, at, *, dur=EP_MS, off=None, done=True, media_type="anime"):
    return {"title": f"{series} E{ep}", "series_title": series, "media_type": media_type,
            "season": 1, "episode": ep, "viewed_at": at, "duration_ms": dur,
            "view_offset_ms": dur if off is None else off, "completed": done, "genres": "action"}


def _one_sitting(series="Mushoku", start=datetime(2026, 8, 7, 15, 48), n=6):
    return [_play(series, i + 13, start + timedelta(minutes=22 * i)) for i in range(n)]


def _four_evenings(series="Tokyo Ghoul", first=datetime(2026, 9, 13, 20, 0)):
    """Six episodes: 2, 1, 2, 1 over four evenings a day apart."""
    plays, ep = [], 1
    for day, count in enumerate((2, 1, 2, 1)):
        for k in range(count):
            plays.append(_play(series, ep, first + timedelta(days=day, minutes=25 * k)))
            ep += 1
    return plays


def test_consecutive_plays_form_one_sitting_and_earn_the_word_binge():
    plays = _one_sitting()
    now = plays[-1]["viewed_at"] + timedelta(hours=1)
    ss = vs.sittings(plays, now)
    assert len(ss) == 1 and ss[0]["episodes"] == 6 and ss[0]["plays"] == 6
    assert ss[0]["span_min"] == 110
    r = vs.rhythm(plays, now)
    assert r["binge"] is True and r["max_in_one_sitting"] == 6 and r["sittings"] == 1
    assert r["phrase"].startswith("6 episodes in one sitting"), r["phrase"]
    assert "1 h 50 min" in r["phrase"]


def test_an_episode_or_two_an_evening_is_a_routine_not_a_binge():
    plays = _four_evenings()
    now = plays[-1]["viewed_at"] + timedelta(hours=12)
    r = vs.rhythm(plays, now)
    assert r["sittings"] == 4 and r["episodes"] == 6 and r["max_in_one_sitting"] == 2
    assert r["binge"] is False
    assert "in one sitting" not in r["phrase"]
    assert "6 episodes over 4 sittings" in r["phrase"] and "an episode or two at a time" in r["phrase"], r["phrase"]


def test_abandoned_starts_and_old_plays_do_not_count():
    plays = _one_sitting(n=3)
    plays.append(_play("Mushoku", 99, plays[-1]["viewed_at"] + timedelta(minutes=22), off=EP_MS // 20, done=False))
    plays.append(_play("Mushoku", 1, plays[0]["viewed_at"] - timedelta(days=30)))     # outside the window
    now = plays[3]["viewed_at"] + timedelta(hours=1)
    ss = vs.sittings(plays, now)
    assert len(ss) == 1 and ss[0]["plays"] == 4 and ss[0]["episodes"] == 3, ss


def test_bulk_marked_rows_are_plays_not_episodes():
    """12 episodes of Kakegurui carried the same second; five of High School
    DxD sat 5 minutes apart after the Blu-ray swap. Neither is viewing."""
    stamp = datetime(2026, 9, 14, 15, 27)
    bulk = [_play("Kakegurui", i, stamp) for i in range(1, 13)]
    ss = vs.sittings(bulk, stamp + timedelta(hours=1))
    assert len(ss) == 1 and ss[0]["plays"] == 12 and ss[0]["episodes"] == 1 and ss[0]["phantoms"] == 11
    assert vs.rhythm(bulk, stamp + timedelta(hours=1))["binge"] is False
    assert pm.detect_binge(bulk, stamp + timedelta(hours=1)) is None
    assert pm.detect_series_completion(bulk, stamp + timedelta(hours=1)) is None, "12 rows, one viewing at most"
    swap = [_play("DxD", i, stamp + timedelta(minutes=5 * i)) for i in range(5)]
    assert vs.sittings(swap, stamp + timedelta(hours=1))[0]["episodes"] == 1
    # a genuinely fast watcher who skips intros is still counted: 20-minute gaps on 23.7-minute episodes
    quick = [_play("Quick", i, stamp + timedelta(minutes=20 * i)) for i in range(4)]
    assert vs.sittings(quick, stamp + timedelta(hours=2))[0]["episodes"] == 4


def test_detect_binge_fires_for_one_sitting_only_and_only_while_fresh():
    sitting = _one_sitting()
    now = sitting[-1]["viewed_at"] + timedelta(hours=1)
    hit = pm.detect_binge(sitting, now)
    assert hit and hit["type"] == "binge_episode" and hit["count"] == 6
    assert hit["span_minutes"] == 110 and hit["rhythm"].startswith("6 episodes in one sitting")
    assert "hours" not in hit, "the payload carries measured facts, not the window constant"
    evenings = _four_evenings()
    assert pm.detect_binge(evenings, evenings[-1]["viewed_at"] + timedelta(hours=1)) is None
    stale = sitting[-1]["viewed_at"] + timedelta(hours=settings.BINGE_SESSION_HOURS + 1)
    assert pm.detect_binge(sitting, stale) is None, "a sitting that ended hours ago is no longer 'just now'"


def test_series_completion_carries_the_rhythm():
    # two evenings of six, all inside the detector's 48-hour window
    plays = [_play("Tanya", i, datetime(2026, 9, 13, 18, 0) + timedelta(hours=(i // 6) * 24, minutes=25 * (i % 6)))
             for i in range(12)]
    now = plays[-1]["viewed_at"] + timedelta(hours=2)
    hit = pm.detect_series_completion(plays, now)
    assert hit and hit["episodes_watched"] == 12, hit
    assert hit["rhythm"].startswith("6 episodes in one sitting"), hit["rhythm"]
    assert "12 in the last 2 days over 2 sittings" in hit["rhythm"], hit["rhythm"]


def test_subject_memory_indexes_every_pattern_type_and_the_detectors_honour_it():
    now = datetime(2026, 9, 22, 12, 0)
    asked = {"tracks": set(), "titles": set(), "series": {},
             "subjects": {"new_genre:latin": now - timedelta(days=3),
                          "night_owl:the empire": now - timedelta(days=2),
                          "low_completion:show a": now - timedelta(days=5),
                          "low_completion:show b": now - timedelta(days=5),
                          "low_completion:show c": now - timedelta(days=5)}}
    assert pm._subject_asked(asked, "new_genre", "Latin", days=30, now=now)
    assert not pm._subject_asked(asked, "new_genre", "Latin", days=2, now=now), "a horizon that has passed re-opens the subject"
    assert not pm._subject_asked(asked, "new_genre", "holiday", days=30, now=now)
    assert not pm._subject_asked(None, "new_genre", "latin", days=30, now=now)

    recent = now - timedelta(days=3)
    entries = ([{"title": f"L{i}", "series_title": None, "media_type": "movie", "viewed_at": recent,
                 "genres": "latin", "duration_ms": 1, "view_offset_ms": 1, "completed": True} for i in range(4)]
               + [{"title": f"H{i}", "series_title": None, "media_type": "movie", "viewed_at": recent,
                   "genres": "holiday", "duration_ms": 1, "view_offset_ms": 1, "completed": True} for i in range(3)])
    assert pm.detect_new_genre(entries, now)["genre"] == "latin"
    assert pm.detect_new_genre(entries, now, asked)["genre"] == "holiday", "latin was asked three days ago"

    owl = [{"title": "The Empire", "series_title": None, "media_type": "movie",
            "last_viewed_at": now - timedelta(days=1, hours=9)},
           {"title": "Nine Blades", "series_title": None, "media_type": "movie",
            "last_viewed_at": now - timedelta(days=2, hours=9)}]
    assert pm.detect_night_owl(owl, now)["media_title"] == "The Empire"
    assert pm.detect_night_owl(owl, now, asked)["media_title"] == "Nine Blades"

    dropped = [{"title": t, "series_title": t, "media_type": "show", "viewed_at": recent,
                "duration_ms": 1_000_000, "view_offset_ms": 100_000, "completed": False, "genres": ""}
               for t in ("Show A", "Show B", "Show C")]
    assert pm.detect_low_completion(dropped, now)["count"] == 3
    assert pm.detect_low_completion(dropped, now, asked) is None, "the same three dropped shows were asked five days ago"
    dropped.append({"title": "Show D", "series_title": "Show D", "media_type": "show", "viewed_at": recent,
                    "duration_ms": 1_000_000, "view_offset_ms": 100_000, "completed": False, "genres": ""})
    assert pm.detect_low_completion(dropped, now, asked)["count"] == 4, "a new drop re-opens the subject"

    marked = {"tracks": set(), "titles": set(), "series": {}, "subjects": {}}
    pm._mark_asked(marked, {"type": "new_genre", "genre": "Holiday", "count": 4})
    pm._mark_asked(marked, {"type": "low_completion", "dropped": [{"title": "Show D"}], "count": 1})
    pm._mark_asked(marked, {"type": "night_owl", "media_title": "Nine Blades", "title": "Nine Blades"})
    assert {"new_genre:holiday", "low_completion:show d", "night_owl:nine blades"} <= set(marked["subjects"])


def test_the_pick_prefers_types_that_have_been_quiet():
    now = datetime(2026, 9, 22, 12, 0)
    hits = [("new_genre", {"type": "new_genre"}), ("history_deep_dive", {"type": "history_deep_dive"})]
    last_fired = {"new_genre": now - timedelta(days=2)}          # deep dive never fired
    rng = random.Random(7)
    picks = [pm._pick_trigger(hits, last_fired, now, rng)["type"] for _ in range(300)]
    quiet, busy = picks.count("history_deep_dive"), picks.count("new_genre")
    assert quiet > busy * 3, (quiet, busy)
    followup = [("rewatch", {"type": "rewatch"}), ("recommendation_followup", {"type": "recommendation_followup"})]
    assert pm._pick_trigger(followup, {}, now, rng)["type"] == "recommendation_followup", "a watched recommendation always wins"
    assert pm._pick_trigger([], {}, now, rng) is None


def test_the_starter_prompt_forbids_inflating_a_routine_into_a_binge():
    from src.services import chat_starters as cs
    src = pathlib.Path(cs.__file__).read_text(encoding="utf-8")
    assert '"kind": "current_binge"' not in src and '"kind": "active_series"' in src
    assert "ONE sitting" in cs._PROMPT and "routine" in cs._PROMPT


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    sys.exit(1 if fails else 0)
