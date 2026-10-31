"""Watch history from Jellyfin (docs/JELLYFIN_DESIGN.md §4, phase 3).

- tier A: a live session that ends past 80 % is a play, with its device; a
  restart below 15 % after 80 % is a second run; a pause over four hours ends
  it; under 80 %, or without a runtime, nothing is invented;
- writing: plays become watch_history rows shaped like the Plex sync's
  (category from the library mapping, series / season / episode, album,
  series-level TMDB id, MediaIdentity under the series key); plays of
  strangers, unmapped or ignored libraries are skipped;
- merging: two reports of one play less than 20 minutes apart are one row,
  the richer witness wins (hook > session > report > poll > marked); plays
  from other sources are never merged; further apart they stay two;
- tier C: the first poll files one "marked" row per played item (none for
  an item with a finished play on record: after a server switch that is
  the carried-over history); later a count up by n is n plays (the extra
  ones date-only), a re-mark without a count change is one, nothing changed
  is nothing; the switch's reset_poll makes the next poll a first one;
- tier D: the Playback Reporting rows of this user past 80 %, once; without
  the plugin that is recorded, not retried;
- tier B: the webhook needs its secret, takes JSON objects of a capped size,
  writes only what the server confirms as freshly played;
- sync_history: all members, the follow-up flag for the taste work, one at a
  time; the scheduler, startup and the Sync button dispatch to it.

    python tests/test_jellyfin_history.py
"""
import asyncio
import copy
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import (Base, LibraryConfig, MediaIdentity, ServerPlayState, User,  # noqa: E402
                                 WatchHistoryEntry as W)
Base.metadata.create_all(conn_mod.engine)

from media_server_fixtures import jellyfin_transport, load  # noqa: E402
from src.config import settings  # noqa: E402
from src.services.app_state import get_state, set_state  # noqa: E402
from src.services.media_server import jellyfin_history as jh, jellyfin_hook as hook  # noqa: E402
from src.services.media_server.base import LiveSession, Play  # noqa: E402
from src.services.media_server.jellyfin_adapter import JellyfinAdapter  # noqa: E402

from _checks import check  # noqa: E402


JANE, SAM = "b0c1d2e3f405162738495a6b7c8d9e0f", "c1d2e3f405162738495a6b7c8d9e0fa1"
HEAT, SECRETS, DOUBLE = "4b1d2f3e5a6c7b8d9e0f1a2b3c4d5e6f", "2a3b4c5d6e7f80910a1b2c3d4e5f6071", "3b4c5d6e7f8091a20b1c2d3e4f506172"
HUNTER, JOGA = "8f90a1b2c3d4e5f60718293a4b5c6d7e", "90a1b2c3d4e5f60718293a4b5c6d7e8f"
LIB_MOV, LIB_TV, LIB_MUS = "f137a2dd21bbc1b99aa5c0f6bf02a805", "a656b907eb3a73532e40e44b968d0225", "7e64e319657a9516ec78490da03edccb"
T0 = datetime(2026, 10, 26, 20, 0)

with conn_mod.get_db_session() as db:
    db.add_all([User(id=1, plex_user_id=JANE, plex_username="jane", is_admin=True),
                User(id=2, plex_user_id=SAM, plex_username="Sam")])
    db.add_all([LibraryConfig(plex_section_key=LIB_MOV, plex_section_title="Movies", plex_section_type="movie", media_category="movie"),
                LibraryConfig(plex_section_key=LIB_TV, plex_section_title="Shows", plex_section_type="show", media_category="show"),
                LibraryConfig(plex_section_key=LIB_MUS, plex_section_title="Music", plex_section_type="artist", media_category="music")])
    db.commit()


def adapter(**kw):
    return JellyfinAdapter("http://jf:8096", "jf-key", transport=jellyfin_transport(**kw))


def rows(**flt):
    with conn_mod.get_db_session() as db:
        q = db.query(W)
        for k, v in flt.items():
            q = q.filter(getattr(W, k) == v)
        return [SimpleNamespace(**{c.name: getattr(r, c.name) for c in W.__table__.columns})
                for r in q.order_by(W.viewed_at, W.id).all()]


def wipe():
    with conn_mod.get_db_session() as db:
        db.query(W).delete()
        db.query(ServerPlayState).delete()
        db.query(MediaIdentity).delete()
        db.commit()


def sess(pos_s, item=HEAT, dur_s=10000, paused=False, sk="s1"):
    return LiveSession(session_key=sk, user_key=JANE, item_key=item, position_ms=pos_s * 1000,
                       duration_ms=dur_s * 1000 if dur_s else None, paused=paused,
                       device="Pixel 8", client="Jellyfin Android")


