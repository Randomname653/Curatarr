"""Shoko watched sync (services/shoko_sync + shoko_client) against a fake
Shoko served by httpx.MockTransport and the app's real migrated database.

The fake speaks the routes and shapes of the live server's OpenAPI
(Shoko 6.0.0-dev.588): /Init/Version, /User/Current, /File/PathEndsWith,
/Tmdb/Episode/{id}/Shoko/Episode, GET|PUT /Episode/{id}/UserData.

    python tests/test_shoko_sync.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx

conn = app_db()
from src.config import settings  # noqa: E402
from src.database.models import ShokoWatchSync, User, WatchHistoryEntry  # noqa: E402
from src.services import shoko_client, shoko_sync  # noqa: E402
from src.services.media_server.base import ExternalIds, FileRef, Library, MediaItem  # noqa: E402

KEY_OWNER, KEY_PARTNER, KEY_STRANGER = "k-owner", "k-partner", "k-stranger"


# ── a fake Shoko ─────────────────────────────────────────────────────────────

class FakeShoko:
    def __init__(self):
        self.users = {
            KEY_OWNER: {"ID": 1, "Username": "Default", "IsAdmin": True, "PlexUsernames": ""},
            KEY_PARTNER: {"ID": 2, "Username": "anna", "IsAdmin": False,
                          "PlexUsernames": "Anna_Plex, other"},
            KEY_STRANGER: {"ID": 3, "Username": "guest", "IsAdmin": False, "PlexUsernames": ""},
        }
        # file name -> file records (two "01.mkv" in different series)
        self.files = {
            "[Grp] Show A - 01 [1080p].mkv": [
                {"ID": 10, "Size": 1000, "SeriesIDs": [{"EpisodeIDs": [{"ID": 101}]}],
                 "Locations": [{"RelativePath": "Show A/[Grp] Show A - 01 [1080p].mkv"}]}],
            "01.mkv": [
                {"ID": 20, "Size": 2000, "SeriesIDs": [{"EpisodeIDs": [{"ID": 201}]}],
                 "Locations": [{"RelativePath": "Show B\\Season 1\\01.mkv"}]},
                {"ID": 21, "Size": 3000, "SeriesIDs": [{"EpisodeIDs": [{"ID": 301}]}],
                 "Locations": [{"RelativePath": "Show C/Season 1/01.mkv"}]}],
        }
        self.tmdb = {555: [{"IDs": {"ID": 401}}, {"IDs": {"ID": 402}}]}
        self.user_data: dict = {}      # (user id, episode) -> dict
        self.puts: list = []
        self.put_status = 200
        self.version = "6.0.0-dev.588"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v3")
        if path == "/Init/Version":
            return httpx.Response(200, json={"Server": {"Version": self.version}})
        user = self.users.get(request.headers.get("apikey", ""))
        if user is None:
            return httpx.Response(401)
        if path == "/User/Current":
            return httpx.Response(200, json=user)
        if path == "/File/PathEndsWith":
            files = self.files.get(request.url.params.get("path"), [])
            if request.url.params.get("include") != "XRefs":   # as the live server does
                files = [{**f, "SeriesIDs": None} for f in files]
            return httpx.Response(200, json=files)
        if path.startswith("/Tmdb/Episode/"):
            return httpx.Response(200, json=self.tmdb.get(int(path.split("/")[3]), []))
        if path.startswith("/Episode/") and path.endswith("/UserData"):
            ep = int(path.split("/")[2])
            if request.method == "GET":
                return httpx.Response(200, json=self.user_data.get(
                    (user["ID"], ep), {"PlaybackCount": 0, "LastPlayedAt": None,
                                       "IsFavorite": False, "UserTags": [], "UserRating": None}))
            if self.put_status != 200:
                return httpx.Response(self.put_status)
            body = json.loads(request.content)
            self.user_data[(user["ID"], ep)] = body
            self.puts.append((user["ID"], ep, body))
            return httpx.Response(200, json=body)
        return httpx.Response(404)


class FakeAdapter:
    """item() for the played keys; libraries/items/children for the
    current copies (series "Show A" now lives under key s1)."""

    def __init__(self, items):
        self.played = items
        self.calls = 0
        self.listings = 0

    async def item(self, key):
        self.calls += 1
        return self.played.get(key)

    async def libraries(self):
        return [Library(key="12", title="Anime", kind="show"),
                Library(key="9", title="Music", kind="music")]

    async def items_of(self, lib):
        if lib.key == "12":
            yield MediaItem(key="s1", kind="series", title="Show A")
            yield MediaItem(key="s9", kind="series", title="Other")

    def items(self, lib, kinds=()):
        self.listings += 1
        return self.items_of(lib)

    async def children(self, key):
        if key == "s1":
            return [MediaItem(key="s1-1", kind="season", title="Season 1", season_number=1)]
        if key == "s1-1":
            return [MediaItem(key="e1", kind="episode", title="ep1", season_number=1, episode_number=1,
                              files=ITEMS["e1"].files)]
        return []


def ep(key, path=None, size=None, tmdb=None, kind="episode"):
    return MediaItem(key=key, kind=kind, title=key,
                     files=(FileRef(path, size),) if path else (),
                     external_ids=ExternalIds(tmdb_id=tmdb))


ITEMS = {
    "e1": ep("e1", "/data/AnimeShows/Show A/[Grp] Show A - 01 [1080p].mkv", 1000),
    # no size from the server: the folders above the file decide
    "e2": ep("e2", "D:\\Anime\\Show B\\Season 1\\01.mkv"),
    "e3": ep("e3", "/x/unknown.mkv", 5, tmdb=555),            # TMDB fallback, two eps
    "e4": ep("e4", "/x/nowhere.mkv", 7),                      # Shoko has nothing
}

T0 = datetime(2026, 10, 1, 20, 0)
with conn.get_db_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="DonDroid", is_admin=True))
    db.add(User(id=2, plex_user_id="2", plex_username="anna_plex"))

    def play(uid, item, t, completed=True, media_type="anime", source=None):
        db.add(WatchHistoryEntry(user_id=uid, plex_user_id=str(uid), plex_item_id=item,
                                 title=item, media_type=media_type, viewed_at=t,
                                 completed=completed, source=source))
    play(1, "e1", T0)
    play(1, "e1", T0 + timedelta(hours=1))      # same viewing, recorded twice
    play(1, "e1", T0 + timedelta(days=3))       # a re-watch
    play(1, "e2", T0 + timedelta(days=1))
    play(1, "e3", T0 + timedelta(days=2))
    play(1, "e4", T0 + timedelta(days=2))
    play(1, "e1", T0 + timedelta(days=4), completed=False)   # abandoned: not reported
    play(1, "m1", T0, media_type="movie")                    # not anime
    # a play whose key is gone (the file was replaced): found again by title
    db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id="gone1", title="ep1",
                             series_title="Show A", season=1, episode=1, media_type="anime",
                             viewed_at=T0 + timedelta(days=6), completed=True))
    db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id="gone2", title="x",
                             series_title="Nowhere", season=1, episode=1, media_type="anime",
                             viewed_at=T0 + timedelta(days=6), completed=True))
    play(2, "e1", T0 + timedelta(days=5))


def run(fake, adapter, **kw):
    return asyncio.run(shoko_sync.sync_watched(
        transport=httpx.MockTransport(fake.handler), adapter=adapter, **kw))


def ledger():
    with conn.get_db_session() as db:
        return {(r.user_id, r.watch_history_id): (r.status, r.shoko_episode_ids, r.matched_by)
                for r in db.query(ShokoWatchSync).all()}


# ── 1. not configured: rests ─────────────────────────────────────────────────
settings.SHOKO_URL, settings.SHOKO_API_KEY = None, None
fake = FakeShoko()
res = run(fake, FakeAdapter(ITEMS))
check("without Shoko configured the sync does nothing", res.get("skipped") == "no_shoko"
      and not fake.puts and not ledger(), res)
settings.SHOKO_URL = "http://shoko.test:8111"
res = asyncio.run(shoko_sync.sync_watched(adapter=FakeAdapter(ITEMS)))
check("half configured (no key) rests too", res.get("skipped") == "no_shoko", res)
settings.SHOKO_API_KEY = KEY_OWNER
check("a test process never writes to a real Shoko", shoko_client.writes_allowed() is False)
res = asyncio.run(shoko_sync.sync_watched(adapter=FakeAdapter(ITEMS)))
check("so without an injected transport the run is skipped", res.get("skipped") == "writes_off", res)

# ── 2. plan: no writes anywhere ──────────────────────────────────────────────
res = run(fake, FakeAdapter(ITEMS), plan=True)
owner = res["users"]["Default"]
check("a plan resolves but writes neither Shoko nor the ledger",
      not fake.puts and not ledger() and owner["sent"] >= 1, res)

# ── 3. the real run ──────────────────────────────────────────────────────────
adapter = FakeAdapter(ITEMS)
res = run(fake, adapter)
owner = res["users"]["Default"]
check("the single admin key gets the Curatarr admin's plays", owner["curatarr_user"] == 1, owner)
check("eight finished anime plays read, the abandoned one and the movie left out",
      owner["plays"] == 8, owner)
check("each media-server item is fetched once per run", adapter.calls == 6, adapter.calls)
check("the series of the show libraries are listed once, only when a key is gone",
      adapter.listings == 1, adapter.listings)
ud = fake.user_data
check("e1: double row one viewing, re-watch a second, the gone key's play (found by title) a third",
      ud[(1, 101)]["PlaybackCount"] == 3
      and shoko_client.parse_time(ud[(1, 101)]["LastPlayedAt"]) == T0 + timedelta(days=6),
      ud.get((1, 101)))
check("e2 resolved by the folders above an ambiguous file name (backslashes on Shoko's side)",
      (1, 201) in ud and (1, 301) not in ud, sorted(ud))
check("e3 resolved through TMDB onto both linked Shoko episodes",
      (1, 401) in ud and (1, 402) in ud, sorted(ud))
check("the play's own date is written, not now",
      shoko_client.parse_time(ud[(1, 201)]["LastPlayedAt"]) == T0 + timedelta(days=1))
check("a PUT keeps the user's other fields (favourite, tags, rating)",
      all({"IsFavorite", "UserTags", "UserRating"} <= set(b) for _, _, b in fake.puts))
led = ledger()
statuses = sorted(s for s, _, _ in led.values())
check("ledger: 5 sent, 1 already (the double row), 2 unmatched",
      statuses == ["already", "sent", "sent", "sent", "sent", "sent", "unmatched", "unmatched"],
      statuses)
check("ledger names how each was matched",
      {h for _, _, h in led.values()} == {"path", "tmdb", "title", None}, led)
check("the done flag says nothing is left", res["done"] is True, res)

# ── 4. idempotent ────────────────────────────────────────────────────────────
n_puts = len(fake.puts)
res = run(fake, FakeAdapter(ITEMS))
check("a second run sends nothing again", len(fake.puts) == n_puts
      and res["users"]["Default"]["plays"] == 0, res)

# ── 5. an unmatched play is retried after a week ─────────────────────────────
fake.files["nowhere.mkv"] = [{"ID": 50, "Size": 7, "SeriesIDs": [{"EpisodeIDs": [{"ID": 501}]}]}]
later = datetime.now(timezone.utc).replace(tzinfo=None) + shoko_sync.UNMATCHED_RETRY + timedelta(hours=1)
async def _retry():
    async with shoko_client.new_client(httpx.MockTransport(fake.handler)) as c:
        sess = shoko_client.ShokoSession(c, KEY_OWNER, settings.SHOKO_URL)
        return await shoko_sync.sync_user(sess, FakeAdapter(ITEMS), 1, now=later)
r = asyncio.run(_retry())
check("the file Shoko imported later is reported on the retry", r["sent"] == 1
      and r["unmatched"] == 1
      and (1, 501) in fake.user_data, r)

# ── 6. several keys: each to its user ────────────────────────────────────────
settings.SHOKO_API_KEY = f"{KEY_OWNER}, {KEY_PARTNER},{KEY_STRANGER}"
res = run(fake, FakeAdapter(ITEMS))
check("the partner's key goes to the Curatarr user named in its Plex usernames",
      res["users"]["anna"]["curatarr_user"] == 2 and res["users"]["anna"]["sent"] == 1
      and (2, 101) in fake.user_data, res["users"].get("anna"))
check("a Shoko user no Curatarr user matches is skipped, not given the admin's plays",
      "error" in res["users"]["guest"], res["users"].get("guest"))
check("with several keys the admin key is matched by name only",
      "error" in res["users"]["Default"], res["users"].get("Default"))

# ── 7. a Shoko that refuses the write stops the run, ledger untouched ───────
settings.SHOKO_API_KEY = KEY_OWNER
with conn.get_db_session() as db:
    db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id="e2", title="e2",
                             media_type="anime", viewed_at=T0 + timedelta(days=9), completed=True))
fake.put_status = 405
before = ledger()
res = run(fake, FakeAdapter(ITEMS))
check("an old Shoko without the user-data route stops the run and says why",
      "needs Shoko 6" in (res["users"]["Default"].get("stopped") or "") and res["done"] is False
      and ledger() == before, res)
fake.put_status = 200

# ── 8. pure helpers ──────────────────────────────────────────────────────────
check.table("match_user", [
    ("plex username, any case", {"PlexUsernames": "X, DONDROID"}, False, 1),
    ("own username", {"Username": "anna_plex"}, False, 2),
    ("unknown non-admin", {"Username": "z"}, True, None),
    ("single admin key", {"Username": "z", "IsAdmin": True}, True, 1),
], lambda su, single: shoko_sync.match_user(
    su, [{"id": 1, "name": "DonDroid", "is_admin": True}, {"id": 2, "name": "anna_plex"}], single))
check.table("_norm_title", [
    ("multiplication sign", "High School D×D", "high school dxd"),
    ("accents and punctuation", "Pokémon: The Series!", "pokemon the series"),
], shoko_sync._norm_title)
check.table("server_major", [
    ("dev build", {"Server": {"Version": "6.0.0-dev.588"}}, 6),
    ("stable", {"Server": {"Version": "5.3.3"}}, 5),
    ("missing", {}, None),
], shoko_client.server_major)
check("Shoko times with an offset come back as naive UTC",
      shoko_client.parse_time("2026-11-05T17:20:11+01:00") == datetime(2026, 11, 5, 16, 20, 11))
now = datetime(2026, 11, 8)
check("a play older than Shoko's last one is already there",
      shoko_sync.next_user_data({"PlaybackCount": 1, "LastPlayedAt": "2026-10-05T00:00:00Z"},
                                datetime(2026, 9, 1), now) is None)
check("a first play counts one",
      shoko_sync.next_user_data({"PlaybackCount": 0}, datetime(2026, 9, 1), now)["PlaybackCount"] == 1)
check("an unreadable file match is no match",
      shoko_sync.pick_files(fake.files["01.mkv"], "/elsewhere/01.mkv", None) == [])

# ── 9. the connection test ───────────────────────────────────────────────────
r = asyncio.run(shoko_client.test_connection("http://shoko.test:8111", f"{KEY_OWNER},{KEY_PARTNER}",
                                             transport=httpx.MockTransport(fake.handler)))
check("the connection test names every key's user",
      r["ok"] and r["users"] == ["Default", "anna"] and "6.0.0" in r["version"], r)
r = asyncio.run(shoko_client.test_connection("http://shoko.test:8111", "bad",
                                             transport=httpx.MockTransport(fake.handler)))
check("a refused key fails the test", r["ok"] is False, r)

check.done()
