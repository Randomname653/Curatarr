"""Rediscover and "a year ago" (services/rediscover_recs, services/on_this_day).

Rediscover:
- a film finished, or a series watched in earnest (six finished episodes),
  and nothing of it played in the last two years; a series dabbled in, a
  film never finished, or anything touched recently does not count;
- only owned titles, ranked by today's taste fit, each with its fact line
  in the member's language ("Zuletzt vor 3 Jahren · 2× gesehen").

A year ago:
- what was finished within three days of this date last year, a series
  once with its episode count, closest to the date first; music and
  unfinished plays left out; hidden titles never come back, per member.

    python tests/test_rediscover_and_year_ago.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio
from datetime import datetime, timedelta

app_db()
import src.database.connection as conn_mod  # noqa: E402
from src.database.models import User, WatchHistoryEntry as W  # noqa: E402
from src.services import on_this_day as otd, rediscover_recs as rd  # noqa: E402

NOW = datetime(2026, 11, 8, 20, 0)
with conn_mod.get_db_session() as db:
    db.add_all([User(id=1, plex_user_id="1", plex_username="owner", is_admin=True),
                User(id=2, plex_user_id="2", plex_username="member")])
    db.commit()
    n = [0]

    def play(uid, cat, title, at, series=None, done=True, ep=None):
        n[0] += 1
        db.add(W(user_id=uid, plex_user_id=str(uid), plex_item_id=f"k{n[0]}", title=title, series_title=series,
                 media_type=cat, viewed_at=at, completed=done, season=1 if ep else None, episode=ep))
    long_ago = NOW - timedelta(days=3 * 365)
    play(1, "movie", "Heat", long_ago)
    play(1, "movie", "Heat", long_ago + timedelta(days=30))
    play(1, "movie", "Far Signal", NOW - timedelta(days=100))                # touched recently
    play(1, "movie", "Half Watched", long_ago, done=False)
    for e in range(1, 8):
        play(1, "show", f"e{e}", long_ago + timedelta(days=e), series="Night Shift", ep=e)
    for e in range(1, 3):
        play(1, "show", f"p{e}", long_ago, series="Pilot Only", ep=e)      # dabbled
    then = NOW.replace(year=2025)
    play(1, "movie", "Snow Post", then + timedelta(days=1))
    play(1, "movie", "Far Away", then + timedelta(days=10))               # outside the window
    play(1, "show", "x1", then - timedelta(days=2), series="Office Blocks", ep=1)
    play(1, "show", "x2", then - timedelta(days=1), series="Office Blocks", ep=2)
    play(1, "movie", "Started", then, done=False)
    play(1, "music", "Track", then)
    play(2, "movie", "Member Film", then)
    db.commit()

movies = rd.loved_long_ago(1, "movie", NOW)
check("rediscover: a film finished (twice) long ago counts; one touched recently or never finished does not",
      set(movies) == {"heat"} and movies["heat"]["times"] == 2)
shows = rd.loved_long_ago(1, "show", NOW)
check("...a series watched in earnest counts, one dabbled in does not", set(shows) == {"night shift"}
      and shows["night shift"]["times"] == 7)
check("the fact line in the member's language",
      rd.fact_line(long_ago, 2, "movie", "de", NOW) == "Zuletzt vor 3 Jahren · 2× gesehen"
      and rd.fact_line(long_ago, 7, "show", "en", NOW) == "Last watched 3 years ago · 7 episodes watched"
      and rd.fact_line(long_ago, 1, "movie", "en", NOW) == "Last watched 3 years ago · seen once")


async def _ranked(user_id, cat, pool, top_n=8):
    return list(reversed(pool))[:top_n]          # the taste ranking, stood in for
import src.services.recommendations_engine as reng  # noqa: E402
reng.score_arr_items = _ranked
import src.services.user_language as ul  # noqa: E402
ul._cache[1] = ("de", 10 ** 12)
owned = [{"title": "Heat", "year": 1995, "genres": "Crime"}, {"title": "Not Loved", "year": 2000}]
recs = asyncio.run(rd.rediscover_recs(1, "movie", owned, now=NOW))
check("only owned loved titles, with their line and the Why?",
      [r["title"] for r in recs] == ["Heat"] and recs[0]["lane"] == "rediscover"
      and recs[0]["reason"] == "Zuletzt vor 3 Jahren · 2× gesehen"
      and recs[0]["score_factors"]["lane"] == "rediscover")
fams = rd.one_per_family([{"title": t} for t in ("The Speed and the Run: Tokyo", "The Speed and the Run",
                                                  "Speed & Run", "2 Speed 2 Run", "Quiet Morning, Harbor",
                                                  "Heat", "Heat 2")], 5)
check("a film series once: four of one series become one",
      [x["title"] for x in fams] == ["The Speed and the Run: Tokyo", "Quiet Morning, Harbor", "Heat"])
check("nothing owned of it: nothing", asyncio.run(rd.rediscover_recs(1, "movie", [{"title": "Other"}], now=NOW)) == [])

got = otd.a_year_ago(1, NOW)
check("a year ago: within three days, a series once with its episodes, closest first; no music, "
      "no unfinished play, no one else's",
      [(x["title"], x["category"], x["episodes"]) for x in got] == [("Snow Post", "movie", 0), ("Office Blocks", "show", 2)])
otd.hide(1, "Snow Post")
check("a hidden title never comes back, for this member only",
      [x["title"] for x in otd.a_year_ago(1, NOW)] == ["Office Blocks"]
      and [x["title"] for x in otd.a_year_ago(2, NOW)] == ["Member Film"])
leap = otd.a_year_ago(1, datetime(2028, 2, 29, 12, 0))
check("29 February looks at 28 February", leap == [])

check.done()
