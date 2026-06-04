"""Unit tests for series-progress awareness (pure, in-memory layer).

These cover the watch-side aggregation, the conservative ``finished`` derivation,
the prompt phrase, and the re-engage (don't-repeat-the-same-question) decision —
none of which touch the DB or the enrichment cache, so no mocking is needed.
"""

from datetime import datetime

from src.services.series_progress import (
    compute_watch_progress,
    get_series_progress,
    should_reengage_series,
    progress_milestone,
    normalize_title,
)


def _ep(series, season, episode, *, completed=True, viewed=None,
        dur=1_400_000, off=None, media_type="anime"):
    return {
        "title": f"{series} S{season}E{episode}",
        "series_title": series,
        "media_type": media_type,
        "season": season,
        "episode": episode,
        "completed": completed,
        "viewed_at": viewed or datetime(2026, 1, episode if episode else 1, 20, 0),
        "duration_ms": dur,
        "view_offset_ms": off if off is not None else dur,
    }


# ── compute_watch_progress ──────────────────────────────────────────────────

def test_single_episode_then_stopped():
    """The Nukitashi case: one episode watched, never went further."""
    entries = [_ep("Nukitashi the Animation", 1, 1)]
    p = compute_watch_progress(entries, "Nukitashi the Animation")
    assert p is not None
    assert p["distinct_episodes"] == 1
    assert p["furthest_season"] == 1
    assert p["furthest_episode"] == 1
    assert p["total_plays"] == 1


def test_furthest_point_is_max_season_episode():
    entries = [
        _ep("Frieren", 1, 1),
        _ep("Frieren", 1, 12),
        _ep("Frieren", 1, 5),
    ]
    p = compute_watch_progress(entries, "Frieren")
    assert p["furthest_season"] == 1
    assert p["furthest_episode"] == 12
    assert p["distinct_episodes"] == 3


def test_distinct_episodes_dedups_replays():
    entries = [_ep("Frieren", 1, 3), _ep("Frieren", 1, 3), _ep("Frieren", 1, 4)]
    p = compute_watch_progress(entries, "Frieren")
    assert p["distinct_episodes"] == 2      # two distinct, not three plays
    assert p["total_plays"] == 3


def test_other_series_ignored():
    entries = [_ep("Frieren", 1, 1), _ep("Bleach", 1, 1)]
    p = compute_watch_progress(entries, "Frieren")
    assert p["distinct_episodes"] == 1


def test_movies_and_music_excluded():
    entries = [
        {"title": "A Movie", "series_title": None, "media_type": "movie",
         "season": None, "episode": None, "viewed_at": datetime(2026, 1, 1)},
    ]
    assert compute_watch_progress(entries, "A Movie") is None


def test_furthest_completed_false_when_bailed_midway():
    entries = [_ep("Apothecary", 1, 1, completed=False, dur=1_400_000, off=100_000)]
    p = compute_watch_progress(entries, "Apothecary")
    assert p["furthest_completed"] is False


# ── get_series_progress: conservative `finished` + phrase ───────────────────

def test_finished_true_when_total_known_and_fully_watched():
    entries = [_ep("Frieren", 1, n) for n in range(1, 29)]   # all 28
    p = get_series_progress(entries, "Frieren", total_episodes=28)
    assert p["finished"] is True
    assert "finished" in p["phrase"].lower()


def test_not_finished_when_more_episodes_remain():
    """The Apothecary Diaries case: finished S1 (24 eps) but the series has 48."""
    entries = [_ep("Apothecary Diaries", 1, n) for n in range(1, 25)]
    p = get_series_progress(entries, "Apothecary Diaries", total_episodes=48)
    assert p["finished"] is False
    assert "not finished" in p["phrase"].lower()
    assert "24 of 48" in p["phrase"]


def test_finished_is_none_when_total_unknown():
    """No enrichment total -> never CLAIM an ending; phrase warns the LLM."""
    entries = [_ep("Some Show", 1, n) for n in range(1, 13)]
    p = get_series_progress(entries, "Some Show", total_episodes=None)
    assert p["finished"] is None
    assert "unknown" in p["phrase"].lower()


def test_single_episode_phrase_says_stopped():
    entries = [_ep("Nukitashi", 1, 1)]
    p = get_series_progress(entries, "Nukitashi", total_episodes=12)
    assert p["finished"] is False
    assert "stopped" in p["phrase"].lower()


def test_finished_requires_finale_completed():
    """All episodes seen by number, but the last was bailed mid-way -> not finished."""
    entries = [_ep("Show", 1, n) for n in range(1, 12)]
    entries.append(_ep("Show", 1, 12, completed=False, dur=1_400_000, off=50_000))
    p = get_series_progress(entries, "Show", total_episodes=12)
    assert p["finished"] is False


# ── should_reengage_series: don't repeat the same question ──────────────────

def test_reengage_when_never_asked():
    cur = {"furthest_season": 1, "distinct_episodes": 12, "finished": False}
    assert should_reengage_series(cur, None) is True


def test_no_reengage_same_position():
    """Asked at S1E12; still at S1E12 -> stay quiet (already answered)."""
    cur = {"furthest_season": 1, "distinct_episodes": 12, "finished": False}
    last = {"furthest_season": 1, "distinct_episodes": 12, "finished": False}
    assert should_reengage_series(cur, last) is False


