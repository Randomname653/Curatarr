"""Deletion-proposal card entity resolution.

Live failure 2026-08-17: Radarr holds BOTH "Good Boy" films (37022 = 2025
dog-horror, tmdb 1422096; 37548 = 2026 abduction thriller, tmdb 1381027).
The (title, category) item_map let the second entry overwrite the first, so
the dog-horror's proposal card shipped with the OTHER film's synopsis and
genres — the owner read the pitch (which correctly described the dog film)
against the wrong plot and filed it as a hallucination. Same failure class
as the Devil-Wears-Prada movie-vs-band collision, one level deeper: WITHIN
one service and category, only the arr_id disambiguates.

    python tests/test_proposal_entity.py
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


import src.routers.recommendations as rr

dog = {"title": "Good Boy", "category": "movie", "service": "radarr",
       "arr_id": 37022, "tmdb_id": 1422096, "year": 2025,
       "genres": "Horror", "overview": "dog plot"}
tommy = {"title": "Good Boy", "category": "movie", "service": "radarr",
         "arr_id": 37548, "tmdb_id": 1381027, "year": 2026,
         "genres": "Thriller, Drama, Mystery", "overview": "tommy plot"}
imap = rr.build_proposal_item_map([dog, tommy])

check("map carries an id key per item PLUS the legacy title key",
      ("id", "radarr", "37022") in imap and ("id", "radarr", "37548") in imap
      and ("Good Boy", "movie") in imap)

seen = {}


async def _fake_tmdb(title, category, tmdb_id=None, year=None,
                     tvdb_id=None, mbid=None):
    seen["tmdb_id"] = tmdb_id
    return None, f"synopsis-of-{tmdb_id}"

_real_fetch = rr._fetch_tmdb
rr._fetch_tmdb = _fake_tmdb
try:
    p = {"title": "Good Boy", "category": "movie", "service": "radarr",
         "arr_id": 37022, "pitch": "dog tropes", "confidence": 0.8}
    out = asyncio.run(rr._enrich_proposal(p, imap, "movie"))
    check("same-title same-service twins resolve by arr_id "
          "(dog proposal fetches the dog tmdb id)",
          seen["tmdb_id"] == 1422096 and out["genres"] == "Horror"
          and out["synopsis"] == "synopsis-of-1422096")

    p2 = {**p, "arr_id": 37548}
    out2 = asyncio.run(rr._enrich_proposal(p2, imap, "movie"))
    check("the twin gets ITS own id and genres",
          seen["tmdb_id"] == 1381027
          and out2["genres"].startswith("Thriller"))

    p3 = {"title": "Good Boy", "category": "movie"}
    out3 = asyncio.run(rr._enrich_proposal(p3, imap, "movie"))
    check("proposal without arr_id still resolves via the legacy title key",
          seen["tmdb_id"] in (1422096, 1381027) and out3["synopsis"])
finally:
    rr._fetch_tmdb = _real_fetch

# The scheduler path must use the same builder — a hand-rolled dict there
# would silently reintroduce the collision.
sch = (Path(__file__).resolve().parents[1] / "src/services/scheduler.py").read_text(encoding="utf-8")
check("scheduler builds its item map via build_proposal_item_map",
      "build_proposal_item_map(cat_items)" in sch)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
