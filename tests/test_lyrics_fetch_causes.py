"""Why a lyric stream did not come back (2026-11-02).

The nightly lyrics run logged a bare failed count - 1265 failed beside 1912
fetched on 2026-11-01, 3390 beside 320 on 10-30 - and nothing about why:
a stale stream key (404), a busy server (timeout) and a Plex restart all
read the same. fetch_stream counts each failure by its cause, and the run
line carries them as failed_by.

    python tests/test_lyrics_fetch_causes.py
"""
from _harness import check  # noqa: I001 - first: temp stores

import asyncio
from collections import Counter

import httpx

from src.services import lyrics  # noqa: E402


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/ok"):
        return httpx.Response(200, content=b"[00:01.00] la la")
    if path.endswith("/gone"):
        return httpx.Response(404)
    if path.endswith("/busy"):
        raise httpx.ReadTimeout("timed out", request=request)
    return httpx.Response(500)


async def run(paths):
    causes = Counter()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        got = [await lyrics.fetch_stream(client, f"http://plex.test{p}", "tok", causes, p)
               for p in paths]
    return got, causes


got, causes = asyncio.run(run(["/s/ok", "/s/gone", "/s/gone", "/s/busy", "/s/boom"]))
check("a stream that answers comes back as its bytes", got[0] == b"[00:01.00] la la")
check("every other answer is None, as before", got[1:] == [None, None, None, None])
check("...with each failure counted by its cause",
      causes == Counter({"HTTP 404": 2, "ReadTimeout": 1, "HTTP 500": 1}), causes)
got, causes = asyncio.run(run(["/s/ok"]))
check("a run without failures has nothing to count", causes == Counter())

check.done()