print("\n-- tier A: live sessions --")
tr = jh.SessionTracker()
check("a session in progress is no play yet", tr.observe([sess(5000)], T0) == [])
tr.observe([sess(8500)], T0 + timedelta(minutes=1))
done = tr.observe([], T0 + timedelta(minutes=2))
check("ending past 80 % is one play, with device, client, the furthest position, not finished (< 90 %)",
      len(done) == 1 and done[0].position_ms == 8_500_000 and done[0].device == "Pixel 8"
      and done[0].client == "Jellyfin Android" and done[0].completed is False
      and done[0].played_at == T0 + timedelta(minutes=2) and done[0].source == "jellyfin_session")
tr = jh.SessionTracker()
tr.observe([sess(7000)], T0)
check("ending below 80 % is no play", tr.observe([], T0 + timedelta(minutes=1)) == [])
tr = jh.SessionTracker()
tr.observe([sess(9500)], T0)
rew = tr.observe([sess(600)], T0 + timedelta(minutes=1))
check("a restart below 15 % after 80 % writes the first run (finished: 95 %)",
      len(rew) == 1 and rew[0].completed is True)
tr.observe([sess(9000)], T0 + timedelta(minutes=50))
check("...and the second run counts on its own", len(tr.observe([], T0 + timedelta(minutes=51))) == 1)
tr = jh.SessionTracker()
tr.observe([sess(8800, paused=True)], T0)
check("a pause under four hours keeps the session open",
      tr.observe([sess(8800, paused=True)], T0 + timedelta(hours=3)) == [])
check("a pause over four hours ends it as a play",
      len(tr.observe([sess(8800, paused=True)], T0 + timedelta(hours=4, minutes=1))) == 1 and not tr.pairs)
tr = jh.SessionTracker()
tr.observe([sess(9000, dur_s=None)], T0)
check("without a runtime nothing is invented", tr.observe([], T0 + timedelta(minutes=1)) == [])
tr = jh.SessionTracker()
tr.observe([sess(9000)], T0)
switched = tr.observe([sess(10, item=SECRETS, dur_s=3000)], T0 + timedelta(minutes=1))
check("the next item in the same session ends the previous one",
      len(switched) == 1 and switched[0].item_key == HEAT and len(tr.pairs) == 1)

print("\n-- writing plays --")
a = adapter()
res = asyncio.run(jh.record_plays([
    Play(user_key=JANE, item_key=HEAT, played_at=T0, source="jellyfin_session", position_ms=9_500_000,
         duration_ms=10_200_000, device="Pixel 8", client="Jellyfin Android"),
    Play(user_key=JANE, item_key=DOUBLE, played_at=T0 + timedelta(hours=1), source="jellyfin_session",
         position_ms=2_900_000),
    Play(user_key=JANE, item_key=JOGA, played_at=T0 + timedelta(hours=2), source="jellyfin_poll"),
    Play(user_key="0" * 32, item_key=HEAT, played_at=T0, source="jellyfin_poll"),
], a))
check("three plays written, the stranger's skipped", res == {"inserted": 3, "merged": 0, "skipped": 1})
heat = rows(plex_item_id=HEAT)[0]
check("a film: category from the library mapping, TMDB id, genres, device and client, finished (93 %)",
      heat.media_type == "movie" and heat.tmdb_id == 949 and heat.genres == "Crime,Thriller"
      and heat.player == "Pixel 8" and heat.platform == "Jellyfin Android" and heat.completed is True
      and heat.user_id == 1 and heat.plex_user_id == JANE and heat.source == "jellyfin_session"
      and heat.duration_ms == 10_200_000)
ep = rows(plex_item_id=DOUBLE)[0]
check("an episode: series title, season and episode, the SERIES' TMDB id, finished (97 %)",
      ep.media_type == "show" and ep.series_title == "Dark" and (ep.season, ep.episode) == (1, 2)
      and ep.tmdb_id == 70523 and ep.completed is True)
joga = rows(plex_item_id=JOGA)[0]
check("a track: artist as series title, the album, a poll play counts as finished",
      joga.media_type == "music" and joga.series_title == "Björk" and joga.album == "Homogenic"
      and joga.completed is True)
with conn_mod.get_db_session() as db:
    ident = {m.plex_rating_key: (m.tvdb_id, m.media_type) for m in db.query(MediaIdentity).all()}
check("identities are filed, an episode's under its series key", ident.get("9a8b7c6d5e4f30211203f4e5d6c7b8a9") == (334824, "show")
      and DOUBLE not in ident)
