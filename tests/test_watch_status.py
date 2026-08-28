from unittest.mock import patch

from src.services.watch_status import _artist_variants, music_listening_stats

def test_artist_variants():
    # Empty inputs
    assert _artist_variants(None) == [""]
    assert _artist_variants("") == [""]
    assert _artist_variants("   ") == [""]

    # Normal base case
    assert sorted(_artist_variants("Artist")) == ["artist"]

    # Dash folding test for every dash in _DASHES
    # The dashes are: "‐‑‒–—−"
    dashes = "‐‑‒–—−"
    for dash in dashes:
        # Each dash variant should produce a set containing the lowercase dash variant AND the folded dash variant
        name = f"Jay{dash}Z"
        variants = _artist_variants(name)
        assert len(variants) == 2, f"Expected 2 variants for dash '{dash}', got {variants}"
        assert "jay-z" in variants, f"Expected 'jay-z' in variants for dash '{dash}', got {variants}"
        assert name.lower() in variants, f"Expected '{name.lower()}' in variants for dash '{dash}', got {variants}"

def test_music_listening_stats_early_exit():
    # Test early exit conditions (returns None before DB access)

    # Missing user_id
    assert music_listening_stats(0, "Artist") is None
    assert music_listening_stats(None, "Artist") is None

    # Missing both artist and artist_mbid
    assert music_listening_stats(1, "") is None
    assert music_listening_stats(1, None) is None
    assert music_listening_stats(1, "", "") is None

    # With user_id and one of artist/mbid, it proceeds past early exit
    # (Since we haven't mocked DB here, if we pass valid args it will try to access DB,
    # but for early exit we just test the None returns).

def test_music_listening_stats_exception():
    # Test that DB exceptions are caught and return None
    import contextlib

    @contextlib.contextmanager
    def mock_get_db_session():
        raise Exception("Mock DB connection failure")
        yield # just to make it a generator for context manager

    with patch("src.database.connection.get_db_session", new=mock_get_db_session):
        result = music_listening_stats(1, "Artist")
        assert result is None
