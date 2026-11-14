"""Shoko phase 2: what Shoko knows per anime episode reaches the curator.

Shoko keeps AniDB's episode data locally (type, number, rating with votes)
and the series' tags (weight, spoiler flags). Verified on the live server
2026-11-14 (6.0.0-dev.596): 98 % of watched regular episodes are rated, on
few votes (median 16); an unrated one comes as 0.0 with 0 votes; recaps are
filed as Special; a series is reached from a play's Shoko episode, never by
title search; the Refresh, MyList-sync and Action routes queue AniDB work.

A fake Shoko speaking those routes, the real app database:
- the refresh places the ledger's episodes on their series and copies
  episodes and tags, only reading, and refreshes a series weekly;
- an episode is "the weakest" only when it is a regular one, on enough
  votes, among the season's two lowest and a point under the median;
- the stalled-series opener fact carries it.

    python tests/test_shoko_meta.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio
import json
from datetime import datetime, timedelta

import httpx

from src.config import settings
from src.services import shoko_client

conn = app_db()

SERIES = {
    77: {"name": "Night Harbor", "anidb": 9001, "episodes": [
        # (id, type, number, rating, votes)
        (701, "Episode", 1, 7.4, 20), (702, "Episode", 2, 7.1, 18), (703, "Episode", 3, 4.6, 16),
        (704, "Episode", 4, 7.3, 15), (705, "Episode", 5, 7.0, 3), (706, "Episode", 6, 7.6, 14),
        (707, "Episode", 7, 0.0, 0), (708, "Special", 1, 3.0, 12),   # a recap special
        (709, "Credits", 1, 0.0, 0)],
        "tags": [("night", 600, False, False), ("everybody dies", 400, True, False),
                 ("betrayal", 0, False, True), ("harbour", 300, False, False)]},
}
EP_SERIES = {e[0]: sid for sid, s in SERIES.items() for e in s["episodes"]}


class FakeShoko:
    def __init__(self):
        self.paths: list = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        p = request.url.path.removeprefix("/api/v3")
        self.paths.append((request.method, p, dict(request.url.params)))
        parts = p.strip("/").split("/")
        if parts[0] == "Episode" and len(parts) == 2:
            sid = EP_SERIES.get(int(parts[1]))
            return httpx.Response(200, json={"IDs": {"ID": int(parts[1]), "ParentSeries": sid}}) \
                if sid else httpx.Response(404)
        if parts[0] == "Series" and len(parts) >= 2 and int(parts[1]) in SERIES:
            s = SERIES[int(parts[1])]
            if len(parts) == 2:
                return httpx.Response(200, json={"Name": s["name"], "IDs": {"ID": int(parts[1]), "AniDB": s["anidb"]}})
            if parts[2] == "Episode":
                q = request.url.params
                lst = [{"IDs": {"ID": i}, "AniDB": {"Type": t, "EpisodeNumber": n, "Title": f"Ep {n}",
                                                   "AirDate": "2024-01-01",
                                                   "Rating": {"Value": r, "MaxValue": 10, "Votes": v}}
                                                    if q.get("includeDataFrom") == "AniDB" else None}
                       for i, t, n, r, v in s["episodes"]]
                if q.get("pageSize") != "0":
                    lst = lst[:20]
                return httpx.Response(200, json={"Total": len(lst), "List": lst})
            if parts[2] == "Tags":
                return httpx.Response(200, json=[{"Name": n, "Weight": w, "IsSpoiler": sp, "IsLocalSpoiler": lsp}
                                                 for n, w, sp, lsp in s["tags"]])
        return httpx.Response(404)


fake = FakeShoko()
_orig_client = shoko_client.new_client
shoko_client.new_client = lambda transport=None: _orig_client(httpx.MockTransport(fake.handler))
settings.SHOKO_URL, settings.SHOKO_API_KEY = "http://shoko.test:8111", "k-test"

from src.database.models import ShokoSeriesMeta, ShokoWatchSync, User, WatchHistoryEntry as W  # noqa: E402
import src.services.shoko_meta as sm  # noqa: E402

now = datetime(2026, 11, 14, 20, 0)
with conn.get_db_session() as db:
    db.add(User(id=1, plex_user_id="p1", plex_username="u1"))
    db.flush()
    plays = [(701, 60, True), (702, 59, True), (703, 58, False)]   # left at episode 3
    for n, (ep, days, done) in enumerate(plays, 1):
        w = W(user_id=1, plex_user_id="p1", plex_item_id=f"e{n}", title=f"Ep {n}", series_title="Night Harbor",
              media_type="anime", season=1, episode=n, viewed_at=now - timedelta(days=days), completed=done)
        db.add(w)
        db.flush()
        db.add(ShokoWatchSync(watch_history_id=w.id, user_id=1, status="sent", shoko_episode_ids=str(ep)))
    db.commit()

res = asyncio.run(sm.refresh(now=now))
check("the refresh places the ledger's episodes on their series and copies it",
      res["ok"] and res["series"] == 1 and res["fetched"] == 1, res)
with conn.get_db_session() as db:
    row = db.query(ShokoSeriesMeta).one()
    eps = json.loads(row.episodes)
    tags = json.loads(row.tags)
    title_anidb = (row.title, row.anidb_id)
check("...every episode of the series, the ones beyond the first page too", len(eps) == 9, len(eps))
check("...with its AniDB type, number and rating", eps[2] == {"id": 703, "type": "Episode", "number": 3,
      "title": "Ep 3", "air_date": "2024-01-01", "rating": 4.6, "votes": 16}, eps[2])
check("...an unrated episode (0.0 on 0 votes) is no rating, not a zero",
      next(e for e in eps if e["id"] == 707)["rating"] is None)
check("...the series' title and AniDB id", title_anidb == ("Night Harbor", 9001), title_anidb)
check("...tags with weight, either spoiler flag as one",
      {t["name"]: t["spoiler"] for t in tags} == {"night": False, "everybody dies": True,
                                                   "betrayal": True, "harbour": False}, tags)
check("only reads: every call a GET, never a Refresh, MyList-sync or Action route",
      all(m == "GET" and not any(x in pth for x in ("Refresh", "MyList", "/Action")) for m, pth, _ in fake.paths),
      fake.paths)
check("...the episode list asks for all of them, the missing and hidden ones, with AniDB data",
      any(pth == "/Series/77/Episode" and q.get("pageSize") == "0" and q.get("includeMissing") == "true"
          and q.get("includeDataFrom") == "AniDB" for _m, pth, q in fake.paths))
fake.paths.clear()
asyncio.run(sm.refresh(now=now + timedelta(days=2)))
check("a stored series is not fetched again within the week",
      not any("/Series/77" in pth for _m, pth, _q in fake.paths), fake.paths)
asyncio.run(sm.refresh(now=now + timedelta(days=8)))
check("...and is after it", any(pth == "/Series/77/Episode" for _m, pth, _q in fake.paths))

# ── the weakest episode ──────────────────────────────────────────────────────
low = sm.low_point(eps, 703)
check("an episode among the season's weakest, a point under the median, is named",
      low and low["episode"] == 3 and low["rank_from_bottom"] == 1 and low["anidb_rating"] == 4.6
      and low["of"] == 5, low)
check("...an ordinary episode is not", sm.low_point(eps, 702) is None)
check("...one rated on too few votes counts nowhere (episode 5, 3 votes)", sm.low_point(eps, 705) is None)
check("...a recap special is never ranked as an episode", sm.low_point(eps, 708) is None)
check("...a season with too few rated episodes ranks nothing",
      sm.low_point([e for e in eps if e["id"] in (701, 703)], 703) is None)

stop = sm.stop_point(1, "Night Harbor")
check("where they left the series: the weakest episode, unfinished",
      stop and stop["episode"] == 3 and stop["finished"] is False, stop)
check("...nothing for a series without Shoko data", sm.stop_point(1, "Elsewhere") is None)

# ── the opener fact ──────────────────────────────────────────────────────────
from zoneinfo import ZoneInfo  # noqa: E402
import src.services.chat_starters as cs  # noqa: E402

facts = cs.collect_facts(1, now=now, zone=ZoneInfo("Europe/Berlin"))
st = next((f for f in facts if f["kind"] == "stalled_series"), None)
check("the stalled-series fact names the weakest episode they stopped at",
      st and st["title"] == "Night Harbor" and st.get("stopped_at_weakest_episode", {}).get("episode") == 3, st)

settings.SHOKO_URL, settings.SHOKO_API_KEY = None, None
check("without Shoko the refresh does nothing", asyncio.run(sm.refresh())["skipped"] == "no_shoko")

check.done()
