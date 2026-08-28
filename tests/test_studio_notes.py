"""Test cache miss and error handling in studio_notes."""
from unittest.mock import Mock

from src.services.studio_notes import get_studio_note_cached, get_director_note_cached

def test_studio_note_cached_hit():
    """Cache hit with a dict response returns the note."""
    cache = Mock()
    cache.get_cache.return_value = {"response": {"note": "A great studio."}}

    result = get_studio_note_cached("MADHOUSE", cache)
    assert result == "A great studio."

def test_studio_note_cached_miss():
    """Cache miss returns None."""
    cache = Mock()
    cache.get_cache.return_value = None

    result = get_studio_note_cached("MADHOUSE", cache)
    assert result is None

def test_studio_note_cached_not_dict():
    """Cache hit where response is not a dict returns None."""
    cache = Mock()
    # If the response is not a dict, it should return None without error
    cache.get_cache.return_value = {"response": "just a string"}

    result = get_studio_note_cached("MADHOUSE", cache)
    assert result is None

class BrokenCache:
    def get_cache(self, key):
        raise ValueError("Simulated cache failure")

def test_studio_note_cached_exception():
    """Cache exception is swallowed and returns None."""
    cache = BrokenCache()
    result = get_studio_note_cached("MADHOUSE", cache)
    assert result is None

def test_director_note_cached_hit():
    """Cache hit with a dict response returns the note."""
    cache = Mock()
    cache.get_cache.return_value = {"response": {"note": "A great director."}}

    result = get_director_note_cached("Makoto Shinkai", cache)
    assert result == "A great director."

def test_director_note_cached_miss():
    """Cache miss returns None."""
    cache = Mock()
    cache.get_cache.return_value = None

    result = get_director_note_cached("Makoto Shinkai", cache)
    assert result is None

def test_director_note_cached_not_dict():
    """Cache hit where response is not a dict returns None."""
    cache = Mock()
    cache.get_cache.return_value = {"response": "just a string"}

    result = get_director_note_cached("Makoto Shinkai", cache)
    assert result is None

def test_director_note_cached_exception():
    """Cache exception is swallowed and returns None."""
    cache = BrokenCache()
    result = get_director_note_cached("Makoto Shinkai", cache)
    assert result is None