with conn_mod.get_db_session() as db:
    db.query(LibraryConfig).filter(LibraryConfig.plex_section_key == LIB_MUS).update({"media_category": "ignore"})
    db.commit()
ign = asyncio.run(jh.record_plays([Play(user_key=JANE, item_key=HUNTER, played_at=T0, source="jellyfin_poll")], a))
check("an ignored library's plays are skipped", ign["skipped"] == 1 and not rows(plex_item_id=HUNTER))
with conn_mod.get_db_session() as db:
    db.query(LibraryConfig).filter(LibraryConfig.plex_section_key == LIB_MUS).update({"media_category": "music"})
    db.commit()

print("\n-- one play, many witnesses --")
m = asyncio.run(jh.record_plays([Play(user_key=JANE, item_key=HEAT, played_at=T0 + timedelta(minutes=12),
                                      source="jellyfin_poll")], a))
h = rows(plex_item_id=HEAT)
check("a poll 12 minutes after the session's play merges; the session's time and device stay",
      m["merged"] == 1 and len(h) == 1 and h[0].viewed_at == T0 and h[0].player == "Pixel 8"
      and h[0].source == "jellyfin_session" and h[0].completed is True)
asyncio.run(jh.record_plays([Play(user_key=JANE, item_key=HEAT, played_at=T0 + timedelta(minutes=3),
                                  source="jellyfin_hook", client="Jellyfin Web")], a))
h = rows(plex_item_id=HEAT)
check("a hook report wins: its time and client, the device it lacked stays",
      len(h) == 1 and h[0].source == "jellyfin_hook" and h[0].viewed_at == T0 + timedelta(minutes=3)
      and h[0].platform == "Jellyfin Web" and h[0].player == "Pixel 8")
asyncio.run(jh.record_plays([Play(user_key=JANE, item_key=HEAT, played_at=T0 + timedelta(hours=3),
                                  source="jellyfin_poll")], a))
check("three hours later it is a second play", len(rows(plex_item_id=HEAT)) == 2)
with conn_mod.get_db_session() as db:
    db.add(W(user_id=1, plex_user_id=JANE, plex_item_id=SECRETS, title="Secrets", media_type="show",
             viewed_at=T0, source="netflix"))
    db.commit()
asyncio.run(jh.record_plays([Play(user_key=JANE, item_key=SECRETS, played_at=T0 + timedelta(minutes=5),
                                  source="jellyfin_poll")], a))
check("a play from another source (Netflix) is never merged into", len(rows(plex_item_id=SECRETS)) == 2)

print("\n-- tier C: the delta poll --")
wipe()
played = copy.deepcopy(load("jellyfin", f"Items_played_{JANE}.json")["Items"])
first = asyncio.run(jh.poll_user(adapter(played={JANE: played}), 1, JANE, now=T0))
r = rows(user_id=1)
check("the first poll files one 'marked' row per played item, at its last-played date",
      first["first"] is True and first["inserted"] == 3 and {x.source for x in r} == {"marked"}
      and {x.viewed_at for x in r} == {datetime(2026, 10, 25, 7, 40), datetime(2026, 10, 20, 21, 0),
                                       datetime(2026, 10, 18, 22, 10)})
check("...and seeds the snapshot", asyncio.run(asyncio.to_thread(jh._load_snapshot, 1))[HEAT] == (2, datetime(2026, 10, 20, 21, 0)))
same = asyncio.run(jh.poll_user(adapter(played={JANE: played}), 1, JANE, now=T0 + timedelta(hours=2)))
check("nothing changed, nothing written", same["inserted"] == 0 and same["merged"] == 0)
played[1]["UserData"] = {**played[1]["UserData"], "PlayCount": 4, "LastPlayedDate": "2026-10-26T23:30:00.0000000Z"}
played[0]["UserData"] = {**played[0]["UserData"], "LastPlayedDate": "2026-10-26T23:50:00.0000000Z"}
moved = asyncio.run(jh.poll_user(adapter(played={JANE: played}), 1, JANE, now=T0 + timedelta(hours=12)))
heat_rows = rows(plex_item_id=HEAT, source="jellyfin_poll")
check("a count up by two is two plays: one at the last-played date, one date-only at the last poll",
      len(heat_rows) == 2 and heat_rows[1].viewed_at == datetime(2026, 10, 26, 23, 30)
      and heat_rows[0].date_only is True and heat_rows[0].viewed_at == T0 + timedelta(hours=2))
check("a re-mark without a count change is one play", len(rows(plex_item_id=HUNTER, source="jellyfin_poll")) == 1
      and moved["inserted"] == 3)

