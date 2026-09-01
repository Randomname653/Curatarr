"""A discussion about a movie argues from the MOVIE taste profile.

Provoked by the Supervixens discussion: the chat's taste injection guessed
relevant categories by keyword-matching the user's reply, and a reply like
"but they have gigantic titties" names no category — the fallback is ALL
categories, so a movie deletion discussion carried the user's music
profile. When the curator then needed an explanation for an unrelated
language switch, that profile supplied the confabulated motive ("your
preference for K.I.Z and Alligatoah").

The judge's evidence has been category-scoped from the start; these tests
pin the discussion to the same rule. Free chat deliberately keeps the
cross-category behaviour — one thread may hop from music to movies.
"""

import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


def _read(rel: str) -> str:
    return (_ROOT / "src" / rel).read_text(encoding="utf-8")


def test_the_keyword_detector_falls_back_to_everything():
    """The behaviour that made the pin necessary — documented, not fixed:
    free chat WANTS this fallback."""
    from src.services.plex_sync import _detect_relevant_categories
    type_data = {"movie": {}, "music": {}, "anime": {}, "show": {}}
    cats = _detect_relevant_categories("but they have gigantic titties", type_data)
    assert set(cats) == set(type_data), (
        "category-less replies resolve to ALL categories — which is exactly "
        "why a discussion must pass its known category instead")


def test_an_explicit_category_pins_the_scope():
    src = _read("services/plex_sync.py")
    assert "category: str = None" in src
    assert "if category and category in type_data:" in src
    assert "relevant_cats = [category]" in src


def test_the_discussion_passes_its_known_category():
    """discuss_domain is set by the discussion context builder and is None
    in free chat — so discussions are scoped and free chat is untouched,
    in one argument."""
    src = _read("routers/chat.py")
    assert "category=discuss_domain)" in src
    # and the judge's own evidence stays category-scoped, as it always was
    assert "OWNER taste (category-scoped)" in _read("services/pillars.py")
