"""ListenBrainz client: digest math, tri-state cache, topup markers.

Contract (same tri-state as significance): dict = real data (cache 30 d),
{} = DEFINITIVE nothing on LB (cache 7 d), None = TRANSIENT (never cached,
retried). topup_listenbrainz additionally treats a missing artist MBID as
a PREREQUISITE gap: nothing stamped, so the top-up re-runs once the #41
upgrade pass fills the mbid.

    python tests/test_listenbrainz.py
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


import src.services.listenbrainz as lb


class FakeCache:
    def __init__(self, store=None):
        self.store = store or {}
        self.writes = []          # (key, value, days)

    def get_cache(self, key):
        return self.store.get(key)

    def set_cache(self, key, value, days=None):
        self.writes.append((key, value if not isinstance(value, dict)
                            else dict(value), days))
        self.store[key] = {"response": value}

    def patch_cache(self, key, updates, drop=(), days=None):
        row = self.store.get(key)
        if not row:
            return False
        row["response"].update(updates)
        for field in drop:
            row["response"].pop(field, None)
        self.writes.append((key, dict(updates), days))
        return True

    def close(self):
        pass


class FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._p = payload

    def json(self):
        return self._p


def _entry(name, listens, users, date="2007-10-10", rg_type="Album"):
    return {"release_group": {"name": name, "date": date, "type": rg_type},
            "total_listen_count": listens, "total_user_count": users}


# ── digest math ────────────────────────────────────────────────────────────

entries = [_entry("OK Computer", 100, 10),
           _entry("In Rainbows — Disk 2", 300, 30),
           _entry("Kid A", 200, 20),
           {"not": "a release group"},          # malformed entry survives
           _entry(None, 50, 5)]                 # nameless RG counts, not listed
d = lb._digest("mbid-x", entries)
check("total_listens sums over ALL entries incl. nameless",
      d["total_listens"] == 100 + 300 + 200 + 50)
check("n_release_groups is the raw array length", d["n_release_groups"] == 5)
check("albums sorted descending by listens",
      [a["name"] for a in d["albums"]] == ["In Rainbows — Disk 2",
                                           "Kid A", "OK Computer"])
check("top_user_count is the max", d["top_user_count"] == 30)
check("norm_name folds dashes + punctuation",
      d["albums"][0]["norm_name"] == "in rainbows disk 2")

big = lb._digest("m", [_entry(f"A{i}", i, 1) for i in range(600)])
check("album list capped, totals still over everything",
      len(big["albums"]) == lb._ALBUM_CAP
      and big["n_release_groups"] == 600
      and big["total_listens"] == sum(range(600)))

check("norm_album_title matches typographic-dash variants",
      lb.norm_album_title("In Rainbows – Disk 2")
      == lb.norm_album_title("in rainbows - disk 2!"))


# ── fetch_artist_popularity tri-state ──────────────────────────────────────

def _fetch(resp=None, raise_exc=False, store=None, token="test-token"):
    cache = FakeCache(store)
    real = lb._lb_request
    real_token = getattr(lb.settings, "LISTENBRAINZ_TOKEN", None)
    lb.settings.LISTENBRAINZ_TOKEN = token

    async def _fake(client, url, headers):
        if raise_exc:
            raise OSError("network down")
        return resp
    lb._lb_request = _fake
    try:
        out = asyncio.run(lb.fetch_artist_popularity("mbid-x", cache=cache))
    finally:
        lb._lb_request = real
        lb.settings.LISTENBRAINZ_TOKEN = real_token
    return out, cache


out, cache = _fetch(FakeResp(200, entries), token=None)
check("no LISTENBRAINZ_TOKEN: transient skip — nothing fetched or cached",
      out is None and cache.writes == [])

out, cache = _fetch(raise_exc=True)
check("TRANSIENT (network): returns None, nothing cached",
      out is None and cache.writes == [])

out, cache = _fetch(FakeResp(500))
check("TRANSIENT (HTTP 500): returns None, nothing cached",
      out is None and cache.writes == [])

out, cache = _fetch(FakeResp(200, []))
check("DEFINITIVE empty ([]): returns {}, cached 7 d",
      out == {} and len(cache.writes) == 1 and cache.writes[0][2] == 7)

out, cache = _fetch(FakeResp(404))
check("DEFINITIVE unknown MBID (404): returns {}, cached 7 d",
      out == {} and len(cache.writes) == 1 and cache.writes[0][2] == 7)

out, cache = _fetch(FakeResp(200, entries))
check("real data: digest returned, cached 30 d",
      out and out["total_listens"] == 650
      and len(cache.writes) == 1 and cache.writes[0][2] == 30)

out, cache = _fetch(store={"lb:artist:mbid-x": {"response": {"total_listens": 7}}})
check("cache hit short-circuits (no request, no write)",
      out == {"total_listens": 7} and cache.writes == [])

out, cache = _fetch(store={"lb:artist:mbid-x": {"response": {}}})
check("negative cache hit returns {} without a request",
      out == {} and cache.writes == [])


# ── topup_listenbrainz marker semantics ────────────────────────────────────

def _topup(pop_result, doc=None, artist_mbid=None):
    doc = {"media_type": "music", "title": "Radiohead"} if doc is None else doc
    cache = FakeCache({"raw:music:Radiohead": {"response": doc}})
    real = lb.fetch_artist_popularity

    async def _fake(mbid, cache=None):
        return pop_result
    lb.fetch_artist_popularity = _fake
    try:
        added = asyncio.run(lb.topup_listenbrainz(
            "Radiohead", "music", artist_mbid=artist_mbid, cache=cache))
    finally:
        lb.fetch_artist_popularity = real
    return added, cache


added, cache = _topup({"total_listens": 1}, doc={"media_type": "music"})
check("no MBID anywhere: prerequisite gap — nothing stamped",
      added is False and cache.writes == [])

added, cache = _topup(None, artist_mbid="m1")
check("transient fetch: nothing stamped, retried next debate",
      added is False and cache.writes == [])

added, cache = _topup({}, artist_mbid="m1")
check("definitive empty: lb_checked stamped, no payload",
      added is False and len(cache.writes) == 1
      and cache.writes[0][1].get("lb_checked") is True
      and "lb_popularity" not in cache.writes[0][1])

digest = {"total_listens": 650, "n_release_groups": 3, "albums": []}
added, cache = _topup(digest, artist_mbid="m1")
check("real digest: stamped AND stored",
      added is True and cache.writes[0][1].get("lb_popularity") == digest
      and cache.writes[0][1].get("lb_checked") is True)

added, cache = _topup(digest, doc={"media_type": "music", "mbid": "from-doc"})
check("MBID from the raw doc works when the caller passes none",
      added is True)

added, cache = _topup(digest, doc={"media_type": "music", "lb_checked": True},
                      artist_mbid="m1")
check("already-checked doc: idempotent no-op",
      added is False and cache.writes == [])

check("non-music media type refuses",
      asyncio.run(lb.topup_listenbrainz("X", "movie")) is False)


# ── rank lookup (the album_dossier consumption pattern) ────────────────────

pool = lb._digest("m", [_entry("Live at Pompeii", 900, 90),
                        _entry("The Dark Side of the Moon", 800, 80)])["albums"]
want = lb.norm_album_title("the dark side of the moon")
idx = next((i for i, a in enumerate(pool) if a["norm_name"] == want), None)
check("rank lookup finds the differently-cased album at the right rank",
      idx == 1)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
