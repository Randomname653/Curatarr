"""A slow Lidarr must not stall the app — or be asked twice for nothing.

* ``ClientTimeout(total=90)`` counted the whole body: Lidarr's artist list
  (~124 s measured on 5,211 artists) failed every time, after two attempts
  — 182 s of Lidarr load and no library. Listings now get a READ budget
  (ARR_LISTING_TIMEOUT_S, default 240 s), 10 s to connect, one attempt.
* ``int(Retry-After)`` raised ValueError on the HTTP-date form.
* The discography line and the album dossier downloaded the FULL artist
  list (15-30 MB) per chat turn and per judged candidate.
* discogs_offline ran two blocking ``httpx.get`` calls on the event loop.

A real local aiohttp server stands in for the arr; no network.

    python tests/test_arr_resilience.py
"""
import asyncio
import email.utils
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp import web

import src.services.arr_client as ac
import src.services.lidarr_discography as ld

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── Retry-After ──────────────────────────────────────────────────────────────
print("\n-- Retry-After --")
check("delta-seconds", ac.retry_after_s("7") == 7.0)
future = email.utils.formatdate(time.time() + 30, usegmt=True)
check("HTTP-date form no longer raises", 25 <= ac.retry_after_s(future) <= 31)
check("capped (a proxy cannot park us for an hour)", ac.retry_after_s("3600") == ac._RETRY_AFTER_CAP_S)
check("garbage / missing -> default", ac.retry_after_s("soon") == 60 and ac.retry_after_s(None) == 60)
check("a date in the past -> 0", ac.retry_after_s(email.utils.formatdate(time.time() - 60, usegmt=True)) == 0)

# ── a fake arr ───────────────────────────────────────────────────────────────
hits = {"slow": 0, "stall": 0, "throttled": 0}


async def slow_stream(request):
    """Answers for 2.4 s in total, but never goes quiet for more than 0.4 s —
    a big listing that is slow yet alive."""
    hits["slow"] += 1
    resp = web.StreamResponse(headers={"Content-Type": "application/json"})
    await resp.prepare(request)
    await resp.write(b"[")
    for i in range(6):
        await asyncio.sleep(0.4)
        await resp.write((b"," if i else b"") + b'{"id": %d}' % i)
    await resp.write(b"]")
    await resp.write_eof()
    return resp


async def stall(request):
    hits["stall"] += 1
    await asyncio.sleep(5)
    return web.json_response([])


async def throttled(request):
    hits["throttled"] += 1
    if hits["throttled"] == 1:
        return web.Response(status=429, headers={
            "Retry-After": email.utils.formatdate(time.time() + 1, usegmt=True)})
    return web.json_response({"ok": True})


async def main():
    app = web.Application()
    app.router.add_get("/slow", slow_stream)
    app.router.add_get("/stall", stall)
    app.router.add_get("/throttled", throttled)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    try:
        print("\n-- read budget, not a total budget --")
        async with ac.RadarrClient(base, "k") as c:
            r = await c.request("GET", "/slow", timeout=1.0, max_timeout_retries=0)
        check("a 2.4 s listing that keeps streaming survives a 1 s read budget "
              "(total=1 s used to kill it)", isinstance(r, list) and len(r) == 6)

        t0 = time.monotonic()
        try:
            async with ac.RadarrClient(base, "k") as c:
                await c.request("GET", "/stall", timeout=0.5, max_timeout_retries=0)
            check("a silent arr times out", False)
        except asyncio.TimeoutError:
            check("a silent arr times out on the read budget", time.monotonic() - t0 < 2.5)
        check("listing-style call (max_timeout_retries=0) asks once", hits["stall"] == 1)

        print("\n-- 429 with an HTTP-date Retry-After --")
        slept = []
        real_sleep = asyncio.sleep

        async def fake_sleep(s, *a, **k):
            slept.append(s)
            await real_sleep(0)
        ac.asyncio.sleep = fake_sleep
        try:
            async with ac.RadarrClient(base, "k") as c:
                r = await c.request("GET", "/throttled")
        finally:
            ac.asyncio.sleep = real_sleep
        check("retried after the dated Retry-After instead of crashing",
              r == {"ok": True} and hits["throttled"] == 2 and slept and slept[0] <= 1.5)
    finally:
        await runner.cleanup()

