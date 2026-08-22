"""Tests for SoulSync-as-primary music metadata (Lidarr degraded to
structural-only; MB/Last.fm become gap-fillers + the no-SoulSync fallback).

    python tests/test_soulsync_primary.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.services.music_metadata as mm
import src.services.soulsync_client as sc

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── fakes: no network, no cache DB ───────────────────────────────────────────

class FakeCache:
    def get_cache(self, *a, **k): return None
    def set_cache(self, *a, **k): return None
    def close(self): return None


mm.MetadataCache = FakeCache

calls = {"ss": 0, "mb": 0, "lfm": 0}
SS_FULL = {
    "id": "558135", "name": "Ion Dissonance",
    "genres": ["Metalcore", "Mathcore"], "lastfm_tags": ["mathcore", "chaotic"],
    "lastfm_bio": "Canadian mathcore pioneers.", "lastfm_listeners": 61000,
    "similar_artists": ["Car Bomb", "The Dillinger Escape Plan"],
    "mood": "aggressive", "musicbrainz_id": "c1c621a8",
    "external_ids": {"deezer_id": "11324618"}, "album_count": 5,
}
ss_response = SS_FULL


async def fake_ss(name):
    calls["ss"] += 1
    return ss_response


async def fake_mb(name, mbid=None):
    calls["mb"] += 1
    return {"mbid": "mb-exact", "type": "Group", "country": "CA",
            "genres": ["metal"], "tags": ["mb-tag"], "rating": 4.1}


async def fake_lfm(name):
    calls["lfm"] += 1
    return {"genres": ["lfm-genre"], "tags": ["lfm-tag"],
            "similar_artists": ["LFM Similar"], "bio": "lfm bio [1]",
            "listeners": 999}


sc.artist_info = fake_ss
mm.fetch_musicbrainz_artist = fake_mb
mm.fetch_lastfm_artist = fake_lfm

# ── SoulSync rich → primary, Last.fm call skipped ────────────────────────────

p = asyncio.run(mm.enrich_artist("Ion Dissonance", mbid="c1c621a8"))
check("soulsync fields win the merge",
      p["genres"][:2] == ["Metalcore", "Mathcore"]
      and p["similar_artists"][0] == "Car Bomb"
      and p["bio"] == "Canadian mathcore pioneers."
      and p["listeners"] == 61000)
check("MB still supplies its exclusive facts (type/country/rating)",
      p["type"] == "Group" and p["rating"] == 4.1 and p["mbid"] == "mb-exact")
check("Last.fm call SKIPPED when SoulSync covers its fields",
      calls["lfm"] == 0 and calls["ss"] == 1 and calls["mb"] == 1)
check("new fields exposed: mood + deezer_id + source tag",
      p["mood"] == "aggressive" and p["deezer_id"] == "11324618"
      and p["metadata_source"] == "soulsync")
check("embedding_text template unchanged (starts name — genres, Tags:)",
      p["embedding_text"].startswith("Ion Dissonance — Metalcore, Mathcore")
      and "Tags: " in p["embedding_text"]
      and "Similar to: Car Bomb" in p["embedding_text"])

# ── partial SoulSync (no bio) → Last.fm fills the gap ────────────────────────

for k in calls:
    calls[k] = 0
ss_response = {**SS_FULL, "lastfm_bio": None, "similar_artists": []}
p2 = asyncio.run(mm.enrich_artist("Partial Artist"))
check("partial coverage -> Last.fm called and fills bio/similar",
      calls["lfm"] == 1 and p2["bio"] == "lfm bio"
      and p2["similar_artists"] == ["LFM Similar"])

# ── no SoulSync (unconfigured install) → exact legacy behaviour ──────────────

for k in calls:
    calls[k] = 0
ss_response = None
p3 = asyncio.run(mm.enrich_artist("Legacy Artist"))
check("no SoulSync -> legacy MB+Last.fm path, tagged accordingly",
      calls["lfm"] == 1 and calls["mb"] == 1
      and p3["metadata_source"] == "mb+lastfm"
      and p3["genres"][0] == "metal")

# ── fast mode never touches SoulSync ─────────────────────────────────────────

for k in calls:
    calls[k] = 0
ss_response = SS_FULL
p4 = asyncio.run(mm.enrich_artist("Fast Artist", skip_mb=True))
check("fast mode: no SoulSync, no MB (throughput path unchanged)",
      calls["ss"] == 0 and calls["mb"] == 0 and calls["lfm"] == 1)

# ── all-none → None (neg-cache path) ─────────────────────────────────────────


async def none_lfm(name):
    return None


async def none_mb(name, mbid=None):
    return None


ss_response = None
mm.fetch_lastfm_artist = none_lfm
mm.fetch_musicbrainz_artist = none_mb
check("nothing found anywhere -> None",
      asyncio.run(mm.enrich_artist("Ghost")) is None)

# ── wiring asserts ───────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
src = (root / "src/services/music_metadata.py").read_text(encoding="utf-8")
check("no download endpoint anywhere near this path",
      "/api/v1/request" not in src)
rec = (root / "src/routers/recommendations.py").read_text(encoding="utf-8")
check("structural-source note documents why Lidarr stays",
      "SOURCE-OF-TRUTH note" in rec and "lidarr:{id}" in rec)

# -- neighbour lookups run in parallel, and a blip is not cached for a week --

import src.services.app_state as _app_state          # noqa: E402
from src.services import recommendations_engine as _re   # noqa: E402


def _run_neighbors(responses):
    """Drive _get_music_neighbors against fake artist_info results.

    ``responses`` maps artist -> dict to return, or an Exception to raise.
    Returns (names, written_cache_or_None, concurrent_high_water_mark).
    """
    live = {"now": 0, "peak": 0}
    written = {}

    async def fake_artist_info(artist):
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.01)          # overlap only if truly concurrent
        live["now"] -= 1
        r = responses[artist]
        if isinstance(r, Exception):
            raise r
        return r

    orig = (sc.artist_info, _app_state.get_state, _app_state.set_state)
    sc.artist_info = fake_artist_info
    _app_state.get_state = lambda k, d=None: None      # force a cold lookup
    _app_state.set_state = lambda k, v: written.update({k: v})
    try:
        names = asyncio.run(_re._get_music_neighbors(1, list(responses)))
    finally:
        sc.artist_info, _app_state.get_state, _app_state.set_state = orig
    return names, (written or None), live["peak"]


ok_names, ok_cache, peak = _run_neighbors({
    "a": {"similar_artists": ["A1", "A2"]},
    "b": {"similar_artists": ["B1"]},
    "c": {"similar_artists": ["A1"]},          # duplicate, must collapse
})
check("neighbour lookups actually overlap", peak > 1)
check("results merge and de-duplicate", ok_names == ["A1", "A2", "B1"])
check("a clean run is cached", ok_cache is not None)

part_names, part_cache, _ = _run_neighbors({
    "a": {"similar_artists": ["A1"]},
    "b": RuntimeError("soulsync unreachable"),
    "c": {"similar_artists": ["C1"]},
})
check("one failed lookup does not discard the others",
      part_names == ["A1", "C1"])
check("a partial result is NOT frozen in the 168h cache", part_cache is None)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
