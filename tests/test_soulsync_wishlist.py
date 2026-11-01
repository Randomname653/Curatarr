"""A wish onto SoulSync's wishlist (owner decision 2026-11-01).

Without Lidarr the Spotify backlog's "Wish" only filed a wish the owner
fulfilled by hand in SoulSync. The owner's wish now puts the tracks the
wisher streamed by that artist on SoulSync's wishlist (POST
/api/v1/wishlist, track by track), which SoulSync's Auto-Process Wishlist
fetches. Still never /api/v1/request; a banned artist is refused by
SoulSync's own gate (409).

    python tests/test_soulsync_wishlist.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio
import inspect
import json
from datetime import datetime, timedelta

import httpx

conn = app_db()
from fastapi import HTTPException  # noqa: E402
from src.database.models import MusicWish, User, WatchHistoryEntry  # noqa: E402
import src.routers.library as lib  # noqa: E402
from src.routers.auth import require_admin  # noqa: E402
import src.services.soulsync_client as ss  # noqa: E402
from src.config import settings  # noqa: E402

settings.SOULSYNC_URL = "http://soulsync.test:12279"
settings.SOULSYNC_API_KEY = "sk_test"

# ── one track ───────────────────────────────────────────────────────────────
check.table("a streamed track becomes a wishlist entry keyed by its Spotify id", [
    ("a track", ("spotify:track:4uLU6hMCjMI75M1A2tKUQC", "Song", "Band", "Album"),
     {"id": "4uLU6hMCjMI75M1A2tKUQC", "name": "Song", "artists": [{"name": "Band"}],
      "uri": "spotify:track:4uLU6hMCjMI75M1A2tKUQC", "provider": "spotify", "album": {"name": "Album"}}),
    ("no album: no album field", ("spotify:track:abc", "Song", "Band", None),
     {"id": "abc", "name": "Song", "artists": [{"name": "Band"}], "uri": "spotify:track:abc",
      "provider": "spotify"}),
    ("an episode is no track", ("spotify:episode:abc", "Talk", "Pod", None), None),
    ("no uri", ("", "Song", "Band", None), None),
], lambda args: ss.spotify_track_data(*args))

# ── the batch ───────────────────────────────────────────────────────────────
seen: list = []


def soulsync(answers):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content or b"{}")
        seen.append((req.method, req.url.path, body.get("track_data", {}).get("id")))
        return httpx.Response(answers.get(body.get("track_data", {}).get("id"), 201), json={})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


T = [ss.spotify_track_data(f"spotify:track:t{i}", f"Song {i}", "Band") for i in range(5)]
seen.clear()
r = asyncio.run(ss.wishlist_tracks(T, client=soulsync({"t1": 200, "t2": 409, "t3": 400})))
check("each track is one POST to /api/v1/wishlist, never /api/v1/request",
      [(m, p) for m, p, _ in seen] == [("POST", "/api/v1/wishlist")] * 5)
check("...counted by what SoulSync answered: new, already there, refused, rejected",
      (r["ok"], r["sent"], r["added"], r["already"], r["refused"], r["rejected"]) == (True, 5, 2, 1, 1, 1))
seen.clear()
r = asyncio.run(ss.wishlist_tracks(T, client=soulsync({"t0": 403})))
check("a read-only API key ends the batch at the first track and says why",
      not r["ok"] and len(seen) == 1 and "admin key" in r["error"])
check("no tracks: nothing is sent",
      asyncio.run(ss.wishlist_tracks([None]))["error"] == "no Spotify tracks to send")
check("a test process never writes to the real SoulSync",
      asyncio.run(ss.wishlist_tracks(T)).get("skipped") is True)

# ── the route ───────────────────────────────────────────────────────────────
NOW = datetime.utcnow()
with conn.get_db_session() as db:
    db.add_all([User(id=1, plex_user_id="1", plex_username="owner", is_admin=True),
                User(id=2, plex_user_id="2", plex_username="sam")])
    db.commit()
with conn.get_db_session() as db:
    plays = []

    def play(uid, uri, title, artist="Band", album="Album", owned=False, days=1):
        plays.append(WatchHistoryEntry(
            user_id=uid, plex_user_id=str(uid), media_type="music", source="spotify",
            plex_item_id="12345" if owned else uri[:64], spotify_uri=uri, series_title=artist,
            title=title, album=album, viewed_at=NOW - timedelta(days=days)))

    for d in range(3):
        play(2, "spotify:track:hit", "Hit", days=d + 1)      # played three times
    play(2, "spotify:track:deep", "Deep Cut")
    play(2, "spotify:track:owned", "Owned Song", owned=True)
    play(2, "spotify:track:other", "Other", artist="Someone Else")
    play(1, "spotify:track:ownerplay", "Owner's own play")
    db.add_all(plays)
    db.add(MusicWish(id=7, user_id=2, title="band", source="backlog"))
    db.commit()

check("the route is the owner's alone: a member's wish waits for the owner's click",
      inspect.signature(lib.send_wish_to_soulsync).parameters["user"].default.dependency is require_admin)

sent: list = []


async def _capture(tracks, *, client=None):
    sent.append(tracks)
    return {"ok": True, "sent": len(tracks), "added": len(tracks), "already": 0, "refused": 0,
            "rejected": 0, "error": None}

saved = (ss._configured, ss.wishlist_tracks)
ss._configured, ss.wishlist_tracks = (lambda: True), _capture
try:
    res = asyncio.run(lib.send_wish_to_soulsync(7, User(id=1, is_admin=True)))
    ids = [t["id"] for t in sent[-1]]
    check("the wisher's streamed tracks by that artist go, most played first",
          ids == ["hit", "deep"], ids)
    check("...not one the music server already has, another artist's, or someone else's play",
          not {"owned", "other", "ownerplay"} & set(ids))
    with conn.get_db_session() as db:
        note = json.loads(db.get(MusicWish, 7).soulsync or "{}")
    check("the wish remembers what SoulSync did", note.get("added") == 2 and note.get("at"))
    with conn.get_db_session() as db:
        names = {w["title"]: w for w in lib.list_wishes(User(id=1, is_admin=True))["wishes"]}
    check("...and the Wanted list shows it", names["band"]["soulsync"]["sent"] == 2)
    try:
        asyncio.run(lib.send_wish_to_soulsync(99, User(id=1, is_admin=True)))
        status = 200
    except HTTPException as e:
        status = e.status_code
    check("an unknown wish is a 404", status == 404)
    ss._configured = lambda: False
    try:
        asyncio.run(lib.send_wish_to_soulsync(7, User(id=1, is_admin=True)))
        status = 200
    except HTTPException as e:
        status = e.status_code
    check("SoulSync not set up: a 409 that says so, nothing sent", status == 409 and len(sent) == 1)
finally:
    ss._configured, ss.wishlist_tracks = saved

check.done()