# After a media server switch: the switch reset the poll, and marked as
# played on Jellyfin what Curatarr already had from Plex. The first poll
# finds those played - nothing new, no twin "marked" rows.
wipe()
with conn_mod.get_db_session() as db:
    db.add(W(user_id=1, plex_user_id=JANE, plex_item_id=HEAT, title="Heat", media_type="movie",
             viewed_at=datetime(2026, 9, 1, 20, 0), source=None, completed=True))
    db.commit()
asyncio.run(asyncio.to_thread(jh.reset_poll, [1]))
check("the switch's reset makes the next poll a first one again",
      get_state(jh._POLL_SEEDED.format(uid=1)) is None and not asyncio.run(asyncio.to_thread(jh._load_snapshot, 1)))
again = asyncio.run(jh.poll_user(adapter(played={JANE: played}), 1, JANE, now=T0 + timedelta(days=1)))
check("...whose 'played' for a title with a finished play on record adds no twin row",
      again["first"] is True and len(rows(plex_item_id=HEAT)) == 1
      and {x.plex_item_id for x in rows(user_id=1, source="marked")} == {HUNTER, played[2]["Id"].replace("-", "")} - {HEAT})

# The switch's own marks, seen late: a server still scanning lists a marked
# item as played only after the first poll - then it must not be a play.
import calendar  # noqa: E402
import json  # noqa: E402
wipe()
mark_at = datetime(2026, 10, 31, 12, 52, 30)
epoch = calendar.timegm(mark_at.timetuple())
asyncio.run(asyncio.to_thread(set_state, jh.SWITCH_MARKS, json.dumps(
    {"from": epoch - 20, "to": epoch + 20, "keys": [HUNTER]})))
late = asyncio.run(jh.record_plays([
    Play(user_key=JANE, item_key=HUNTER, played_at=mark_at, source="jellyfin_poll", completed=True),
    Play(user_key=JANE, item_key=HUNTER, played_at=mark_at + timedelta(days=2), source="jellyfin_poll",
         completed=True),
    Play(user_key=JANE, item_key=HEAT, played_at=mark_at, source="jellyfin_poll", completed=True)], a))
check("a played mark the switch set is no play when a later poll finds it; a real play of it is, "
      "and so is another title at the same moment",
      late["inserted"] == 2 and {(x.plex_item_id, x.viewed_at) for x in rows(user_id=1)}
      == {(HUNTER, mark_at + timedelta(days=2)), (HEAT, mark_at)})
asyncio.run(asyncio.to_thread(set_state, jh.SWITCH_MARKS, ""))

print("\n-- tier D: Playback Reporting --")
wipe()
asyncio.run(asyncio.to_thread(set_state, "jellyfin_report_done:1", ""))
rep = asyncio.run(jh.import_report(adapter(), 1, JANE))
r = rows(user_id=1)
check("jane's plays past 80 % come in (Heat 9,900 s of 10,200); a 5-minute start does not; Sam's are not hers",
      rep["inserted"] == 1 and rep["skipped"] == 1 and len(r) == 1 and r[0].plex_item_id == HEAT
      and r[0].viewed_at == datetime(2025, 12, 31, 22, 5, 11, 123456) and r[0].player == "Firefox"
      and r[0].source == "jellyfin_report")
check("...once", asyncio.run(jh.import_report(adapter(), 1, JANE)) == {"skipped": "done"})
nop = asyncio.run(jh.import_report(adapter(report=False), 2, SAM))
check("without the plugin: recorded as absent, not an error",
      nop == {"plugin": False} and asyncio.run(asyncio.to_thread(get_state, "jellyfin_report_done:2")) == "absent")

print("\n-- tier B: the webhook --")
wipe()
fresh = datetime(2026, 10, 18, 22, 10) + timedelta(minutes=10)
payload = {"NotificationType": "PlaybackStop", "UserId": JANE.upper(), "ItemId": SECRETS,
           "DeviceName": "Living Room TV", "ClientName": "Jellyfin Android TV", "PlaybackPositionTicks": "29000000000"}
got = asyncio.run(hook.handle(payload, adapter=adapter(), now=fresh))
r = rows(plex_item_id=SECRETS)
check("a fresh, confirmed play is written at the server's last-played time, with device and client",
      got["inserted"] == 1 and len(r) == 1 and r[0].source == "jellyfin_hook"
      and r[0].viewed_at == datetime(2026, 10, 18, 22, 10) and r[0].player == "Living Room TV")
check("a replay of an old post is ignored",
      asyncio.run(hook.handle(payload, adapter=adapter(), now=fresh + timedelta(days=2))) == {"ignored": "stale"})