asyncio.run(main())

print("\n-- listings use the shared budget, one attempt --")
seen = []


async def fake_request(self, method, endpoint, **kw):
    seen.append((endpoint, kw.get("timeout"), kw.get("max_timeout_retries")))
    return []
real_request = ac.MediaService.request
ac.MediaService.request = fake_request
ac.listing_timeout = lambda: 240.0
try:
    async def listings():
        async with ac.RadarrClient("http://x", "k") as r:
            await r.get_movies()
        async with ac.SonarrClient("http://x", "k") as s:
            await s.get_series()
        async with ac.LidarrClient("http://x", "k") as lc:
            await lc.get_artists()
    asyncio.run(listings())
finally:
    ac.MediaService.request = real_request
check("movies / series / artists: ARR_LISTING_TIMEOUT_S, no timeout retry",
      sorted(seen) == sorted([("/api/v3/movie", 240.0, 0), ("/api/v3/series", 240.0, 0),
                              ("/api/v1/artist", 240.0, 0)]))
from src.config import settings
check("ARR_LISTING_TIMEOUT_S defaults to 240", settings.ARR_LISTING_TIMEOUT_S == 240)

# ── Lidarr artist without the full list ──────────────────────────────────────
print("\n-- one Lidarr artist without downloading the library --")
calls = []
FULL = [{"id": 1, "artistName": "Motörhead", "foreignArtistId": "mb-1"},
        {"id": 2, "artistName": "New Act", "foreignArtistId": "mb-2"}]


class R:
    def __init__(self, p, code=200):
        self._p, self.status_code = p, code

    def json(self):
        return self._p


class Client:
    async def get(self, url, params=None, **kw):
        calls.append(dict(params or {}))
        if "mbId" in (params or {}):
            return R([a for a in FULL if a["foreignArtistId"] == params["mbId"]])
        return R(FULL)


def find(cache, **kw):
    calls.clear()
    ld.cached_lidarr_artists = lambda: cache
    return asyncio.run(ld.find_lidarr_artist(Client(), "http://l", "k", **kw))

a = find([FULL[0]], mbid="mb-1")
check("cached library hit: no Lidarr call at all", a and a["id"] == 1 and calls == [])
a = find([FULL[0]], name="motörhead")
check("cached hit by name", a and a["id"] == 1 and calls == [])
a = find([FULL[0]], mbid="mb-2")
check("artist added since the cache: one mbId query, never the full list",
      a and a["id"] == 2 and calls == [{"apikey": "k", "mbId": "mb-2"}])
a = find([FULL[0]], name="Nobody")
check("not in the cached library and no mbid: no full download", a is None and calls == [])
a = find(None, name="New Act")
check("no cached library at all: the full list, as before", a and a["id"] == 2
      and calls == [{"apikey": "k"}])

src = lambda p: (ROOT / p).read_text(encoding="utf-8")
check("album dossier resolves through find_lidarr_artist",
      "find_lidarr_artist" in src("src/services/album_dossier.py")
      and 'f"{base}/api/v1/artist",\n                                   params={"apikey": key})' not in src("src/services/album_dossier.py"))

# ── discogs: off the event loop ──────────────────────────────────────────────
print("\n-- discogs refresh keeps the event loop free --")
d = src("src/services/discogs_offline.py")
i = d.index("async def refresh_discogs_styles")
body = d[i:i + 2500]
check("both blocking fetches run in a worker thread",
      "await asyncio.to_thread(_latest_masters_key)" in body
      and "await asyncio.to_thread(_library_artists)" in body)
check("library artists come from the cached Lidarr list first",
      "cached_lidarr_artists()" in d)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
