"""What the live test against a real Jellyfin found in the writes (2026-10-31).

After a rebind, a library that does not exist on the new server keeps its
Plex section key. Searching it sent ParentId=1 to Jellyfin: HTTP 400, the
watchlist said so instead of "not in the library", and the recommended
playlists broke off. A series whose episodes Jellyfin had not attached yet
went onto the watchlist as the series id, which Jellyfin drops without a
word, while Curatarr said ok.

    python tests/test_jellyfin_live_findings.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Its own database: the watchlist reads the library mapping, and CI has no
# data/curatarr.db to fall back on.
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import Base  # noqa: E402
Base.metadata.create_all(conn_mod.engine)

from src.services.media_server import jellyfin_writes as jw  # noqa: E402
from src.services.media_server.jellyfin_adapter import JellyfinAdapter  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


calls = []


def handler(req: httpx.Request) -> httpx.Response:
    calls.append((req.url.path, dict(req.url.params)))
    if req.url.params.get("ParentId") == "1":
        return httpx.Response(400, json={})
    return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0})


ad = JellyfinAdapter("http://jf:8096/", "key", transport=httpx.MockTransport(handler))
JF_LIB = "0c41907140d802bb58430fed7e2cd79e"
got = asyncio.run(ad.search("The Matrix", "movie", library_keys=["1"]))
check("a library still on its Plex key is not searched on Jellyfin: no request, not found",
      got is None and calls == [])
asyncio.run(ad.search("Akira", "movie", library_keys=["1", JF_LIB]))
check("...a Jellyfin library beside it still is",
      [q.get("ParentId") for _p, q in calls] == [JF_LIB])
calls.clear()
asyncio.run(ad.search("Akira", "movie"))
check("...and no libraries named means the whole server", calls and "ParentId" not in calls[0][1])


class FakeAdapter:
    def __init__(self, next_ep):
        self.next_ep, self.upserts = next_ep, []

    async def search(self, title, kind, library_keys=(), year=None):
        return "seriesid000000000000000000000001"

    async def next_episode(self, who, key):
        return self.next_ep

    async def playlist_keys(self, who, title):
        return []

    async def upsert_playlist(self, who, title, keys, media_type="Video"):
        self.upserts.append(list(keys))
        return "playlistid"


async def _who(uid):
    return SimpleNamespace(key="u1", name="admin")


saved = (jw.server_user, jw._adapter)
jw.server_user = _who
try:
    jw._adapter = lambda a=None: a
    empty = FakeAdapter(None)
    r = asyncio.run(jw.add_to_watchlist(1, "Death Note", media_type="anime", adapter=empty))
    check("a series with no episode in Jellyfin yet is not put on as the series id, and says so",
          r["ok"] is False and "no episode" in r["error"] and empty.upserts == [])
    ok = FakeAdapter("ep00000000000000000000000000003")
    r = asyncio.run(jw.add_to_watchlist(1, "Adachi and Shimamura", media_type="anime", adapter=ok))
    check("...one that has them goes on as its next unwatched episode",
          r["ok"] is True and ok.upserts == [["ep00000000000000000000000000003"]])
finally:
    jw.server_user, jw._adapter = saved

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
