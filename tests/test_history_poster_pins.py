"""Last played: a play without an id gets its poster through the owner's pin.

2026-11-09: a show called "Evil" sat in the chat landing's Last played with
another series' poster. Its play rows carry no tmdb_id, so the poster came
from a title search, and the search for a short title found the wrong show,
although the owner had pinned the right one in the Knowledge Base. The pin
now comes first:

- a pinned show answers with the pin's ids (tvdb and tmdb), also for an
  anime play (one Sonarr serves both);
- a movie pin never answers for a show of the same name;
- a play that carries its own id, and an unpinned one, are untouched.

    python tests/test_history_poster_pins.py
"""
from _harness import check  # noqa: I001 - first: temp stores

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy.orm import sessionmaker

from _checks import memory_engine
from src.database.models import Base, MediaMatchOverride, User, WatchHistoryEntry as W
import src.routers.history as history
import src.routers.recommendations as recs

engine = memory_engine()
Base.metadata.create_all(engine)
S = sessionmaker(bind=engine)


@contextmanager
def _session():
    db = S()
    try:
        yield db
        db.commit()          # like get_db_session: a clean exit commits
    finally:
        db.close()


history.get_db_session = _session

now = datetime(2026, 11, 8, 20, 0)
with _session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="o", is_admin=True))
    rows = [("Genesis 1", "Evil", "show", None), ("Pilot", "Night Shift Diaries", "anime", None),
            ("Glass Meridian", None, "movie", None), ("North Signal", None, "movie", 4711),
            ("Harbour Lights", None, "movie", None)]
    for i, (title, series, mt, tmdb) in enumerate(rows):
        db.add(W(user_id=1, plex_user_id="1", plex_item_id=f"k{i}", title=title, series_title=series,
                 media_type=mt, tmdb_id=tmdb, viewed_at=now - timedelta(hours=i), completed=True))
    db.add(MediaMatchOverride(service="sonarr", arr_id=5, category="show", title="Evil",
                              tvdb_id=356146, tmdb_id=86848))
    db.add(MediaMatchOverride(service="sonarr", arr_id=6, category="show", title="Night-Shift Diaries",
                              tvdb_id=111))
    db.add(MediaMatchOverride(service="radarr", arr_id=7, category="movie", title="Glass Meridian",
                              tmdb_id=222))
    db.add(MediaMatchOverride(service="radarr", arr_id=8, category="movie", title="North Signal",
                              tmdb_id=999))
    db.commit()

calls = {}


async def fake_fetch(title, category, tmdb_id=None, year=None, tvdb_id=None, mbid=None):
    calls[title] = (category, tmdb_id, tvdb_id)
    return f"https://img/{title}.jpg", None

recs._fetch_tmdb = fake_fetch
out = asyncio.run(history.recent_history(limit=8, offset=0, category=None, user=User(id=1)))

check("the pinned show answers with the pin's ids, not a title search",
      calls.get("Evil") == ("show", 86848, 356146), calls)
check("...a show pin answers for an anime play, by name without punctuation",
      calls.get("Night Shift Diaries") == ("anime", None, 111), calls)
check("...a movie pin answers for its movie", calls.get("Glass Meridian") == ("movie", 222, None), calls)
check("a play's own id stays its own, a pin does not override it",
      calls.get("North Signal") == ("movie", 4711, None), calls)
check("an unpinned play without an id still searches by title",
      calls.get("Harbour Lights") == ("movie", None, None), calls)
check("...and every entry carries the poster it got",
      all(e["poster_url"] for e in out["entries"]), out["entries"])

with _session() as db:
    db.add(MediaMatchOverride(service="radarr", arr_id=9, category="movie", title="Harbour Lights",
                              tmdb_id=333))
    db.commit()
check("a movie pin never answers for a show of the same name",
      history._pinned_ids([("Harbour Lights", "show")]) == {}
      and history._pinned_ids([("Harbour Lights", "movie")]) == {("Harbour Lights", "movie"): (333, None)})

# ── 2026-11-14: a show in Plex but not in Sonarr has no pin to take ──────────
# The real "Evil" was in Plex only: no arr item, so no pin, and its plays had
# no series id. Plex's own ids for the library item (the tech sync's
# media_tech_profiles, filed from the show's Guid) answer before the title
# search; an ambiguous name does not.
from src.database.models import MediaTechProfile as P  # noqa: E402

with _session() as db:
    for i, (title, series, mt) in enumerate([("Pilot", "Lantern Row", "show"),
                                              ("Part One", "Twin Mirrors", "show"),
                                              ("Ep 1", "Night Shift Diaries", "anime")]):
        db.add(W(user_id=1, plex_user_id="1", plex_item_id=f"n{i}", title=title, series_title=series,
                 media_type=mt, viewed_at=now + timedelta(hours=1 + i), completed=True))
    db.add(P(plex_rating_key="501", media_type="show", title="Lantern Row", tmdb_id=86848, tvdb_id=363955))
    db.add(P(plex_rating_key="502", media_type="show", title="Twin Mirrors", tmdb_id=1, tvdb_id=None))
    db.add(P(plex_rating_key="503", media_type="show", title="Twin Mirrors", tmdb_id=2, tvdb_id=None))
    db.add(P(plex_rating_key="504", media_type="anime", title="Night Shift Diaries", tmdb_id=777, tvdb_id=None))
    db.add(P(plex_rating_key="505", media_type="movie", title="Harbour Lights", tmdb_id=444, tvdb_id=None))
    db.commit()

calls.clear()
recs._fetch_tmdb = fake_fetch
asyncio.run(history.recent_history(limit=8, offset=0, category=None, user=User(id=1)))
check("a show without a pin takes Plex's own ids for the library item",
      calls.get("Lantern Row") == ("show", 86848, 363955), calls)
check("...two library items of one name with different ids leave it to the title search",
      calls.get("Twin Mirrors") == ("show", None, None), calls)
check("...the owner's pin still comes before Plex's ids",
      calls.get("Night Shift Diaries") == ("anime", None, 111), calls)
check("a movie's Plex ids never answer for a show of the same name",
      history._plex_ids([("Harbour Lights", "show")]) == {}
      and history._plex_ids([("Harbour Lights", "movie")]) == {("Harbour Lights", "movie"): (444, None)})

check.done()
