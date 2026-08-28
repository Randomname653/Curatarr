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
