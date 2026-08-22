"""Tests for the series-aware rewatch fix + subject de-duplication / rotation
in proactive message generation.

Same mock preamble as test_proactive_messages.py — the DB / config / http
layers are stubbed so the pure in-memory detectors can be exercised directly.
The detectors covered here (rewatch, history_deep_dive, the suppression helpers)
take their data from the in-memory ``entries`` list + ``asked_subjects`` and
never touch the DB, so the stubs are enough.
"""

import sys
from unittest.mock import MagicMock

sys.modules['httpx'] = MagicMock()
sys.modules['sqlalchemy'] = MagicMock()
sys.modules['sqlalchemy.orm'] = MagicMock()
sys.modules['src.database.connection'] = MagicMock()
sys.modules['src.database.models'] = MagicMock()
sys.modules['src.config'] = MagicMock()

from datetime import datetime

from src.services.proactive_messages import (
    detect_rewatch,
    detect_history_deep_dive,
    _series_recently_covered,
    _titled_recently_covered,
    _mark_asked,
    _series_key_of,
    _resolve_sonarr_stats,
    _sonarr_series_stats,
)


def _ep(series, season, episode, *, viewed=None, completed=True,
        media_type="anime", dur=1_400_000, off=None):
    return {
        "title": f"{series} S{season}E{episode}",
        "series_title": series,
        "media_type": media_type,
        "season": season,
        "episode": episode,
        "completed": completed,
        "genres": "Action",
        "viewed_at": viewed or datetime(2026, 1, max(episode, 1), 20, 0),
        "duration_ms": dur,
        "view_offset_ms": off if off is not None else dur,
    }


def _movie(title, viewed):
    return {
        "title": title, "series_title": None, "media_type": "movie",
        "season": None, "episode": None, "completed": True, "genres": "Sci-Fi",
        "viewed_at": viewed, "duration_ms": 7_000_000, "view_offset_ms": 7_000_000,
    }


def _empty_asked():
    return {"tracks": set(), "titles": set(), "series": {}}


# ── detect_rewatch: the series-vs-episode-count fix ─────────────────────────

def test_series_distinct_episodes_is_not_a_rewatch():
    """Three DISTINCT episodes watched once each must NOT read as 'rewatched 3x'."""
    entries = [_ep("New Show", 1, 1), _ep("New Show", 1, 2), _ep("New Show", 1, 3)]
    assert detect_rewatch(entries) is None


def test_series_genuine_replays_fire_as_series_rewatch():
    """The same episode re-viewed enough times IS a rewatch — flagged is_series."""
    entries = [_ep("Comfort Anime", 1, 1, viewed=datetime(2026, 1, d)) for d in range(1, 6)]
    r = detect_rewatch(entries)
    assert r is not None
    assert r["type"] == "rewatch"
    assert r["is_series"] is True
    assert r["title"] == "Comfort Anime"
    assert r["replays"] == 4          # 5 plays - 1 distinct episode


def test_movie_three_plays_is_a_rewatch():
    entries = [_movie("The Matrix", datetime(2026, 1, d)) for d in range(1, 4)]
    r = detect_rewatch(entries)
    assert r is not None
    assert r["is_series"] is False
    assert r["title"] == "The Matrix"
    assert r["count"] == 3


def test_rewatch_skips_already_asked_movie():
    entries = [_movie("The Matrix", datetime(2026, 1, d)) for d in range(1, 4)]
    asked = _empty_asked()
    asked["titles"].add("the matrix")
    assert detect_rewatch(entries, asked) is None


def test_rewatch_skips_series_not_advanced():
    entries = [_ep("Comfort Anime", 1, 1, viewed=datetime(2026, 1, d)) for d in range(1, 6)]
    asked = _empty_asked()
    asked["series"]["comfort anime"] = {
        "furthest_season": 1, "distinct_episodes": 1, "finished": False,
    }
    assert detect_rewatch(entries, asked) is None


