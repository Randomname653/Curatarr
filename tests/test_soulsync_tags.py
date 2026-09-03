"""Tests for SoulSync tag/genre normalisation + the dossier's SoulSync line.

Regression (seen live 2026-09-03, Farin Urlaub 'Am Ende der Sonne'): the
per-album ``lastfm_tags`` field arrives as one BARE JSON-encoded string
('["rock", ...]'). _norm_genres iterated it character-wise, and the dossier
rendered  tags: [, ", r, o, c, k …

    python tests/test_soulsync_tags.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.cache.metadata_cache as mc
import src.services.album_dossier as ad
import src.services.discogs_offline as do
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


# ── _norm_genres: every shape SoulSync has been seen to emit ─────────────────

check("bare JSON-encoded string parses to a list (the live bug)",
      sc._norm_genres('["rock", "punk rock"]') == ["rock", "punk rock"])
check("list of JSON-encoded strings still flattens (artist genres shape)",
      sc._norm_genres(['["Electronic"]']) == ["Electronic"])
check("plain list passes through",
      sc._norm_genres(["Pop/Rock", "Punk"]) == ["Pop/Rock", "Punk"])
check("bare plain string becomes a one-element list, never characters",
      sc._norm_genres("rock") == ["rock"])
check("broken JSON string degrades to one element, never characters",
      sc._norm_genres('["rock') == ['["rock'])
check("None/empty -> []",
      sc._norm_genres(None) == [] and sc._norm_genres([]) == [])
check("case-insensitive dedup survives",
      sc._norm_genres('["Rock", "rock", "Punk"]') == ["Rock", "Punk"])

# ── album_info end-to-end: raw API shapes -> normalised lists ────────────────

RAW_ALBUM = {
    "id": "al1", "title": "Am Ende der Sonne",
    "genres": ['["Pop/Rock"]',],
    "lastfm_tags": '["rock", "hard rock"]',      # the bare-string shape
    "style": "Punk", "mood": None, "label": "Völker hört die Tonträger",
    "record_type": "album", "year": 2004, "track_count": 13,
}


async def fake_get(path, params=None):
    if path == "/library/artists":
        return {"artists": [{"id": "ar1", "name": "Farin Urlaub",
                             "genres": ['["Pop/Rock"]'], "lastfm_tags": None}]}
    if path == "/library/artists/ar1/albums":
        return {"albums": [RAW_ALBUM]}
    return None


_orig_get = sc._get
sc._get = fake_get
try:
    info = asyncio.run(sc.album_info("Farin Urlaub", "Am Ende der Sonne"))
finally:
    sc._get = _orig_get

check("album_info returns lastfm_tags as a parsed list",
      info is not None and info["lastfm_tags"] == ["rock", "hard rock"])
check("album_info genres stay a parsed list",
      info is not None and info["genres"] == ["Pop/Rock"])

# ── dossier SoulSync line: joins lists, drops raw strings defensively ────────

FAKE_ARTIST = {"id": 1, "artistName": "Farin Urlaub"}      # no foreignArtistId
FAKE_ALBUMS = [{"id": 5, "title": "Am Ende der Sonne",
                "releaseDate": "2004-06-01", "albumType": "Album",
                "secondaryTypes": [], "monitored": True,
                "statistics": {"trackFileCount": 13, "trackCount": 13,
                               "sizeOnDisk": 4.2e8}}]


class FakeCache:
    def get_cache(self, *a, **k): return None
    def set_cache(self, *a, **k): return None
    def close(self): return None


class _NoNet:
    """httpx stand-in: any client construction raises, so every guarded
    network section (tracklist, Last.fm) skips itself."""
    def AsyncClient(self, *a, **k):
        raise RuntimeError("no network in tests")


def build_with_soulsync(ss_result):
    async def fake_lidarr(name):
        return FAKE_ARTIST, FAKE_ALBUMS

    async def fake_album_info(artist, album):
        return ss_result

    orig = (ad._lidarr_artist_albums, ad.httpx, sc.album_info,
            mc.MetadataCache, do.STYLES_DB_PATH)
    ad._lidarr_artist_albums = fake_lidarr
    ad.httpx = _NoNet()
    sc.album_info = fake_album_info
    mc.MetadataCache = FakeCache
    do.STYLES_DB_PATH = Path("__no_such_dir__/discogs_styles.db")
    try:
        return asyncio.run(
            ad.build_album_dossier("Farin Urlaub", "Am Ende der Sonne"))
    finally:
        (ad._lidarr_artist_albums, ad.httpx, sc.album_info,
         mc.MetadataCache, do.STYLES_DB_PATH) = orig


d = build_with_soulsync({"genres": ["Pop/Rock"], "style": "Punk",
                         "lastfm_tags": ["rock", "hard rock"],
                         "label": "Völker hört die Tonträger",
                         "record_type": "album"})
check("dossier renders parsed tags as comma list",
      d is not None and "tags: rock, hard rock" in d
      and "genres: Pop/Rock" in d and "style: Punk" in d)

# simulate a client regression: raw JSON strings reach the dossier
d2 = build_with_soulsync({"genres": '["Pop/Rock"]', "style": "Punk",
                          "lastfm_tags": '["rock", "hard rock"]'})
check("raw-string tags/genres are DROPPED, never joined character-wise",
      d2 is not None and "tags:" not in d2 and "genres:" not in d2
      and "style: Punk" in d2 and '[, "' not in d2)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
