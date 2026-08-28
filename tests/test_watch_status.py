from datetime import datetime
from src.services.watch_status import format_listening_line

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
