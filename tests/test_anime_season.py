"""The anime season as a Wrap-Up period (wrapup_facts).

- season keys: Winter from January, Spring April, Summer July, Fall
  October; a future season is no period, a running one ends now;
- available: the seasons with anime plays, newest first;
- the season's facts: started (a first episode this season), finished
  (every aired episode of a season, from Sonarr, the last one this
  season; "the whole series" for an ended one), dropped (begun, not
  finished, untouched for a month at the season's end); a series
  resumed later is not dropped.

    python tests/test_anime_season.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

from datetime import datetime, timedelta

app_db()
import src.database.connection as conn_mod  # noqa: E402
from src.database.models import User, WatchHistoryEntry as W  # noqa: E402
from src.services import proactive_messages as pm  # noqa: E402
from src.services import wrapup_facts as wf  # noqa: E402

NOW = datetime(2026, 11, 8, 20, 0)
p = wf.resolve_period("season:2026-summer", NOW)
check("Summer: July to September, closed once over",
      (p.start, p.end, p.closed, p.label) == (datetime(2026, 7, 1), datetime(2026, 10, 1), True, "Summer 2026 season"))
check("a running season ends now; Fall runs to the year's end",
      wf.resolve_period("season:2026-fall", NOW).end == NOW and not wf.resolve_period("season:2026-fall", NOW).closed)
try:
    wf.resolve_period("season:2027-winter", NOW)
    future = False
except ValueError:
    future = True
check("a future season is no period", future)
check("season of a date", wf.season_key(datetime(2026, 2, 3)) == "season:2026-winter"
      and wf.season_key(datetime(2026, 10, 1)) == "season:2026-fall")

with conn_mod.get_db_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True))
    db.commit()
    n = [0]

    def play(series, at, s=1, e=1, done=True):
        n[0] += 1
        db.add(W(user_id=1, plex_user_id="1", plex_item_id=f"k{n[0]}", title=f"ep{e}", series_title=series,
                 media_type="anime", viewed_at=at, completed=done, season=s, episode=e))
    jul = datetime(2026, 7, 10, 21, 0)
    for e in range(1, 4):
        play("Done Anime", jul + timedelta(days=e), e=e)                   # all 3 aired: finished
    play("Dropped Anime", jul, e=1)
    play("Dropped Anime", jul + timedelta(days=1), e=2)                     # then nothing
    play("Resumed Anime", jul, e=1)
    play("Resumed Anime", datetime(2026, 10, 20), e=2)                       # came back in Fall
    play("Old Friend", datetime(2026, 3, 1), e=1)
    play("Old Friend", jul + timedelta(days=40), e=2)                        # begun before Summer
    db.commit()

pm._sonarr_index_cache["data"] = {"by_tvdb": {}, "by_title": {
    "done anime": [{"status": "ended", "series_type": "anime", "total_episodes": 3, "per_season": {1: 3}}],
    "dropped anime": [{"status": "continuing", "series_type": "anime", "total_episodes": 12, "per_season": {1: 12}}],
    "resumed anime": [{"status": "continuing", "series_type": "anime", "total_episodes": 12, "per_season": {1: 12}}],
}}
with conn_mod.get_db_session() as db:
    a = wf._anime_season(db, 1, p, NOW)
check("started this season: the first episodes; a series begun before is not",
      sorted(x["title"] for x in a["started"]) == ["Done Anime", "Dropped Anime", "Resumed Anime"])
check("finished: every aired episode, the last this season; an ended series as a whole",
      a["finished"] == [{"title": "Done Anime", "season": 1, "series_done": True}])
check("dropped: begun, unfinished, untouched for a month; one resumed later is not",
      [(x["title"], x["episodes"], x["of"]) for x in a["dropped"]] == [("Dropped Anime", 2, 12), ("Old Friend", 2, None)])
av = wf.available(1)
check("available seasons, newest first", av["seasons"] == ["season:2026-fall", "season:2026-summer",
                                                           "season:2026-winter"])

check.done()