def test_rewatch_refires_series_after_new_season():
    entries = [_ep("Comfort Anime", 1, 1, viewed=datetime(2026, 1, d)) for d in range(1, 6)]
    entries.append(_ep("Comfort Anime", 2, 1, viewed=datetime(2026, 2, 1)))
    asked = _empty_asked()
    asked["series"]["comfort anime"] = {
        "furthest_season": 1, "distinct_episodes": 1, "finished": False,
    }
    r = detect_rewatch(entries, asked)
    assert r is not None and r["title"] == "Comfort Anime"   # new season -> ask again


# ── detect_history_deep_dive: don't dredge up the same memory ───────────────

def test_deep_dive_excludes_covered_series():
    now = datetime(2026, 6, 1, 12, 0)
    entries = [_ep("Old Anime", 1, 1, viewed=datetime(2025, 1, 1))]
    asked = _empty_asked()
    asked["series"]["old anime"] = {
        "furthest_season": 1, "distinct_episodes": 1, "finished": False,
    }
    assert detect_history_deep_dive(entries, now, asked) is None


def test_deep_dive_returns_fresh_series():
    now = datetime(2026, 6, 1, 12, 0)
    entries = [_ep("Old Anime", 1, 1, viewed=datetime(2025, 1, 1))]
    r = detect_history_deep_dive(entries, now, _empty_asked())
    assert r is not None and r["title"] == "Old Anime"


# ── suppression helpers ─────────────────────────────────────────────────────

def test_series_recently_covered_true_when_same_position():
    entries = [_ep("Frieren", 1, n, viewed=datetime(2026, 1, max(n, 1))) for n in range(1, 13)]
    asked = _empty_asked()
    asked["series"]["frieren"] = {
        "furthest_season": 1, "distinct_episodes": 12, "finished": False,
    }
    assert _series_recently_covered("Frieren", entries, asked) is True


def test_series_recently_covered_false_when_advanced():
    entries = [_ep("Frieren", 2, n, viewed=datetime(2026, 2, max(n, 1))) for n in range(1, 4)]
    asked = _empty_asked()
    asked["series"]["frieren"] = {
        "furthest_season": 1, "distinct_episodes": 12, "finished": False,
    }
    assert _series_recently_covered("Frieren", entries, asked) is False


def test_series_recently_covered_false_when_never_asked():
    entries = [_ep("Frieren", 1, 1)]
    assert _series_recently_covered("Frieren", entries, _empty_asked()) is False


def test_series_legacy_no_milestone_allows_one_more():
    """Asked before but milestone unknown (legacy) -> allow one more to capture it."""
    entries = [_ep("Frieren", 1, 1)]
    asked = _empty_asked()
    asked["series"]["frieren"] = None
    assert _series_recently_covered("Frieren", entries, asked) is False


def test_titled_recently_covered():
    asked = _empty_asked()
    asked["titles"].add("the matrix")
    assert _titled_recently_covered("The Matrix", asked) is True
    assert _titled_recently_covered("Inception", asked) is False


# ── _mark_asked / _series_key_of ────────────────────────────────────────────

def test_mark_asked_track():
    asked = _empty_asked()
    _mark_asked(asked, {"type": "track_obsession", "track": "Influencer"})
    assert "influencer" in asked["tracks"]


def test_mark_asked_series_via_milestone():
    asked = _empty_asked()
    ms = {"furthest_season": 1, "distinct_episodes": 12, "finished": False}
    _mark_asked(asked, {"type": "series_completion", "series": "Frieren", "milestone": ms})
    assert asked["series"]["frieren"] == ms


def test_mark_asked_movie_rewatch_goes_to_titles():
    asked = _empty_asked()
    _mark_asked(asked, {"type": "rewatch", "title": "The Matrix", "is_series": False})
    assert "the matrix" in asked["titles"]
    assert "the matrix" not in asked["series"]


