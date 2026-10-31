"""Recommendation posters for titles written with their year (2026-10-25,
routers/recommendations.poster_query).

The curator writes a discovery pick as "The Cycle (2026)" and the arr names
some titles "Given (2021)". Searched with the year in it, TMDB found nothing
and the card showed a "?": every movie discovery and 7 of 10 anime picks on
the owner's cache. A music pick names "Artist - Track".

    python tests/test_rec_posters.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.routers.recommendations as recs  # noqa: E402

from _checks import check  # noqa: E402


q = recs.poster_query
check("the year leaves the title and becomes the hint",
      q("The Cycle (2026)", "movie") == ("The Cycle", 2026) and q("Given (2021)", "anime") == ("Given", 2021))
check("a title without a year stays as it is",
      q("Inception", "movie") == ("Inception", None) and q("Blade Runner 2049", "movie") == ("Blade Runner 2049", None))
check("a year inside the title is no suffix", q("1917", "movie") == ("1917", None)
      and q("(500) Days of Summer", "movie") == ("(500) Days of Summer", None))
check("a music pick is its artist", q("FKA twigs - Cellophane", "music") == ("FKA twigs", None)
      and q("Apparat", "music") == ("Apparat", None))
check("a dash in a film title stays", q("Spider-Man - No Way Home", "movie") == ("Spider-Man - No Way Home", None))

seen = []


async def fake(title, category, **kw):
    seen.append((title, category, kw.get("year")))
    return "poster", "synopsis"


recs._fetch_tmdb = fake
asyncio.run(recs.fetch_rec_art("The Cycle (2026)", "movie"))
asyncio.run(recs.fetch_rec_art("Possession", "movie", 1981))
check("the lookup gets the bare title and the year", seen == [("The Cycle", "movie", 2026), ("Possession", "movie", 1981)])

sched = (ROOT / "src/services/scheduler.py").read_text(encoding="utf-8")
check("the cache refresh looks posters up this way",
      'await fetch_rec_art(\n                                rec.get("title", ""), cat, rec.get("year"))' in sched)

check.done()
