"""The Wrap-Up's playlists (stage 3.5 of docs/UX_RETENTION_DESIGN.md): the
answer carries a link that plays the playlist, Jellyfin as Plex; a week's
and a month's playlist is one rolling playlist each, named in the member's
language; a tap can come before the page was ever opened for that period.

Every title here is invented.

    python tests/test_wrapup_playlists.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio
from datetime import datetime
from types import SimpleNamespace

conn = app_db()

from fastapi import HTTPException  # noqa: E402

from src.services import playlist_names as pn  # noqa: E402

check("the rolling week and month playlists have a name in each language",
      pn.name("wrapup_week", "en") == "Curatarr · Your Week" and pn.name("wrapup_week", "de") == "Curatarr · Deine Woche"
      and pn.name("wrapup_month", "de") == "Curatarr · Dein Monat"
      and pn.names("wrapup_month") == {"Curatarr · Your Month", "Curatarr · Dein Monat"})

# ── Jellyfin: the playlist's id comes back, under the rolling name ──────────

from src.services.media_server import jellyfin_writes as jw  # noqa: E402
import src.services.plex_playlists as pp  # noqa: E402


class FakeJellyfin:
    def __init__(self):
        self.upserts = []

    async def search(self, title, kind, library_keys=None, artist=None):
        return {"The Lantern Room": "a" * 32, "Starfall Archive": "b" * 32}.get(title)

    async def next_episode(self, who, key):
        return "c" * 32

    async def upsert_playlist(self, who, title, keys, media, *, aliases=()):
        self.upserts.append((title, list(keys), media, tuple(aliases)))
        return "d" * 32


async def _who(uid):
    return SimpleNamespace(key="u", name="Member")

_saved = (jw.server_user, pp._section_keys)
jw.server_user, pp._section_keys = _who, (lambda cat: [])
try:
    fake = FakeJellyfin()
    out = asyncio.run(jw.push_wrapup_playlist(
        SimpleNamespace(id=1), "Week 45, 2026",
        [{"title": "The Lantern Room", "media_type": "movie"}, {"title": "Starfall Archive", "media_type": "anime"},
         {"title": "Not Here", "media_type": "movie"}], [],
        adapter=fake, name="Curatarr · Deine Woche", aliases=("Curatarr · Your Week",)))
finally:
    jw.server_user, pp._section_keys = _saved
check("Jellyfin: the rolling name in the member's language, the other language's name renamed in place, "
      "the series as its next episode, and the playlist's id back for a link",
      fake.upserts == [("Curatarr · Deine Woche", ["a" * 32, "c" * 32], "Video", ("Curatarr · Your Week",))]
      and out == {"pushed": [{"title": "Curatarr · Deine Woche", "items": 2, "key": "d" * 32}],
                  "missing": ["Not Here"]}, (fake.upserts, out))

# ── the route: names, links, and a tap before the page was opened ───────────

import src.services.media_server as media_server  # noqa: E402
import src.services.user_language as ul  # noqa: E402
from src.database.models import User, WatchHistoryEntry  # noqa: E402
from src.routers import wrapup as wr  # noqa: E402
from src.services import server_links  # noqa: E402

with conn.get_db_session() as db:
    u = User(plex_user_id="pl-1", plex_username="Member", timezone="Europe/Berlin")
    db.add(u)
    db.commit()
    UID = u.id
    for i in range(6):
        db.add(WatchHistoryEntry(user_id=UID, plex_user_id="pl-1", plex_item_id=f"k{i}", title="The Lantern Room",
                                 media_type="movie", duration_ms=3_600_000, view_offset_ms=3_600_000,
                                 completed=True, viewed_at=datetime(2026, 11, 3, 20, 0)))
    db.commit()

pushed = []


async def _push(user, label, videos, tracks, name=None, aliases=()):
    pushed.append((label, name, aliases, [v["title"] for v in videos]))
    return {"pushed": [{"title": name or "Curatarr Wrap-Up · " + label, "items": 1, "key": "777"}],
            "missing": []}


async def _mid(force=False):
    return "MACHINE"


async def _no_posters(facts):
    facts.setdefault("posters", {})

saved = (pp.push_wrapup_playlist, pp.get_machine_identifier, media_server.active_kind, wr.attach_posters,
         ul.user_language)
pp.push_wrapup_playlist, pp.get_machine_identifier = _push, _mid
media_server.active_kind = lambda settings=None: "plex"
wr.attach_posters = _no_posters
ul.user_language = lambda uid: "de"
server_links._miss["until"] = 0.0
user = SimpleNamespace(id=UID, plex_username="Member", is_admin=False)
try:
    week = asyncio.run(wr.push_playlist({"scope": "all", "period": "week:2026-W45"}, user=user))
    asyncio.run(wr.push_playlist({"scope": "all", "period": "30d"}, user=user))
finally:
    (pp.push_wrapup_playlist, pp.get_machine_identifier, media_server.active_kind, wr.attach_posters,
     ul.user_language) = saved
check("a week's playlist from a tap, before the page was opened: the rolling name in the member's "
      "language, the other names replaced with it",
      pushed[0] == ("Week 45, 2026", "Curatarr · Deine Woche", ("Curatarr · Your Week",), ["The Lantern Room"]),
      pushed)
check("the answer plays it: a link to the playlist on the member's server, no key",
      week["pushed"] == [{"title": "Curatarr · Deine Woche", "items": 1,
                          "link": server_links.PLEX_PLAYLIST.format(mid="MACHINE", key="777")}], week)
check("other periods keep the Wrap-Up's own name", pushed[1][1] is None and pushed[1][0] == "Last 30 days", pushed)


async def _no_access(user, label, videos, tracks, name=None, aliases=()):
    return {"skipped": "no_server_access"}

pp.push_wrapup_playlist = _no_access
media_server.active_kind = lambda settings=None: "plex"
wr.attach_posters = _no_posters
try:
    asyncio.run(wr.push_playlist({"scope": "all", "period": "week:2026-W45"}, user=user))
    check("no write access on the server is said, not 'not found'", False)
except HTTPException as e:
    check("no write access on the server is said, not 'not found'", e.status_code == 409 and "write" in e.detail,
          e.detail)
finally:
    (pp.push_wrapup_playlist, pp.get_machine_identifier, media_server.active_kind, wr.attach_posters,
     ul.user_language) = saved

check.done()
