"""Milestones (services/milestones): fixed rules over the watch history.

- films: the Nth DIFFERENT film finished, at the play and with the title
  that crossed it; a rewatch or an unfinished film does not count;
- episodes, hours (finished plays at their length, others at what played,
  music included), different artists; a series finished = every aired
  episode of an ENDED series (Sonarr), a continuing one never; anime
  apart from series; years since the first play, none in the future;
- stored per member: the first run is history (nothing announced), a
  later one announces what is new and recent, once (mark_announced), and
  not after two weeks; a kind switched off is out of the history and the
  announcements; in_period gives the Wrap-Up slide's;
- the phrases the page and the curator read.

    python tests/test_milestones.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

from datetime import datetime, timedelta

app_db()
import src.database.connection as conn_mod  # noqa: E402
from src.database.models import User, WatchHistoryEntry as W  # noqa: E402
from src.services import milestones as ms  # noqa: E402

T0 = datetime(2024, 1, 1, 20, 0)
NOW = datetime(2026, 11, 8, 20, 0)
with conn_mod.get_db_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True))
    db.commit()
    n = [0]

    def play(cat, title, at, series=None, done=True, dur=None, off=None, s=None, e=None):
        n[0] += 1
        db.add(W(user_id=1, plex_user_id="1", plex_item_id=f"k{n[0]}", title=title, series_title=series,
                 media_type=cat, viewed_at=at, completed=done, duration_ms=dur, view_offset_ms=off,
                 season=s, episode=e))
    for i in range(9):
        play("movie", f"Film {i}", T0 + timedelta(days=i), dur=2 * 3_600_000)
    play("movie", "Film 0", T0 + timedelta(days=20), dur=2 * 3_600_000)           # a rewatch
    play("movie", "Unfinished", T0 + timedelta(days=21), done=False, off=600_000)
    play("movie", "Tenth", T0 + timedelta(days=30), dur=2 * 3_600_000)
    for ep in range(1, 4):                                                       # an ended series, all 3
        play("show", f"e{ep}", T0 + timedelta(days=40 + ep), series="Done Show", dur=3_600_000, s=1, e=ep)
    for ep in range(1, 4):                                                       # still running
        play("show", f"r{ep}", T0 + timedelta(days=50 + ep), series="Running", dur=3_600_000, s=1, e=ep)
    for ep in range(1, 3):
        play("anime", f"a{ep}", T0 + timedelta(days=60 + ep), series="Short Anime", dur=1_440_000, s=1, e=ep)
    play("music", "Track", T0 + timedelta(days=70), series="Artist A", done=False, off=180_000)
    db.commit()

index = {"by_tvdb": {}, "by_title": {
    "done show": [{"status": "ended", "total_episodes": 3, "series_type": "standard"}],
    "running": [{"status": "continuing", "total_episodes": 3, "series_type": "standard"}],
    "short anime": [{"status": "ended", "total_episodes": 2, "series_type": "anime"}]}}
items = ms.compute(1, index, now=NOW)
by = {(m["kind"], m["value"]): m for m in items}
check("the 10th DIFFERENT film, at its play and with its title (rewatch and unfinished left out)",
      by[("films", 10)]["title"] == "Tenth" and by[("films", 10)]["reached_at"] == T0 + timedelta(days=30))
check("a series finished: every aired episode of an ended one; a continuing one never",
      by[("series", 1)]["title"] == "Done Show" and ("series", 2) not in by)
check("anime apart from series", by[("anime", 1)]["title"] == "Short Anime")
check("hours: finished plays at their length, crossing 100 h nowhere here; years since the first play",
      ("hours", 100) not in by and by[("years", 1)]["reached_at"] == datetime(2025, 1, 1, 20, 0)
      and by[("years", 2)]["reached_at"] == datetime(2026, 1, 1, 20, 0) and ("years", 3) not in by)
check("no series milestones without the Sonarr totals", not any(m["kind"] == "series" for m in ms.compute(1, None, now=NOW)))
ev = [(T0 + timedelta(hours=i), f"t{i}", 60) for i in range(3)]
check("crossings: a step is reached at the event that crosses it",
      [(m["value"], m["title"]) for m in ms._crossings(ev, "hours")] == [(100, "t1")])

check("phrases", [ms.phrase(m) for m in (
    {"kind": "films", "value": 100}, {"kind": "anime", "value": 1}, {"kind": "hours", "value": 1000},
    {"kind": "series", "value": 22}, {"kind": "years", "value": 1})] == [
    "100th film watched", "first anime series finished", "1,000 hours played", "22nd series finished",
    "1 year since the first play"])

first = ms.store(1, items, now=NOW)
check("the first run is history: nothing to announce", first == [] and ms.pending_announcements(1) == [])
new = {"kind": "films", "value": 50, "reached_at": NOW - timedelta(days=2), "title": "Fiftieth"}
fresh = ms.store(1, items + [new], now=NOW)
check("a later run announces what is new and recent", [m["title"] for m in fresh] == ["Fiftieth"]
      and [m["title"] for m in ms.pending_announcements(1)] == ["Fiftieth"])
ms.mark_announced(1, ["films:50"])
check("...once", ms.pending_announcements(1) == [])
ms.store(1, items + [new, {"kind": "episodes", "value": 100, "reached_at": NOW - timedelta(days=30),
                           "title": "Old News"}], now=NOW)
check("one reached a month before it was seen is history, not news", ms.pending_announcements(1) == [])
ms.set_kind(1, "years", False)
check("a kind switched off leaves the history", not any(m["kind"] == "years" for m in ms.history(1))
      and ms.kinds_off(1) == {"years"})
ms.set_kind(1, "years", True)
inside = ms.in_period(1, datetime(2024, 1, 1), datetime(2024, 4, 1))
check("the period's milestones for the Wrap-Up slide, oldest first",
      [m["title"] for m in inside] == ["Tenth", "Done Show", "Short Anime"])
try:
    ms.set_kind(1, "nonsense", False)
    bad = False
except ValueError:
    bad = True
check("an unknown kind is refused", bad)

check.done()
