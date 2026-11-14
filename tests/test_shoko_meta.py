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
        # (name, weight, IsSpoiler, IsLocalSpoiler, tag id)
        "tags": [("Japan", 600, False, False, 2672), ("dark fantasy", 600, False, False, 9000),
                 ("present", 600, False, False, 2624), ("action", 500, False, False, 9002),
                 ("everybody dies", 400, True, False, 9200), ("betrayal", 0, False, True, 9201),
                 ("manga", 0, False, False, 9100), ("elements", 0, False, False, 2611),
                 ("based on real events", 300, False, False, 4616), ("space", 200, False, False, 2627),
                 ("friendship", 0, False, False, 9300), ("death game", 100, False, False, 9400),
                 # the third live run: the dropped nodes themselves, a season, grouping tags
                 ("Earth", 600, False, False, 2630), ("summer", 400, False, False, 4576),
                 ("cast", 0, False, False, 6230), ("male protagonist", 0, False, False, 6231),
                 ("fictional location", 300, False, False, 2628), ("virtual world", 300, False, False, 9500),
                 ("fantasy", 500, False, False, 9001), ("swordplay", 400, False, False, 9003)]},
}
# AniDB's tree as /Tag/AniDB lists it: {id: parent}
TREE = {2672: 2834, 2834: 2630, 2630: 2613, 2613: 2610, 2610: None, 2627: 2613,
        4576: 6185, 6185: 2612, 6230: 2611, 6231: 6230, 2628: 2613, 9500: 2628, 9003: 9002,
        9000: 9001, 9001: 2611, 2611: None, 9002: 2611, 9200: 2611, 9201: 2607, 2607: None,
        2624: 2612, 2612: 2610, 9100: 2609, 2609: None, 4616: 6151, 6151: None, 9300: 2607,
        9400: None}
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
                return httpx.Response(200, json=[{"ID": i, "ParentID": TREE.get(i), "Name": n, "Weight": w,
                                                  "IsSpoiler": sp, "IsLocalSpoiler": lsp}
                                                 for n, w, sp, lsp, i in s["tags"]])
        if parts == ["Tag", "AniDB"] and request.url.params.get("pageSize") == "0":
            return httpx.Response(200, json={"Total": len(TREE), "List": [
                {"ID": i, "ParentID": p, "Name": f"t{i}"} for i, p in TREE.items()]})
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
      {t["name"]: t["spoiler"] for t in tags if t["spoiler"]} == {"everybody dies": True, "betrayal": True}, tags)
keep = {t["name"]: t["keep"] for t in tags}
check("the tag tree sorts out what says nothing about the series: places under Earth, 'present', "
      "source material, the root tags themselves",
      not any(keep[n] for n in ("Japan", "present", "manga", "elements")), keep)
check("...the dropped nodes themselves and everything under them: 'Earth', 'summer' under 'season' "
      "(both slipped through the third live run)", not keep["Earth"] and not keep["summer"], keep)
check("...a broader tag goes when a narrower one under it carries at least its weight: 'cast' (0) "
      "beside 'male protagonist', 'fictional location' (300) beside 'virtual world' (300)",
      not keep["cast"] and not keep["fictional location"]
      and keep["male protagonist"] and keep["virtual world"], keep)
check("...'fantasy' (500) gives way to 'dark fantasy' (600) under it",
      keep["fantasy"] is False, keep)
check("...but one weighted above all its narrower ones stays: 'action' (500) over 'swordplay' (400)",
      keep["action"] is True and keep["swordplay"] is True, keep)
check("...and keeps the content: elements, themes, a place that is a setting ('space'), "
      "'based on real events' out of the technical branch, a tag with no parent yet",
      all(keep[n] for n in ("dark fantasy", "action", "space", "based on real events", "friendship",
                            "death game")), keep)
check("the tag tree is read once per run, not per series",
      sum(1 for _m, pth, _q in fake.paths if pth == "/Tag/AniDB") == 1)
got = sm.series_tags_for(anidb_id=9001)
check("the curator's tags: no spoilers, strongest first, 'applies, unweighted' (0) last, not dropped",
      got == ["dark fantasy", "action", "swordplay", "based on real events", "virtual world", "space",
              "death game", "friendship", "male protagonist"], got)
