"""The owner's Shoko pins (services/shoko_pins, routers/shoko) end to end:
the real migrated database, the real router, the sync honouring the pins,
and a fake Shoko (httpx.MockTransport) speaking the routes of the live
server's OpenAPI (Shoko 6.0.0-dev.588): /Series/AniDB/Search,
/Series/AniDB/{id}/Series, /Series/{id}/Episode, GET|PUT /Episode/{id}/UserData.

    python tests/test_shoko_pins.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio
import json
from datetime import datetime, timedelta

import httpx
from fastapi.testclient import TestClient

conn = app_db()
from src.config import settings  # noqa: E402
from src.database.models import ShokoPin, ShokoWatchSync, User, WatchHistoryEntry  # noqa: E402
from src.main import app  # noqa: E402
from src.routers.auth import get_current_user  # noqa: E402
from src.services import shoko_client, shoko_sync  # noqa: E402
from src.services.media_server.base import FileRef, MediaItem  # noqa: E402

KEY = "k-owner"


class FakeShoko:
    """Food Wars (AniDB 10901, Shoko series 2441, 24 regular episodes with
    Shoko ids 9001..9024) and the file of "Show A" episode 1 (Shoko 101)."""

    def __init__(self):
        self.user_data: dict = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v3")
        q = request.url.params
        if path == "/Init/Version":
            return httpx.Response(200, json={"Server": {"Version": "6.0.0-dev.588"}})
        if request.headers.get("apikey") != KEY:
            return httpx.Response(401)
        if path == "/User/Current":
            return httpx.Response(200, json={"ID": 1, "Username": "admin", "IsAdmin": True,
                                             "PlexUsernames": ""})
        if path == "/Series/AniDB/Search":
            hits = [{"ID": 11664, "ShokoID": 2516, "Type": "OVA", "Title": "Shokugeki no Souma OAD",
                     "AirDate": "2015-11-03", "EpisodeCount": 5},
                    {"ID": 10901, "ShokoID": 2441, "Type": "TV", "Title": "Shokugeki no Souma",
                     "AirDate": "2015-04-04", "EpisodeCount": 24}]
            ok = q.get("local") == "true" and "food" in q.get("query", "").casefold()
            return httpx.Response(200, json={"Total": 2 if ok else 0, "List": hits if ok else []})
        if path == "/Series/AniDB/10901/Series":
            return httpx.Response(200, json={"IDs": {"ID": 2441, "AniDB": 10901},
                                             "Name": "Shokugeki no Souma"})
        if path == "/Series/AniDB/20000/Series":     # a second cour: 12 episodes
            return httpx.Response(200, json={"IDs": {"ID": 3000, "AniDB": 20000},
                                             "Name": "Part Two"})
        if path == "/Series/3000/Episode":
            return httpx.Response(200, json={"Total": 12, "List": [
                {"IDs": {"ID": 8000 + n}, "AniDB": {"EpisodeNumber": n, "Type": "Episode"}}
                for n in range(1, 13)]})
        if path.startswith("/Series/AniDB/") and path.endswith("/Series"):
            return httpx.Response(404)
        if path == "/Series/2441/Episode":
            if q.get("type") != "Episode" or q.get("includeDataFrom") != "AniDB":
                return httpx.Response(200, json={"Total": 0, "List": []})
            return httpx.Response(200, json={"Total": 24, "List": [
                {"IDs": {"ID": 9000 + n}, "AniDB": {"EpisodeNumber": n, "Type": "Episode"}}
                for n in range(1, 25)]})
        if path == "/File/PathEndsWith":
            if q.get("path") == "a01.mkv":
                return httpx.Response(200, json=[{"ID": 1, "Size": 10,
                                                  "SeriesIDs": [{"EpisodeIDs": [{"ID": 101}]}]}])
            return httpx.Response(200, json=[])
        if path.startswith("/Episode/") and path.endswith("/UserData"):
            ep = int(path.split("/")[2])
            if request.method == "GET":
                return httpx.Response(200, json=self.user_data.get(ep, {"PlaybackCount": 0}))
            self.user_data[ep] = json.loads(request.content)
            return httpx.Response(200, json=self.user_data[ep])
        return httpx.Response(404)


class Adapter:
    """Only "e1" (Show A episode 1) is still on the media server; no show
    library lists anything, so gone keys have no current copy."""

    async def item(self, key):
        if key == "e1":
            return MediaItem(key="e1", kind="episode", title="ep1",
                             files=(FileRef("/x/Show A/a01.mkv", 10),))
        return None

    async def libraries(self):
        return []

    async def children(self, key):
        return []


fake = FakeShoko()
_orig_client = shoko_client.new_client
shoko_client.new_client = lambda transport=None: _orig_client(httpx.MockTransport(fake.handler))
settings.SHOKO_URL, settings.SHOKO_API_KEY = "http://shoko.test:8111", KEY

T0 = datetime(2026, 10, 1, 20, 0)
with conn.get_db_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="DonDroid", is_admin=True))
    db.add(User(id=2, plex_user_id="2", plex_username="member"))

    def play(key, series, season, episode, days, title="x"):
        db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id=key, title=title,
                                 series_title=series, season=season, episode=episode,
                                 media_type="anime", viewed_at=T0 + timedelta(days=days),
                                 completed=True))
    play("old1", "Food Wars!", 1, 4, 1)
    play("old2", "Food Wars!", 1, 8, 2)
    play("old3", "Kim Possible", 1, 1, 3)
    play("old4", None, None, None, 4, title="Some Film")
    play("e1", "Show A", 1, 1, 5)
    # a season AniDB splits into two cours: episode 14 is part two's episode 3
    play("old5", "Mushoku", 1, 3, 6)
    play("old6", "Mushoku", 1, 14, 7)


def sync():
    return asyncio.run(shoko_sync.sync_watched(transport=httpx.MockTransport(fake.handler),
                                               adapter=Adapter()))["users"]["admin"]


def ledger():
    with conn.get_db_session() as db:
        rows = (db.query(WatchHistoryEntry.plex_item_id, ShokoWatchSync.status,
                         ShokoWatchSync.shoko_episode_ids, ShokoWatchSync.matched_by)
                .join(ShokoWatchSync, ShokoWatchSync.watch_history_id == WatchHistoryEntry.id).all())
    return {k: (s, e, h) for k, s, e, h in rows}


admin = User(id=1, plex_username="DonDroid", plex_user_id="1", is_active=True, is_admin=True)
member = User(id=2, plex_username="member", plex_user_id="2", is_active=True, is_admin=False)
client = TestClient(app)


def as_user(u):
    app.dependency_overrides[get_current_user] = lambda: u


# ── 1. a first run leaves the gone keys unmatched ───────────────────────────
r = sync()
led = ledger()
check("first run: Show A by its file, the four gone keys unmatched",
      led["e1"][0] == "sent" and all(led[k][0] == "unmatched"
                                     for k in ("old1", "old2", "old3", "old4", "old5", "old6")),
      led)

# ── 2. the list and its gate ─────────────────────────────────────────────────
as_user(member)
check("a member cannot read the list", client.get("/api/shoko/unmatched").status_code == 403)
check("nor pin", client.post("/api/shoko/pins", json={"series": "x", "season": 1,
                                                      "action": "skip"}).status_code == 403)
as_user(admin)
ov = client.get("/api/shoko/unmatched").json()
groups = {(g["series"], g["season"]): g for g in ov["groups"]}
check("the list groups by series and season, most plays first",
      ov["groups"][0]["series"] == "Food Wars!" and groups[("Food Wars!", 1)]["plays"] == 2
      and ("Kim Possible", 1) in groups and ("Some Film", None) in groups and ov["configured"], ov)

# ── 3. search ────────────────────────────────────────────────────────────────
res = client.get("/api/shoko/search", params={"q": "Food Wars"}).json()["results"]
check("search lists the collection's anime with AniDB id, type and episodes",
      any(a["anidb_id"] == 10901 and a["type"] == "TV" and a["episodes"] == 24 for a in res), res)
check("an empty query is refused", client.get("/api/shoko/search", params={"q": ""}).status_code == 422)

# ── 4. pins ──────────────────────────────────────────────────────────────────
rp = client.post("/api/shoko/pins", json={"series": "Food Wars!", "season": 1, "action": "link",
                                          "anidb_id": 10901}).json()
check("a link pin names Shoko's title and hands the season's two plays back",
      rp["ok"] and rp["requeued"] == 2 and rp["shoko_title"] == "Shokugeki no Souma", rp)
check("a link to an anime not in the collection is refused",
      client.post("/api/shoko/pins", json={"series": "Food Wars!", "season": 2, "action": "link",
                                           "anidb_id": 1}).status_code == 404)
check("a link without an AniDB id is refused",
      client.post("/api/shoko/pins", json={"series": "X", "season": 1,
                                           "action": "link"}).status_code == 422)
check("an unknown action is refused",
      client.post("/api/shoko/pins", json={"series": "X", "season": 1,
                                           "action": "delete"}).status_code == 422)
rs = client.post("/api/shoko/pins", json={"series": "Kim Possible", "season": 1,
                                          "action": "skip"}).json()
check("a skip pin hands its play back too", rs["requeued"] == 1, rs)
# A pin wins over the automatic match: Show A's next play goes to the pinned anime.
with conn.get_db_session() as db:
    db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id="e1", title="ep1",
                             series_title="Show A", season=1, episode=1, media_type="anime",
                             viewed_at=T0 + timedelta(days=9), completed=True))
client.post("/api/shoko/pins", json={"series": "Show A", "season": 1, "action": "link",
                                     "anidb_id": 10901})

# Cours: part one from episode 1, part two from episode 12.
client.post("/api/shoko/pins", json={"series": "Mushoku", "season": 1, "action": "link",
                                     "anidb_id": 10901})
rc = client.post("/api/shoko/pins", json={"series": "Mushoku", "season": 1, "action": "link",
                                          "anidb_id": 20000, "from_episode": 12}).json()
check("a second pin on the same season starts at its own episode", rc["ok"], rc)
check("a pin cannot start before episode 1",
      client.post("/api/shoko/pins", json={"series": "Mushoku", "season": 1, "action": "skip",
                                           "from_episode": 0}).status_code == 422)

# ── 5. the sync honours them ─────────────────────────────────────────────────
r = sync()
led = ledger()
check("linked: Food Wars! S1E4 and S1E8 become regular episodes 4 and 8 of the anime",
      led["old1"] == ("sent", "9004", "pin") and led["old2"] == ("sent", "9008", "pin"), led)
check("the dates are the plays' own",
      shoko_client.parse_time(fake.user_data[9008]["LastPlayedAt"]) == T0 + timedelta(days=2))
check("skipped: Kim Possible rests as skipped", led["old3"] == ("skipped", None, "pin"), led)
check("the film without a pin waits for its weekly retry", led["old4"][0] == "unmatched", led)
check("a pin wins over the file match", fake.user_data.get(9001, {}).get("PlaybackCount") == 1
      and fake.user_data.get(101, {}).get("PlaybackCount") == 1, fake.user_data)
check("cours: episode 3 is part one's 3, episode 14 is part two's 3",
      led["old5"] == ("sent", "9003", "pin") and led["old6"] == ("sent", "8003", "pin"), led)
check("the run counts the skip", r["skipped"] == 1 and r["sent"] == 5, r)

ov = client.get("/api/shoko/unmatched").json()
groups = {(g["series"], g["season"]): g for g in ov["groups"]}
check("the list: Food Wars! gone, Kim Possible shown as skipped with its pin",
      ("Food Wars!", 1) not in groups and groups[("Kim Possible", 1)]["status"] == "skipped"
      and groups[("Kim Possible", 1)]["pins"][0]["action"] == "skip", ov)
check("every pin is listed", sorted(p["series"] for p in ov["pins"])
      == ["Food Wars!", "Kim Possible", "Mushoku", "Mushoku", "Show A"], ov["pins"])
check("each pin says where it starts",
      sorted(p["from_episode"] for p in ov["pins"] if p["series"] == "Mushoku") == [1, 12], ov["pins"])
r = sync()
check("a skipped play is not retried", r["plays"] == 0, r)

# ── 6. re-pinning replaces, unpinning hands back ─────────────────────────────
client.post("/api/shoko/pins", json={"series": "Kim Possible", "season": 1, "action": "skip"})
with conn.get_db_session() as db:
    n = db.query(ShokoPin).filter(ShokoPin.series_key == "kim possible").count()
    pin_id = db.query(ShokoPin.id).filter(ShokoPin.series_key == "kim possible").scalar()
check("pinning the same series and season again replaces the pin", n == 1, n)
sync()      # the re-pin handed the play back; this run skips it again
check("re-skipped after the re-pin", ledger()["old3"][0] == "skipped", ledger()["old3"])
rd = client.delete(f"/api/shoko/pins/{pin_id}").json()
check("unpinning hands the skipped play back", rd["requeued"] == 1, rd)
r = sync()
check("and the next run tries it again: unmatched without a pin",
      ledger()["old3"][0] == "unmatched" and r["plays"] == 1, (r, ledger()["old3"]))
check("lifting a pin twice is a 404", client.delete(f"/api/shoko/pins/{pin_id}").status_code == 404)

# ── 7. not configured ────────────────────────────────────────────────────────
settings.SHOKO_API_KEY = None
check("search without Shoko set up says so",
      client.get("/api/shoko/search", params={"q": "x"}).status_code == 409)
check("the list still opens (pins stay), marked not configured",
      client.get("/api/shoko/unmatched").json()["configured"] is False)

app.dependency_overrides.clear()
shoko_client.new_client = _orig_client
check.done()
