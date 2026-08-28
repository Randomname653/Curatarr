from src.services.anime_mapping import AnimeMapping

def test_lookup_tvdb_missing():
    mapping = AnimeMapping()
    res = mapping.lookup_tvdb(99999)
    assert res == {
        "tvdb_id": 99999,
        "anidb_id": None,
        "anilist_id": None,
    }

def test_lookup_tvdb_missing_anilist():
    mapping = AnimeMapping()
    mapping.tvdb_to_anidb[123] = 456
    # No anidb_to_anilist mapping for 456
    res = mapping.lookup_tvdb(123)
    assert res == {
        "tvdb_id": 123,
        "anidb_id": 456,
        "anilist_id": None,
    }

def test_lookup_tvdb_happy_path():
    mapping = AnimeMapping()
    mapping.tvdb_to_anidb[123] = 456
    mapping.anidb_to_anilist[456] = 789
    res = mapping.lookup_tvdb(123)
    assert res == {
        "tvdb_id": 123,
        "anidb_id": 456,
        "anilist_id": 789,
    }
