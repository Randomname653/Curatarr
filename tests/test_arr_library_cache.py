"""The arr library cache: an expired copy is served at once and refreshed
behind the request; an arr that is not configured never serves its old copy.

2026-10-06: the Knowledge Base overview waited 185-198 s per load. Once the
15-minute window had run out, the next request fetched Sonarr, Radarr and
Lidarr itself, and Lidarr's artist list takes a minute, or two 90-second
attempts when it hangs.

    python tests/test_arr_library_cache.py
"""
import asyncio
import pathlib
import sys
import time

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from fastapi import HTTPException  # noqa: E402

import src.routers.library as lib  # noqa: E402

calls: list = []
mode = {"fail": False, "delay": 0.0}


class _Client:
    def __init__(self, svc):
        self.svc = svc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def _items(self):
        calls.append(self.svc)
        if mode["delay"]:
            await asyncio.sleep(mode["delay"])
        if mode["fail"]:
            raise TimeoutError("hung")
        return [{"id": len(calls), "title": f"fresh {len(calls)}"}]

    get_series = get_movies = get_artists = _items

    async def list_tags(self):
        return [{"id": 1, "label": "t"}]


configured = {"sonarr": True, "radarr": True, "lidarr": True}
lib._make_client = lambda svc, url, key: _Client(svc)
lib._get_arr_url_key = lambda svc: (("http://arr", "key") if configured.get(svc) else (None, None))
lib._persist_lib_cache = lambda *a, **k: None


def _reset(**cache):
    calls.clear()
    mode.update(fail=False, delay=0.0)
    configured.update(sonarr=True, radarr=True, lidarr=True)
    lib._LIB_CACHE.clear()
    lib._LIB_REFRESHING.clear()
    for svc, age in cache.items():
        lib._LIB_CACHE[svc] = {"items_raw": [{"id": 0, "title": "old"}], "tags": [],
                               "at": time.time() - age}


def test_a_fresh_copy_is_served_without_a_call():
    _reset(sonarr=60)
    items, tags, info = asyncio.run(lib._fetch_arr_library("sonarr"))
    assert items[0]["title"] == "old" and info["source"] == "cache" and "refreshing" not in info
    assert calls == []


def test_an_expired_copy_is_served_at_once_and_refreshed_behind():
    _reset(lidarr=lib._LIB_CACHE_TTL + 60)
    mode["delay"] = 0.2                                  # a slow Lidarr

    async def run():
        t = time.monotonic()
        first = await lib._fetch_arr_library("lidarr")
        second = await lib._fetch_arr_library("lidarr")   # while the refresh is in flight
        waited = time.monotonic() - t
        await lib._LIB_REFRESHING["lidarr"]
        third = await lib._fetch_arr_library("lidarr")
        return first, second, third, waited

    first, second, third, waited = asyncio.run(run())
    assert waited < 0.1, f"the request waited {waited:.2f}s for Lidarr"
    assert first[0][0]["title"] == "old" and first[2]["refreshing"] is True
    assert second[0][0]["title"] == "old"
    assert calls == ["lidarr"], "one refresh per service at a time"
    assert third[0][0]["title"] == "fresh 1" and third[2]["source"] == "cache"


def test_a_failed_refresh_keeps_the_copy():
    _reset(radarr=lib._LIB_CACHE_TTL + 60)
    mode["fail"] = True

    async def run():
        await lib._fetch_arr_library("radarr")
        await lib._LIB_REFRESHING["radarr"]
        return await lib._fetch_arr_library("radarr")

    items, _, info = asyncio.run(run())
    assert items[0]["title"] == "old" and info["source"] == "cache"


def test_no_copy_or_a_forced_refresh_waits_for_the_live_list():
    _reset()
    items, _, info = asyncio.run(lib._fetch_arr_library("sonarr"))
    assert items[0]["title"] == "fresh 1" and info["source"] == "live"
    _reset(sonarr=60)
    items, _, info = asyncio.run(lib._fetch_arr_library("sonarr", force_refresh=True))
    assert items[0]["title"] == "fresh 1" and info["source"] == "live"
    _reset(sonarr=60)
    mode["fail"] = True
    items, _, info = asyncio.run(lib._fetch_arr_library("sonarr", force_refresh=True))
    assert items[0]["title"] == "old" and info["source"] == "stale"
    _reset()
    mode["fail"] = True
    try:
        asyncio.run(lib._fetch_arr_library("sonarr"))
        raise AssertionError("expected 502")
    except HTTPException as e:
        assert e.status_code == 502


def test_an_arr_switched_off_never_serves_its_old_copy():
    _reset(lidarr=60, sonarr=60)
    configured.update(lidarr=False, sonarr=False)
    real = lib._plex_music_library
    lib._plex_music_library = lambda: ([{"id": "p1", "_source": "plex"}], [], {"source": "plex-index"})
    try:
        items, _, info = asyncio.run(lib._fetch_arr_library("lidarr"))
        assert info["source"] == "plex-index" and items[0]["_source"] == "plex"
    finally:
        lib._plex_music_library = real
    try:
        asyncio.run(lib._fetch_arr_library("sonarr"))
        raise AssertionError("expected 400")
    except HTTPException as e:
        assert e.status_code == 400
    assert calls == []


def test_startup_restores_only_configured_arrs():
    _reset()
    configured.update(lidarr=False)

    class _MC:
        def get_cache(self, key):
            return {"response": {"items_raw": [{"id": 1}], "tags": [], "at": time.time() - 30}}

        def close(self):
            pass

    import src.cache.metadata_cache as mcmod
    real = mcmod.MetadataCache
    mcmod.MetadataCache = _MC
    try:
        assert lib._load_lib_cache_from_db() == 2
        assert set(lib._LIB_CACHE) == {"sonarr", "radarr"}, "Lidarr's old list stays in the DB"
    finally:
        mcmod.MetadataCache = real
        lib._LIB_CACHE.clear()


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
