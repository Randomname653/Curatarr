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

import asyncio
import json
import pathlib
import sys
from datetime import datetime

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tests"))
from _harness import app_db  # noqa: E402  (first: temp stores, never the live data/)


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


_SEEDED = []


def _taste(**kw):
    """get_user_taste_context on real rows: user 1 has a movie AND a music
    profile, user 2 a different movie profile."""
    if not _SEEDED:
        from src.database.models import TasteVectorEntry, User
        conn = app_db()

        def prof(genres, n=12):
            return {"watch_count": n, "avg_completion": 0.9,
                    "genre_affinity": {g: 1.0 - i / 10 for i, g in enumerate(genres)}}
        with conn.get_db_session() as db:
            db.add_all([User(id=1, plex_user_id="1", plex_username="owner", is_admin=True),
                        User(id=2, plex_user_id="2", plex_username="member")])
            db.flush()
            db.add(TasteVectorEntry(
                user_id=1, watch_count=40, computed_at=datetime(2026, 10, 1),
                genre_affinity=json.dumps({"movie": prof(["Exploitation", "Noir"]),
                                           "music": prof(["German Hip-Hop"])}),
                top_titles=json.dumps({"movie": ["Supervixens"], "music": ["K.I.Z", "Alligatoah"]}),
                summary_text="[MOVIE] Loves sleaze. [MUSIC] Raps along."))
            db.add(TasteVectorEntry(
                user_id=2, watch_count=30, computed_at=datetime(2026, 10, 1),
                genre_affinity=json.dumps({"movie": prof(["Romcom"])}),
                top_titles=json.dumps({"movie": ["Notting Hill"]})))
        _SEEDED.append(1)
    from src.services.plex_sync import get_user_taste_context
    return asyncio.run(get_user_taste_context(1, **kw))


_REPLY = "but they have gigantic titties"


def test_a_discussion_category_pins_the_scope():
    ctx = _taste(query=_REPLY, category="movie")
    assert "Supervixens" in ctx and "Loves sleaze" in ctx, ctx
    assert "K.I.Z" not in ctx and "Alligatoah" not in ctx and "Raps along" not in ctx, \
        "a movie discussion must not carry the music profile (the confabulated motive)"


def test_free_chat_keeps_the_cross_category_profile():
    ctx = _taste(query=_REPLY)              # no category: free chat
    assert "Supervixens" in ctx and "K.I.Z" in ctx, ctx


def test_an_unknown_category_falls_back_to_the_guess_instead_of_nothing():
    ctx = _taste(query=_REPLY, category="podcast")
    assert "Supervixens" in ctx and "K.I.Z" in ctx, ctx


def test_the_profile_is_the_callers_own():
    ctx = _taste(query=_REPLY, category="movie")
    assert "Notting Hill" not in ctx and "Romcom" not in ctx, "another member's taste leaked in"


def test_the_discussion_passes_its_known_category():
    """discuss_domain is set by the discussion context builder and is None
    in free chat — so discussions are scoped and free chat is untouched,
    in one argument."""
    src = _read("routers/chat.py")
    assert "category=discuss_domain)" in src
    # and the judge's own evidence stays category-scoped, as it always was
    assert "OWNER taste (category-scoped)" in _read("services/pillars.py")
