"""Lidarr optional, step 2: music candidates and deletions through Plex.

    python tests/test_music_source.py
"""
import asyncio
import pathlib
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.config import settings  # noqa: E402
from src.services import lyrics as ly  # noqa: E402
from src.services import music_source as ms  # noqa: E402

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="curatarr-music-source-"))
_ORIG_DB = ly.LYRICS_DB_PATH


def _use_db(name):
    """Point the module-level default at a scratch index (readers without db_path use it)."""
    ly.LYRICS_DB_PATH = ly.PLEX_MUSIC_DB_PATH = _TMP / f"{name}.db"
    return ly.LYRICS_DB_PATH


def _seed(db):
    def track(key, akey, aname, album, title, size):
        return {"ratingKey": key, "grandparentRatingKey": akey, "parentRatingKey": "al-" + album,
                "grandparentTitle": aname, "parentTitle": album, "title": title, "duration": 1,
                "Media": [{"Part": [{"size": size, "Stream": []}]}]}
    tracks = [track("1", "a1", "The Band", "First", "One", 4_000_000), track("2", "a1", "The Band", "Second", "Two", 6_000_000),
              track("3", "a2", "Other", "Solo", "Three", 1_000_000)]
    artists = [{"ratingKey": "a1", "title": "The Band", "Guid": [{"id": "mbid://aaaa-1"}]}, {"ratingKey": "a2", "title": "Other"}]

    async def list_tracks(sec):
        return tracks

    async def list_artists(sec):
        return artists

    async def fetch_text(k):
        return None

    asyncio.run(ly.sync_lyrics([("14", "Music")], list_tracks, fetch_text, list_artists=list_artists, db_path=db))


def test_music_service_prefers_lidarr_then_the_plex_index_then_nothing():
    _use_db("empty")
    url, key = settings.LIDARR_URL, settings.LIDARR_API_KEY
    try:
        settings.LIDARR_URL, settings.LIDARR_API_KEY = None, None
        assert ms.music_service() is None, "no Lidarr, empty index: no music curation"
        _seed(_use_db("seeded"))
        assert ms.music_service() == "plex"
        settings.LIDARR_URL, settings.LIDARR_API_KEY = "http://lidarr:8686", "k"
        assert ms.music_service() == "lidarr", "Lidarr configured wins, whatever the index holds"
    finally:
        settings.LIDARR_URL, settings.LIDARR_API_KEY = url, key
        ly.LYRICS_DB_PATH = ly.PLEX_MUSIC_DB_PATH = _ORIG_DB


def test_plex_candidates_look_like_lidarr_candidates():
    from src.routers import recommendations as rec
    _seed(_use_db("cands"))
    try:
        items = asyncio.run(rec._plex_music_candidates())
        assert [i["title"] for i in items] == ["Other", "The Band"]
        band = next(i for i in items if i["title"] == "The Band")
        assert band["service"] == "plex" and band["category"] == "music"
        assert band["media_id" if "media_id" in band else "arr_id"] == "a1" and band["plex_rating_key"] == "a1"
        assert band["musicbrainz_id"] == "aaaa-1" and band["album_count"] == 2 and band["track_count"] == 2
        assert abs(band["size_mb"] - 10_000_000 / (1024 * 1024)) < 0.01
        assert band["arr_url"].startswith(str(settings.effective_plex_url).rstrip("/") + "/web/")
        assert next(i for i in items if i["title"] == "Other")["musicbrainz_id"] is None
    finally:
        ly.LYRICS_DB_PATH = ly.PLEX_MUSIC_DB_PATH = _ORIG_DB


class _Resp:
    def __init__(self, status):
        self.status_code = status


class _Client:
    def __init__(self, delete_status, get_status):
        self._d, self._g, self.calls = delete_status, get_status, []

    async def delete(self, url, headers=None):
        self.calls.append(("DELETE", url))
        return _Resp(self._d)

    async def get(self, url, headers=None):
        self.calls.append(("GET", url))
        return _Resp(self._g)

    async def aclose(self):
        pass


def test_plex_delete_confirms_the_item_is_gone_and_drops_it_from_the_index():
    from src.config import settings
    from src.routers import recommendations as rec
    _seed(_use_db("delete"))
    # Pin the Plex configuration: _plex_delete_artist refuses to touch an
    # unconfigured server, and CI has no .env — without this the test read
    # that refusal as the behaviour under test and passed only on a machine
    # that happens to have Plex set up.
    _plex = (settings.PLEX_URL, settings.PLEX_TOKEN)
    from pydantic import SecretStr
    settings.PLEX_URL = "http://plex.test:32400"
    settings.PLEX_TOKEN = SecretStr("test-token")   # the field is a SecretStr
    try:
        assert settings.effective_plex_url and settings.effective_plex_token
        assert ms.plex_music_indexed()
        ok = asyncio.run(rec._plex_delete_artist("a1", client=_Client(200, 404)))
        assert ok is True
        assert ly.plex_artist("a1") is None and ly.plex_artist("a2") is not None, "deleted artist leaves the index at once"
        assert ly.lyrics_coverage()["tracks"] == 1
        # deletion refused (403: 'Allow media deletion' off) → False, nothing touched
        assert asyncio.run(rec._plex_delete_artist("a2", client=_Client(403, 200))) is False
        assert ly.plex_artist("a2") is not None
        # Plex said 200 but the item is still there → failure, never a success
        c = _Client(200, 200)
        assert asyncio.run(rec._plex_delete_artist("a2", client=c)) is False
        assert [m for m, _ in c.calls] == ["DELETE", "GET"] and ly.plex_artist("a2") is not None
    finally:
        settings.PLEX_URL, settings.PLEX_TOKEN = _plex
        ly.LYRICS_DB_PATH = ly.PLEX_MUSIC_DB_PATH = _ORIG_DB


def test_the_wiring_reads_music_service():
    sched = (_ROOT / "src/services/scheduler.py").read_text(encoding="utf-8")
    assert '"music": music_service() or "lidarr"' in sched
    rec = (_ROOT / "src/routers/recommendations.py").read_text(encoding="utf-8")
    assert "PLEX MUSIC (Lidarr optional)" in rec and 'if p.service == "plex":' in rec
    js = (_ROOT / "frontend/js/deletions.js").read_text(encoding="utf-8")
    assert "p.service !== 'plex' ? {label: 'Fix match'" in js, "no arr to pin a match against"


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
