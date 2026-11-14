"""The curator's "genre absence" question asks about a loved genre only when
its own kind of media went on without it (proactive_messages).

2026-11-14: "Electronic has gone silent for over thirty days" - the owner's
top genre from 380k music plays, and no music play had arrived for six weeks
(the source was quiet, the films and shows went on). A silent source is not
a changed taste.

    python tests/test_genre_absence.py
"""
from _harness import check  # noqa: I001 - first: temp stores

from datetime import datetime, timedelta

from src.services.proactive_messages import detect_genre_absence

now = datetime(2026, 11, 14, 22, 0)


def play(kind, genres, days_ago):
    return {"media_type": kind, "genres": genres, "viewed_at": now - timedelta(days=days_ago)}


# Six weeks ago: lots of electronic music. Since then: films only.
music_then = [play("music", "Electronic", 40 + i % 5) for i in range(50)]
films_now = [play("movie", "Drama", i) for i in range(1, 10)]
got = detect_genre_absence(music_then + films_now, now)
check("no music in the window at all: its genres are not 'absent' (the source is quiet)",
      got is None or got["genre"] != "Electronic", got)

# Music went on - but without electronic: that IS a changed taste.
music_now = [play("music", "Metal", i) for i in range(1, 8)]
got = detect_genre_absence(music_then + films_now + music_now, now)
check("music played, electronic not among it: asked about", got and got["genre"] == "Electronic", got)

# A loved film genre gone while films went on: still asked about.
drama_then = [play("movie", "Horror", 45) for _ in range(30)]
got = detect_genre_absence(drama_then + films_now, now)
check("films played, horror not among them: asked about", got and got["genre"] == "Horror", got)

check("no history: nothing", detect_genre_absence([], now) is None)

check.done()
