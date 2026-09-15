"""Lidarr optional, steps 3 + 5: the Music page rows and the curator's
music evidence come from the Plex index when Lidarr is not configured.

    python tests/test_music_plex_pages.py
"""
import asyncio
import pathlib
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.config import settings  # noqa: E402
from src.services import lyrics as ly  # noqa: E402

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="curatarr-music-pages-"))
_ORIG_DB = ly.LYRICS_DB_PATH


def _use_db(name):
    ly.LYRICS_DB_PATH = ly.PLEX_MUSIC_DB_PATH = _TMP / f"{name}.db"
    return ly.LYRICS_DB_PATH


def _seed(db):
    def track(key, akey, aname, album, year, title, size, stream=None):
        streams = [{"streamType": 4, "codec": "txt", "key": stream}] if stream else []
        return {"ratingKey": key, "grandparentRatingKey": akey, "parentRatingKey": "al-" + album,
                "grandparentTitle": aname, "parentTitle": album, "parentYear": year, "title": title,
                "duration": 1, "addedAt": 1700000000, "Media": [{"Part": [{"size": size, "Stream": streams}]}]}
    tracks = [track("1", "a1", "The Band", "First Light", 2001, "One", 4_000_000, "/s/1"),
              track("2", "a1", "The Band", "First Light", 2001, "Two", 6_000_000),
              track("3", "a1", "The Band", "Second – Wind", 2009, "Three", 2_000_000),
              track("4", "a2", "Other", "Solo", None, "Four", 1_000_000)]
    artists = [{"ratingKey": "a1", "title": "The Band", "addedAt": 1690000000, "Guid": [{"id": "mbid://aaaa-1"}]},
               {"ratingKey": "a2", "title": "Other", "addedAt": 1690000000}]

    async def list_tracks(sec):
        return tracks

    async def list_artists(sec):
        return artists

    async def fetch_text(k):
        return b"words on file\n" if k == "/s/1" else None

    asyncio.run(ly.sync_lyrics([("14", "Music")], list_tracks, fetch_text, list_artists=list_artists, db_path=db))


def _without_lidarr(fn):
    url, key = settings.LIDARR_URL, settings.LIDARR_API_KEY
    settings.LIDARR_URL, settings.LIDARR_API_KEY = None, None
    try:
        return fn()
    finally:
        settings.LIDARR_URL, settings.LIDARR_API_KEY = url, key
        ly.LYRICS_DB_PATH = ly.PLEX_MUSIC_DB_PATH = _ORIG_DB


def test_the_music_page_rows_come_from_the_index_in_lidarrs_shape():
    from src.routers import library as lib

    def run():
        _seed(_use_db("rows"))
        items_raw, tags, info = asyncio.run(lib._fetch_arr_library("lidarr"))
        assert info["source"] == "plex-index" and tags == []
        band = next(r for r in items_raw if r["artistName"] == "The Band")
        assert band["id"] == "a1" and band["foreignArtistId"] == "aaaa-1" and band["_source"] == "plex"
        assert band["statistics"] == {"sizeOnDisk": 12_000_000, "albumCount": 2, "trackFileCount": 3}
        row = lib._flatten_arr_item("lidarr", band, None)
        assert (row["title"], row["mbid"], row["size_on_disk"], row["album_count"]) == ("The Band", "aaaa-1", 12_000_000, 2)
        assert row["added"].startswith("2023-")
        # an empty index: the old "not configured" answer stays
        _use_db("empty")
        try:
            asyncio.run(lib._fetch_arr_library("lidarr"))
            assert False, "expected 400"
        except Exception as e:
            assert "not configured" in str(e)
    _without_lidarr(run)


def test_reenrich_on_a_plex_artist_drops_the_right_cache_keys_and_queues():
    from src.routers import library as lib
    from src.cache.metadata_cache import MetadataCache, _CACHE_VERSION

    def run():
        _seed(_use_db("reenrich"))
        cache = MetadataCache()
        try:
            cache.set_cache("enriched:music:The Band", {"title": "The Band"}, days=30)
            cache.set_cache("raw:music:The Band", {"title": "The Band", "mbid": "aaaa-1"}, days=30)
        finally:
            cache.close()
        queued = []
        import src.services.bg_tasks as bg
        orig = bg.track_task

        def fake_track(coro, name=None):
            queued.append(name)
            coro.close()
        bg.track_task = fake_track
        try:
            class Req:
                service, arr_id, mode = "lidarr", 1, "summary"
            row = ly.plex_artist("a1")
            res = lib._reenrich_plex_artist(Req, row)
            assert res["queued"] and res["service"] == "plex" and res["title"] == "The Band"
            assert queued == ["library_reenrich:plex:a1"]
            cache = MetadataCache()
            try:
                assert cache.get_cache("enriched:music:The Band") is None, "summary mode drops the polished entry"
                assert cache.get_cache("raw:music:The Band") is not None, "…and keeps the raw one"
                Req.mode = "metadata"
                lib._reenrich_plex_artist(Req, row)
                assert cache.get_cache("raw:music:The Band") is None, "metadata mode drops the raw entry too"
            finally:
                cache.close()
        finally:
            bg.track_task = orig
    _without_lidarr(run)


def test_discography_and_album_dossier_fall_back_to_the_index():
    from src.services.lidarr_discography import discography_summary, plex_discography_summary
    from src.services import album_dossier as ad

    def run():
        _seed(_use_db("evidence"))
        line = asyncio.run(discography_summary(artist_mbid="aaaa-1", artist_name="whatever"))
        assert line == "on disk (Plex): 2 albums, 3 tracks (0.0 GB), 2001–2009; lyrics on file for 1 tracks", line
        assert plex_discography_summary(artist_name="the band") == line, "name lookup, case-insensitive"
        assert plex_discography_summary(artist_name="Nobody") is None
        artist, alb, titles = ad._plex_album("The Band", "second - wind")
        assert artist["artistName"] == "The Band" and artist["foreignArtistId"] == "aaaa-1"
        assert alb["title"] == "Second – Wind" and alb["releaseDate"] == "2009-01-01"
        assert alb["statistics"] == {"trackFileCount": 1, "trackCount": 1, "sizeOnDisk": 2_000_000}
        assert titles == ["Three"]
        assert ad._plex_album("The Band", "nope") == (None, None, [])
    _without_lidarr(run)


def test_the_page_and_status_know_the_plex_source():
    js = (_ROOT / "frontend/js/arr.js").read_text(encoding="utf-8")
    assert "info.music_source === 'plex'" in js and "_arrTabsEff" in js
    assert "t.id === 'all' || t.id === 'backlog'" in js, "no arr to add to: no Add-New, no Curatarr-Added tab"
    assert "ci.source === 'plex-index'" in js
    lib = (_ROOT / "src/routers/library.py").read_text(encoding="utf-8")
    assert 'out[svc]["music_source"] = music_service()' in lib
    assert "def _plex_music_library()" in lib and "def _reenrich_plex_artist(" in lib


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
