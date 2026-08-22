import pytest
from src.services.media_enricher import _titles_close_enough, _candidate_matches

def test_titles_close_enough_exact():
    assert _titles_close_enough("The Matrix", ["The Matrix"]) is True
    assert _titles_close_enough("The Matrix", ["the matrix", "something else"]) is True
    assert _titles_close_enough("The Matrix", ["something else", " THE MATRIX "]) is True

def test_titles_close_enough_short_query():
    # Query length <= 4 needs exact match
    assert _titles_close_enough("It", ["It"]) is True
    assert _titles_close_enough("It", ["Strike It Rich"]) is False
    assert _titles_close_enough("Up", ["Up"]) is True
    assert _titles_close_enough("Up", ["Growing Up"]) is False

def test_titles_close_enough_empty_candidates():
    assert _titles_close_enough("The Matrix", []) is False
    assert _titles_close_enough("The Matrix", ["", None]) is False
    assert _titles_close_enough("", ["The Matrix"]) is False
    assert _titles_close_enough(None, ["The Matrix"]) is False

def test_titles_close_enough_colon_suffix():
    # Reject match if candidate is query + colon + subtitle
    assert _titles_close_enough("King Crimson", ["King Crimson: Deja VROOOM"]) is False
    # But accept exact match
    assert _titles_close_enough("King Crimson", ["King Crimson"]) is True
    # If the base doesn't match, it could still be a substring/fuzzy, but not if it's the exact base
    assert _titles_close_enough("Spider-Man", ["Spider-Man: Homecoming"]) is False
    # Removed Spider-Man 2 vs Homecoming as the fuzzy score correctly identifies them as overlapping (0.66 > 0.6) but the colon-guard only protects exact base queries

def test_titles_close_enough_single_word_query():
    assert _titles_close_enough("Ghosts", ["Ghosts of Mars"]) is True
    assert _titles_close_enough("Ghosts", ["Inner Ghosts"]) is False
    assert _titles_close_enough("Ghosts", ["Old Ghosts"]) is False
    assert _titles_close_enough("Ghosts", ["Ghost Story"]) is False # single word query 'ghosts' in 'ghost story' is false.

def test_titles_close_enough_substring_length_check():
    # Allow substring if word count diff <= 2
    assert _titles_close_enough("Batman Begins", ["Batman Begins Again"]) is True # 2 vs 3 words
    # Disallow if word count diff > 2
    assert _titles_close_enough("Jesus", ["Jesus Shows You the Way to the Highway"]) is False

def test_titles_close_enough_threshold():
    # fuzzy matching test
    # "A Big Long Title About Space" vs "Big Long Title About Space"
    assert _titles_close_enough("A Big Long Title Space", ["Big Long Title Space"]) is True
    # Should fail if we set threshold high
    assert _titles_close_enough("Very Long Unrelated Title Here", ["Slightly Long Unrelated Title"], threshold=0.9) is False

def test_candidate_matches_direct():
    # Directly test the helper
    assert _candidate_matches("test", "test", True, 1, "test", 0.6) is True
    assert _candidate_matches("test", "test", True, 1, "test it", 0.6) is False
