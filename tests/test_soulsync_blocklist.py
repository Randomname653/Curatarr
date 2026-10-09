"""SoulSync: the watchlist, the Liked Songs on its wishlist, and the ban.

2026-10-09: SoulSync's playlist sync compares against Plex, so a liked song
of an artist Curatarr deleted lands on its wishlist at the next sync and
downloads again. The owner agreed to one write: a deleted artist goes on
SoulSync's blocklist (a web-UI route, not the documented v1 API; the API
key is accepted there). The ban is read back, never touches the download
trigger, and never runs from a test process against the real SoulSync.

    python tests/test_soulsync_blocklist.py
"""
import asyncio
import pathlib
import sys

import httpx

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.config import settings  # noqa: E402
from src.services import soulsync_client as ss  # noqa: E402

_REAL = (settings.SOULSYNC_URL, settings.SOULSYNC_API_KEY)


def _fake_soulsync():
    settings.SOULSYNC_URL, settings.SOULSYNC_API_KEY = "http://soulsync.test", "sk_test"


def _restore():
    settings.SOULSYNC_URL, settings.SOULSYNC_API_KEY = _REAL


class _Server:
    """SoulSync's blocklist routes, in memory; records every request."""

    def __init__(self, entries=None, post_status=200, keep_post=True):
        self.entries = list(entries or [])
        self.post_status, self.keep_post = post_status, keep_post
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path, request.content))
        assert request.headers.get("authorization") == "Bearer sk_test"
        if request.url.path != "/api/blocklist":
            return httpx.Response(404)
        if request.method == "GET":
            assert request.url.params.get("entity_type") == "artist"
            return httpx.Response(200, json={"success": True, "entries": self.entries})
        if self.post_status != 200:
            return httpx.Response(self.post_status, json={"error": "nope"})
        import json
        body = json.loads(request.content)
        if self.keep_post:
            self.entries.append({"id": 40 + len(self.entries), "name": body["name"],
                                 "entity_type": "artist"})
        return httpx.Response(200, json={"success": True, "id": 40})

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


def _block(server, name, mbid=None):
    async def run():
        async with server.client() as c:
            return await ss.block_artist(name, mbid, client=c)
    return asyncio.run(run())


def test_a_new_ban_is_posted_and_read_back():
    _fake_soulsync()
    try:
        srv = _Server()
        out = _block(srv, "Bishop Briggs", "mbid-1")
        assert out == {"ok": True, "id": 40, "error": None}, out
        methods = [m for m, _p, _b in srv.calls]
        assert methods == ["GET", "POST", "GET"], methods
        import json
        body = json.loads(srv.calls[1][2])
        assert body == {"entity_type": "artist", "name": "Bishop Briggs",
                        "source": "musicbrainz", "source_id": "mbid-1"}, body
        assert all(p == "/api/blocklist" for _m, p, _b in srv.calls), "only the blocklist route"
    finally:
        _restore()


def test_a_name_alone_is_enough():
    _fake_soulsync()
    try:
        srv = _Server()
        assert _block(srv, "Mwk")["ok"]
        import json
        assert json.loads(srv.calls[1][2]) == {"entity_type": "artist", "name": "Mwk"}
    finally:
        _restore()


def test_an_artist_already_banned_is_not_posted_twice():
    _fake_soulsync()
    try:
        srv = _Server(entries=[{"id": 7, "name": "olivia newton-john"}])
        out = _block(srv, "Olivia Newton‐John")          # U+2010, as Lidarr spells it
        assert out["ok"] and out["id"] == 7, out
        assert [m for m, _p, _b in srv.calls] == ["GET"]
    finally:
        _restore()


def test_a_refused_or_missing_ban_is_reported():
    _fake_soulsync()
    try:
        out = _block(_Server(post_status=401), "Third Day")
        assert not out["ok"] and "HTTP 401" in out["error"], out
        out = _block(_Server(keep_post=False), "Third Day")
        assert not out["ok"] and "did not show up" in out["error"], out
    finally:
        _restore()


def test_an_unreachable_soulsync_is_reported():
    _fake_soulsync()

    def down(request):
        raise httpx.ConnectError("refused")
    try:
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as c:
                return await ss.block_artist("Nate Ruess", client=c)
        out = asyncio.run(run())
        assert not out["ok"] and "could not be reached" in out["error"], out
    finally:
        _restore()


def test_a_test_process_never_writes_to_the_real_soulsync():
    _fake_soulsync()
    try:
        assert not ss.writes_allowed(), "this file runs from tests/"
        out = asyncio.run(ss.block_artist("Bishop Briggs"))
        assert out.get("skipped") and not out["ok"], out
    finally:
        _restore()


def test_names_match_across_accents_dashes_and_case():
    assert ss.norm_name("Beyoncé") == ss.norm_name("BEYONCE") == "beyonce"
    assert ss.norm_name("Olivia Newton‐John") == ss.norm_name("olivia newton-john")


def test_only_liked_songs_count_from_the_wishlist():
    _fake_soulsync()
    pages = {
        "1": {"data": {"tracks": [
            {"artist_name": "Hozier", "source_type": "playlist", "source_info": {"playlist_name": "Liked Songs"}},
            {"artist_name": "Retry", "source_type": "playlist", "source_info": {"playlist_name": "Wishlist (Auto - Albums)"}},
            {"artist_name": "Mix", "source_type": "playlist", "source_info": {"playlist_name": "Time Machine — 1980s"}},
        ]}, "pagination": {"has_next": True}},
        "2": {"data": {"tracks": [
            {"artist_name": "Album Req", "source_type": "album", "source_info": {}},
            {"artist_name": "Aurora", "source_type": "playlist", "source_info": {"playlist_name": "Liked Songs"}},
        ]}, "pagination": {"has_next": False}},
    }
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=pages[request.url.params["page"]])
    real = httpx.AsyncClient
    httpx.AsyncClient = lambda *a, **kw: real(*a, transport=httpx.MockTransport(handler), **kw)
    try:
        assert asyncio.run(ss.liked_song_artists()) == ["Hozier", "Aurora"]
        assert seen == ["/api/v1/wishlist", "/api/v1/wishlist"]
    finally:
        httpx.AsyncClient = real
        _restore()


def test_the_watchlist_reader():
    _fake_soulsync()
    real = ss._get

    async def fake_get(path, params=None):
        assert path == "/watchlist"
        return {"artists": [{"artist_name": "Daft Punk"}, {"artist_name": ""}, {"id": 3}]}
    ss._get = fake_get
    try:
        assert asyncio.run(ss.watchlist_artists()) == ["Daft Punk"]

        async def silent(path, params=None):
            return None
        ss._get = silent
        assert asyncio.run(ss.watchlist_artists()) is None, "no answer is not an empty watchlist"
    finally:
        ss._get = real
        _restore()


def test_the_download_trigger_is_never_named():
    src = (_ROOT / "src" / "services" / "soulsync_client.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert '"/request"' not in code and "'/request'" not in code


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    sys.exit(1 if fails else 0)