check("an item the server does not call played is ignored",
      asyncio.run(hook.handle({**payload, "ItemId": DOUBLE}, adapter=adapter(), now=fresh)) == {"ignored": "not played"})
check("other events and broken ids are ignored",
      asyncio.run(hook.handle({**payload, "NotificationType": "ItemAdded"}, adapter=adapter())) == {"ignored": "event"}
      and asyncio.run(hook.handle({**payload, "UserId": "x"}, adapter=adapter())) == {"ignored": "ids"})

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from src.routers import jellyfin as jr  # noqa: E402
from src.routers.auth import get_current_user, require_admin  # noqa: E402

app = FastAPI()
app.include_router(jr.router, prefix="/api/jellyfin")
app.dependency_overrides[require_admin] = lambda: SimpleNamespace(id=1, is_admin=True)
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, is_admin=True)
c = TestClient(app)
settings.MEDIA_SERVER_TYPE = None
check("on a Plex install the routes are gone", c.post("/api/jellyfin/webhook", json={}).status_code == 404)
settings.MEDIA_SERVER_TYPE = "jellyfin"
check("no secret yet: the hook is off", c.post("/api/jellyfin/webhook", json=payload).status_code == 401)
setup = c.get("/api/jellyfin/webhook/setup").json()
check("the setup card creates the secret and names header, events and template",
      len(setup["secret"]) >= 32 and setup["header"] == "X-Curatarr-Secret"
      and setup["events"] == ["PlaybackStop", "UserDataSaved"] and "{{ItemId}}" in setup["template"])
check("a wrong secret is refused",
      c.post("/api/jellyfin/webhook", json=payload, headers={"X-Curatarr-Secret": "nope"}).status_code == 401)
hdr = {"X-Curatarr-Secret": setup["secret"]}
check("a body over 64 KB is refused",
      c.post("/api/jellyfin/webhook", content=b"{" + b" " * 70000 + b"}", headers=hdr).status_code == 413)
check("a body that is not a JSON object is refused",
      c.post("/api/jellyfin/webhook", content=b"[1,2]", headers={**hdr, "Content-Type": "application/json"}).status_code == 400)
jr.hook.handle = lambda p: asyncio.sleep(0, result={"ignored": "event"})
check("with the secret the post is handled",
      c.post("/api/jellyfin/webhook", json={"NotificationType": "ItemAdded"}, headers=hdr).json() == {"ignored": "event"})
old = setup["secret"]
new = c.post("/api/jellyfin/webhook/rotate").json()["secret"]
check("rotating replaces the secret", new != old
      and c.post("/api/jellyfin/webhook", json={}, headers={"X-Curatarr-Secret": old}).status_code == 401)
src_badge = c.get("/api/jellyfin/history-sources").json()
check("the badge: live sessions always, the webhook seen this week, Playback Reporting found",
      src_badge["live_sessions"] is True and src_badge["webhook"] is True and src_badge["playback_reporting"] is True)

print("\n-- sync_history and its callers --")
wipe()
for k in ("jellyfin_report_done:1", "jellyfin_report_done:2", "jellyfin_poll_seeded:1", "jellyfin_poll_seeded:2",
          "jellyfin_last_poll:1", "jellyfin_last_poll:2", "plex_followup_pending"):
    asyncio.run(asyncio.to_thread(set_state, k, ""))
with conn_mod.get_db_session() as db:
    db.query(User).filter(User.id == 2).update({"is_active": True})
    db.commit()
out = asyncio.run(jh.sync_history(adapter=adapter()))
check("every member: backfill + first poll; new rows counted; the taste follow-up is flagged",
      out["synced"] >= 3 and set(out["users"]) == {1, 2}
      and bool(asyncio.run(asyncio.to_thread(get_state, "plex_followup_pending"))))


async def _two():
    return await asyncio.gather(jh.sync_history(adapter=adapter()), jh.sync_history(adapter=adapter()))
both = asyncio.run(_two())
check("one sync at a time", sum(1 for x in both if x.get("busy")) == 1)
srcs = {p: (ROOT / p).read_text(encoding="utf-8") for p in
        ("src/services/scheduler.py", "src/main.py", "src/routers/history.py")}
check("the scheduler, startup and the Sync button dispatch to Jellyfin",
      "sync_history(followup=False)" in srcs["src/services/scheduler.py"]
      and srcs["src/main.py"].count("jellyfin_history import sync_history") == 1
      and "jellyfin_history import sync_history" in srcs["src/routers/history.py"])
check("the session poller starts with a configured Jellyfin install",
      "start_session_poller()" in srcs["src/main.py"])
settings.MEDIA_SERVER_TYPE = None

check.done()