def test_reengage_on_new_season():
    """The Frieren S1 -> S2E3 case the user described."""
    cur = {"furthest_season": 2, "distinct_episodes": 15, "finished": False}
    last = {"furthest_season": 1, "distinct_episodes": 12, "finished": False}
    assert should_reengage_series(cur, last) is True


def test_reengage_when_newly_finished():
    cur = {"furthest_season": 1, "distinct_episodes": 28, "finished": True}
    last = {"furthest_season": 1, "distinct_episodes": 20, "finished": False}
    assert should_reengage_series(cur, last) is True


def test_reengage_on_big_episode_jump_without_season_tags():
    cur = {"furthest_season": None, "distinct_episodes": 20, "finished": None}
    last = {"furthest_season": None, "distinct_episodes": 12, "finished": None}
    assert should_reengage_series(cur, last) is True


def test_no_reengage_on_tiny_progress():
    cur = {"furthest_season": 1, "distinct_episodes": 14, "finished": False}
    last = {"furthest_season": 1, "distinct_episodes": 12, "finished": False}
    assert should_reengage_series(cur, last) is False


def test_milestone_roundtrip_shape():
    entries = [_ep("Frieren", 1, n) for n in range(1, 13)]
    p = get_series_progress(entries, "Frieren", total_episodes=28)
    m = progress_milestone(p)
    assert set(m.keys()) == {
        "furthest_season", "furthest_episode", "distinct_episodes", "finished",
    }


# ── normalize_title ─────────────────────────────────────────────────────────

def test_normalize_title_collapses_case_and_space():
    assert normalize_title("  The   Apothecary  Diaries ") == "the apothecary diaries"
    assert normalize_title("FRIEREN") == normalize_title("frieren")


# ── recency hint in the phrase ──────────────────────────────────────────────

def test_phrase_includes_recency_when_now_given():
    entries = [_ep("Show", 1, n, viewed=datetime(2026, 1, n, 20, 0)) for n in range(1, 13)]
    p = get_series_progress(entries, "Show", total_episodes=None, now=datetime(2026, 6, 1))
    assert "last watched" in p["phrase"]


def test_phrase_no_recency_without_now():
    entries = [_ep("Show", 1, n) for n in range(1, 13)]
    p = get_series_progress(entries, "Show", total_episodes=None)
    assert "last watched" not in p["phrase"]


# ── Sonarr-backed season/episode framing ────────────────────────────────────

def _sonarr(total_seasons, total_episodes, per_season, status="continuing"):
    return {"total_seasons": total_seasons, "total_episodes": total_episodes,
            "per_season": per_season, "status": status}


def test_per_season_watched_counts():
    base = compute_watch_progress([_ep("X", 1, 1), _ep("X", 1, 2), _ep("X", 2, 1)], "X")
    assert base["per_season_watched"] == {1: 2, 2: 1}


def test_multiseason_finished_s1_into_s2():
    # Frieren: S1=28 (all seen), S2=10 (3 seen), ongoing
    entries = ([_ep("Frieren", 1, n) for n in range(1, 29)]
               + [_ep("Frieren", 2, n) for n in range(1, 4)])
    p = get_series_progress(entries, "Frieren", sonarr=_sonarr(2, 38, {1: 28, 2: 10}))
    assert p["finished"] is False
    assert p["seasons_completed"] == [1]
    assert "finished season 1" in p["phrase"]
    assert "3 of 10 episodes into season 2 of 2" in p["phrase"]


def test_multiseason_finished_whole_when_ended_and_caught_up():
    entries = ([_ep("Done", 1, n) for n in range(1, 13)]
               + [_ep("Done", 2, n) for n in range(1, 13)])
    p = get_series_progress(entries, "Done", sonarr=_sonarr(2, 24, {1: 12, 2: 12}, status="ended"))
    assert p["finished"] is True
    assert "finished the ENTIRE series" in p["phrase"]


def test_multiseason_caught_up_but_ongoing_is_not_finished():
    entries = ([_ep("Ongoing", 1, n) for n in range(1, 13)]
               + [_ep("Ongoing", 2, n) for n in range(1, 13)])
    p = get_series_progress(entries, "Ongoing", sonarr=_sonarr(2, 24, {1: 12, 2: 12}, status="continuing"))
    assert p["finished"] is False
    assert p["caught_up"] is True
    assert "caught up" in p["phrase"].lower()


def test_single_season_with_sonarr_uses_simple_total():
    # Death Note: 1 season, 37 aired, watched 25 -> not finished
    entries = [_ep("Death Note", 1, n) for n in range(1, 26)]
    p = get_series_progress(entries, "Death Note", sonarr=_sonarr(1, 37, {1: 37}, status="ended"))
    assert p["finished"] is False
    assert p["total_seasons"] == 1
    assert "25 of 37" in p["phrase"]


def test_nukitashi_single_episode_with_sonarr_total():
    p = get_series_progress([_ep("Nukitashi", 1, 1)], "Nukitashi",
                            sonarr=_sonarr(1, 11, {1: 11}, status="ended"))
    assert p["finished"] is False
    assert "stopped" in p["phrase"].lower()
