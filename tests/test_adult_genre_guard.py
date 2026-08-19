"""Adult-genre merge guard (the Batman Beyond poisoning).

TVDB/arr genre lists are community-editable: 'Hentai' arrived on the
DCAU series Batman Beyond via Sonarr's genre list, survived the raw
merge, and the old curator confabulated an explicit profile from that
one word (doc sonarr:629, found in owner search round 10). An adult
GENRE now survives _merge_raw_metadata only when an anime-database
source lists it itself or the content rating (Rx/Hentai) confirms it.

    python tests/test_adult_genre_guard.py
"""
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


from src.services.media_enricher import _merge_raw_metadata

# 1) The Batman shape: TMDB primary whose genre list got polluted by the
#    arr merge — no anime-DB source, no Rx rating → Hentai dropped.
m = _merge_raw_metadata(
    {"source": "tmdb", "title": "Batman Beyond",
     "genres": ["Animation", "Action & Adventure", "Sci-Fi & Fantasy", "Hentai"],
     "overview": "A teenage Batman in a cyberpunk future."})
check("unconfirmed 'Hentai' from a non-anime source is dropped",
      "Hentai" not in m["genres"] and "Animation" in m["genres"])

# 2) AniList itself lists the genre → kept (real hentai stays real).
m = _merge_raw_metadata(
    {"source": "anilist", "title": "Ingoku Danchi",
     "genres": ["Hentai", "Drama"], "overview": "x" * 50})
check("AniList-listed 'Hentai' survives", "Hentai" in m["genres"])

# 3) Supplement anime-DB confirmation also counts.
m = _merge_raw_metadata(
    {"source": "tmdb", "title": "X", "genres": ["Animation", "Hentai"],
     "overview": "y" * 50},
    {"source": "jikan", "genres": ["Hentai"], "synopsis": "z" * 50})
check("Jikan supplement confirms the genre", "Hentai" in m["genres"])

# 4) An Rx content rating confirms even without an anime-DB genre list.
m = _merge_raw_metadata(
    {"source": "tmdb", "title": "Y", "genres": ["Hentai"], "overview": "p" * 50},
    {"source": "jikan", "rating": "Rx - Hentai", "synopsis": "q" * 50})
check("Rx content rating confirms", "Hentai" in m["genres"])

# 5) Ecchi is deliberately NOT guarded (mild, common, low poison risk).
m = _merge_raw_metadata(
    {"source": "tmdb", "title": "Z", "genres": ["Ecchi", "Comedy"],
     "overview": "r" * 50})
check("'Ecchi' passes unguarded", "Ecchi" in m["genres"])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
