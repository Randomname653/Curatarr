"""discography_summary: derivative-release composition (secondaryTypes).

A discography that is mostly Live/Compilation/Remix re-issues is a
deletion signal the judge never saw. Contract: the segment only renders
when derivatives exist, and a dual-tagged album ("Live" + "Compilation")
counts its GB ONCE while bumping both type counters.

    python tests/test_lidarr_discography_secondary_types.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


import src.services.lidarr_discography as ld


def _album(atype, gb, secondary=(), files=10, monitored=True):
    return {"albumType": atype, "monitored": monitored,
            "secondaryTypes": list(secondary),
            "statistics": {"trackFileCount": files, "trackCount": files,
                           "sizeOnDisk": gb * 1e9}}


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._p = payload

    def json(self):
        return self._p


def _summary(albums):
    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def get(self, url, params=None):
            if url.endswith("/artist"):
                return _Resp([{"id": 1, "artistName": "Motörhead",
                               "foreignArtistId": "mb-1"}])
            return _Resp(albums)

    real_client, real_url, real_key = (ld.httpx.AsyncClient,
                                       ld.settings.LIDARR_URL,
                                       ld.settings.LIDARR_API_KEY)
    ld.httpx.AsyncClient = _Client
    ld.settings.LIDARR_URL = "http://lidarr.local"
    ld.settings.LIDARR_API_KEY = "k"
    try:
        return asyncio.run(ld.discography_summary(artist_mbid="mb-1"))
    finally:
        ld.httpx.AsyncClient = real_client
        ld.settings.LIDARR_URL = real_url
        ld.settings.LIDARR_API_KEY = real_key


line = _summary([_album("Album", 4.0),
                 _album("Album", 2.0, ["Live"]),
                 _album("Album", 1.0, ["Live", "Compilation"]),
                 _album("EP", 0.5)])
check("derivative segment renders with per-type counts",
      line is not None and "derivative re-issues: 2 live + 1 compilation" in line)
check("dual-tagged album's GB counted once (3.0, not 4.0)",
      "= 3.0 GB" in line)
check("share is derivative GB over the artist total (3.0/7.5 = 40%)",
      "(40% of artist total)" in line)

line = _summary([_album("Album", 4.0), _album("EP", 0.5)])
check("no derivatives -> no segment", "derivative" not in (line or ""))
check("the base line survives untouched",
      line is not None and line.startswith("on disk:"))

line = _summary([_album("Album", 2.0, ["Live"], files=0, monitored=True)])
check("a fileless ghost contributes no derivative GB",
      line is not None and "derivative" not in line and "NO files" in line)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