check("...found by title when there is no AniDB id", sm.series_tags_for(title="Night Harbor") == got)
check("...and nothing for a series Shoko does not know", sm.series_tags_for(anidb_id=1) is None)

from src.routers.chat import _build_hidden_context  # noqa: E402
blk = _build_hidden_context("Night Harbor", {"title": "Night Harbor", "shoko_tags": got}, domain="anime")
check("the anime block carries them", "- AniDB tags (strongest first): dark fantasy, action" in blk, blk)
check("...and leaves the line out without Shoko data",
      "AniDB tags" not in _build_hidden_context("X", {"title": "X"}, domain="anime"))
check("only reads: every call a GET, never a Refresh, MyList-sync or Action route",
      all(m == "GET" and not any(x in pth for x in ("Refresh", "MyList", "/Action")) for m, pth, _ in fake.paths),
      fake.paths)
check("one lookup per series: the series' episodes place the other plays (the first live run "
      "spent 500 lookups on 83 series and stopped there)",
      sum(1 for _m, pth, _q in fake.paths if pth.startswith("/Episode/")) == 1,
      [pth for _m, pth, _q in fake.paths if pth.startswith("/Episode/")])
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
check("the first episode is trying a series, not leaving it (the #312 opener rule)",
      sm._left_midway(eps, 701) is False)
check("...the last regular episode finished the season: no stop (the live run named a finale)",
      sm._left_midway(eps, 707) is False)
check("...an episode inside the season is one", sm._left_midway(eps, 703) is True)
check("...a special is never a place in the season", sm._left_midway(eps, 708) is False)

# ── the opener fact ──────────────────────────────────────────────────────────
from zoneinfo import ZoneInfo  # noqa: E402
import src.services.chat_starters as cs  # noqa: E402

facts = cs.collect_facts(1, now=now, zone=ZoneInfo("Europe/Berlin"))
st = next((f for f in facts if f["kind"] == "stalled_series"), None)
check("the stalled-series fact names the weakest episode they stopped at",
      st and st["title"] == "Night Harbor" and st.get("stopped_at_weakest_episode", {}).get("episode") == 3, st)

# ── recaps, credits, trailers: no progress, no stall ─────────────────────────
# Plex/TVDB number a recap as a regular episode (here S01E04); Shoko knows it
# is AniDB's Special 1. Such a play is neither a finished episode nor a drop.
with conn.get_db_session() as db:
    w = W(user_id=1, plex_user_id="p1", plex_item_id="e4", title="Recap", series_title="Night Harbor",
          media_type="anime", season=1, episode=4, viewed_at=now - timedelta(days=30), completed=True)
    db.add(w)
    db.flush()
    recap_id = w.id
    db.add(ShokoWatchSync(watch_history_id=w.id, user_id=1, status="sent", shoko_episode_ids="708"))
    db.commit()
check("a play on a recap special is no regular episode", sm.non_regular_play_ids(1) == {recap_id},
      sm.non_regular_play_ids(1))
check("...the regular ones are", not ({recap_id} ^ sm.non_regular_play_ids(1)) - {recap_id})

import src.services.taste_engine as te  # noqa: E402
rows = te._load_watch_entries(1, "anime")
check("the taste rebuild never reads the recap play as a finished episode",
      [r["title"] for r in rows] == ["Ep 3", "Ep 2", "Ep 1"], [r["title"] for r in rows])
facts = cs.collect_facts(1, now=now, zone=ZoneInfo("Europe/Berlin"))
st = next((f for f in facts if f["kind"] == "stalled_series"), None)
check("...nor do the openers: the stall stays at the episode they left, 58 days ago, not the recap",
      st and st["days_since"] == 58 and st["plays_before_stall"] == 3
      and st.get("stopped_at_weakest_episode", {}).get("episode") == 3, st)
check("a play on an episode Shoko has not described yet still counts (nothing is guessed)",
      sm.non_regular_play_ids(2) == set())

settings.SHOKO_URL, settings.SHOKO_API_KEY = None, None
check("without Shoko the refresh does nothing", asyncio.run(sm.refresh())["skipped"] == "no_shoko")

check.done()
