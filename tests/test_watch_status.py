"""Evidence lines and lookups built from the owner's real listening record.

    python tests/run_all.py

format_listening_line assembles its sentence from optional fragments, so
every absent field is its own failure mode; _artist_variants exists because
artist names arrive with typographic dashes that must fold to one form.
"""
from datetime import datetime
from unittest.mock import patch

from src.services.watch_status import (
    _DASHES, _artist_variants, format_listening_line, music_listening_stats,
)

def test_format_listening_line_none():
    assert format_listening_line(None) == "NO recorded plays in the owner's listening history."


def test_format_listening_line_empty():
    assert format_listening_line({}) == "NO recorded plays in the owner's listening history."


def test_format_listening_line_full():
    stats = {
        "plays": 42,
        "tracks": 10,
        "last": datetime(2023, 10, 15),
        "top": [("Track A", 20), ("Track B", 15), ("Track C", 7)]
    }
    expected = "42 plays across 10 distinct tracks, last Oct 2023; top tracks: Track A (20 plays), Track B (15 plays), Track C (7 plays)"
    assert format_listening_line(stats) == expected


def test_format_listening_line_no_last():
    stats = {
        "plays": 12,
        "tracks": 4,
        "last": None,
        "top": [("Track A", 10), ("Track B", 2)]
    }
    expected = "12 plays across 4 distinct tracks; top tracks: Track A (10 plays), Track B (2 plays)"
    assert format_listening_line(stats) == expected


def test_format_listening_line_no_top():
    stats = {
        "plays": 5,
        "tracks": 5,
        "last": datetime(2023, 11, 20),
        "top": []
    }
    expected = "5 plays across 5 distinct tracks, last Nov 2023; top tracks: "
    assert format_listening_line(stats) == expected


def test_format_listening_line_many_top():
    stats = {
        "plays": 100,
        "tracks": 25,
        "last": datetime(2023, 12, 1),
        "top": [("T1", 50), ("T2", 20), ("T3", 10), ("T4", 5), ("T5", 1)]
    }
    expected = "100 plays across 25 distinct tracks, last Dec 2023; top tracks: T1 (50 plays), T2 (20 plays), T3 (10 plays)"
    assert format_listening_line(stats) == expected


def test_artist_variants():
    assert _artist_variants(None) == [""]
    assert _artist_variants("") == [""]
    assert _artist_variants("   ") == [""]
    assert sorted(_artist_variants("Artist")) == ["artist"]

    # Every dash the source folds — read from _DASHES itself, so adding a
    # dash to the source can never leave this test silently behind.
    for dash in (chr(c) for c in _DASHES):
        name = f"Jay{dash}Z"
        variants = _artist_variants(name)
        assert len(variants) == 2, f"expected 2 variants for {dash!r}, got {variants}"
        assert "jay-z" in variants
        assert name.lower() in variants


class _DBTouched(BaseException):
    """Deliberately NOT an Exception: music_listening_stats catches every
    Exception, so a plain AssertionError raised here would be swallowed and
    the guard would still look intact."""


def test_music_listening_stats_early_exit():
    # The guard must return None BEFORE any DB access. Asserting only the
    # return value is not enough: drop the artist/mbid half of the guard and
    # the call falls through to a REAL session, whose failure the function's
    # own broad except turns back into None — green test, live DB hit.
    def _no_db(*a, **k):
        raise _DBTouched("DB touched on an early-exit path")

    with patch("src.database.connection.get_db_session", new=_no_db):
        assert music_listening_stats(0, "Artist") is None
        assert music_listening_stats(None, "Artist") is None
        assert music_listening_stats(1, "") is None
        assert music_listening_stats(1, None) is None
        assert music_listening_stats(1, "", "") is None


def test_music_listening_stats_exception():
    # A DB failure is silence, not a crash — the caller renders "no plays".
    import contextlib

    @contextlib.contextmanager
    def _boom():
        raise Exception("mock DB connection failure")
        yield

    with patch("src.database.connection.get_db_session", new=_boom):
        assert music_listening_stats(1, "Artist") is None


# ── watched_lookup / watch_tag: music is plays of tracks, never episodes ────────

class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Q:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self.rows


class _Session:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def query(self, *a, **k):
        return _Q(self.rows)


def test_watched_lookup_counts_music_as_tracks_not_episodes():
    """Plex/Spotify music rows carry season=1 / episode=<track index>. An artist
    matched by series_title read '15 episodes played (73 plays)' in chat
    (Otis Redding, 2026-09-22)."""
    from src.services.watch_status import watched_lookup, watch_tag
    when = datetime(2026, 9, 21, 20, 0)
    rows = [
        _Row(title="It's Growing", series_title="Otis Redding", completed=True, viewed_at=when, season=1, episode=2, media_type="music"),
        _Row(title="Louie Louie", series_title="Otis Redding", completed=True, viewed_at=when, season=1, episode=8, media_type="music"),
        _Row(title="Louie Louie", series_title="Otis Redding", completed=True, viewed_at=when, season=1, episode=8, media_type="music"),
        _Row(title="Satisfaction", series_title="Otis Redding", completed=True, viewed_at=when, season=1, episode=6, media_type="music"),
        _Row(title="Frieren E1", series_title="Frieren", completed=True, viewed_at=when, season=1, episode=1, media_type="anime"),
        _Row(title="Frieren E2", series_title="Frieren", completed=True, viewed_at=when, season=1, episode=2, media_type="anime"),
        _Row(title="Frieren E2", series_title="Frieren", completed=True, viewed_at=when, season=1, episode=2, media_type="anime"),
    ]
    with patch("src.database.connection.get_db_session", new=lambda: _Session(rows)):
        got = watched_lookup(1, ["Otis Redding", "Frieren"])
    otis, frieren = got["Otis Redding"], got["Frieren"]
    assert otis["media_type"] == "music" and otis["count"] == 4 and otis["tracks"] == 3 and otis["episodes"] == 0
    assert frieren["media_type"] == "video" and frieren["count"] == 3 and frieren["episodes"] == 2 and frieren["tracks"] == 0
    assert watch_tag(otis) == "4 plays across 3 distinct tracks, last Sep 2026"
    assert watch_tag(frieren) == "2 episodes played (3 plays), last Sep 2026"
    assert watch_tag(None) == "NOT watched"