def test_series_key_of():
    assert _series_key_of({"type": "binge_episode", "series": "Bleach"}) == "Bleach"
    assert _series_key_of({"type": "rewatch", "title": "X", "is_series": True}) == "X"
    assert _series_key_of({"type": "rewatch", "title": "X", "is_series": False}) is None
    assert _series_key_of({"type": "track_obsession", "track": "Y"}) is None


# ── Sonarr stats reduction + matching ───────────────────────────────────────

def test_sonarr_series_stats_excludes_specials():
    s = {
        "title": "X", "tvdbId": 1, "seriesType": "anime", "status": "continuing",
        "seasons": [
            {"seasonNumber": 0, "statistics": {"episodeCount": 5}},   # specials
            {"seasonNumber": 1, "statistics": {"episodeCount": 12}},
            {"seasonNumber": 2, "statistics": {"episodeCount": 3}},
            {"seasonNumber": 3, "statistics": {"episodeCount": 0}},   # announced, unaired
        ],
    }
    st = _sonarr_series_stats(s)
    assert st["per_season"] == {1: 12, 2: 3, 3: 0}
    assert st["total_episodes"] == 15            # specials + unaired excluded from sum
    assert st["total_seasons"] == 2              # only seasons with aired episodes


def test_resolve_sonarr_title_first_with_disambiguation():
    anime = {"title": "Death Note", "series_type": "anime", "per_season": {1: 37},
             "total_episodes": 37, "total_seasons": 1, "status": "ended"}
    live = {"title": "Death Note", "series_type": "standard", "per_season": {1: 11},
            "total_episodes": 11, "total_seasons": 1, "status": "ended"}
    idx = {"by_tvdb": {}, "by_title": {"death note": [anime, live]}}
    assert _resolve_sonarr_stats(idx, None, "Death Note", "anime") is anime
    assert _resolve_sonarr_stats(idx, None, "Death Note", "show") is live


def test_resolve_sonarr_tvdb_fallback_only_when_no_title_match():
    stats = {"title": "Renamed JP", "series_type": "anime", "per_season": {1: 12},
             "total_episodes": 12, "total_seasons": 1, "status": "ended"}
    idx = {"by_tvdb": {424536: stats}, "by_title": {}}
    assert _resolve_sonarr_stats(idx, 424536, "English Name", "anime") is stats
    assert _resolve_sonarr_stats(idx, 999, "Unknown", "anime") is None
    assert _resolve_sonarr_stats(None, 424536, "X", "anime") is None


def test_load_asked_subjects_tolerates_malformed_json():
    from src.services.proactive_messages import _load_asked_subjects
    from unittest.mock import patch

    # Mock the DB query chain to return some rows, including malformed ones.
    class MockQuery:
        def __init__(self, rows):
            self.rows = rows
        def filter(self, *args, **kwargs): return self
        def order_by(self, *args, **kwargs): return self
        def limit(self, *args, **kwargs): return self
        def all(self): return self.rows

    class MockSession:
        def __init__(self, rows):
            self.rows = rows
        def __enter__(self): return self
        def __exit__(self, exc_type, exc_val, exc_tb): pass
        def query(self, *args, **kwargs): return MockQuery(self.rows)

    def mock_get_db_session():
        rows = [
            ("track_obsession", '{"track": "good song"}'),
            ("track_obsession", "not json"),            # ValueError / JSONDecodeError
            ("track_obsession", None),                  # Handled as {}
            ("track_obsession", '{"track": "other"}'),
            ("track_obsession", 123),                   # TypeError / not a string / not a dict later
        ]
        return MockSession(rows)

    with patch("src.services.proactive_messages.get_db_session", mock_get_db_session):
        result = _load_asked_subjects(1)

        assert "good song" in result["tracks"]
        assert "other" in result["tracks"]
        assert len(result["tracks"]) == 2
