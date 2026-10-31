"""Needs attention and maintenance that never reached zero (2026-10-23).

Measured on the owner's data:

- 65 "wrong entity" findings, 39 of them false: a shared TMDB / TVDB / IMDb
  id says it is the same work ("Mad Max 2" / "The Road Warrior", The Evil
  Dead 1983 / 1981, "M" / "M (1931)"). Anime keep the title check - their
  data comes from AniList, whose entry can be another work while the TMDB
  id is still the arr's - but now ask AniList's own names (title and
  synonyms) in the offline snapshot.
- 19 anime "not found in 2+ rounds" whose English arr title AniList's search
  misses; the snapshot's names resolve 10 of them to the right work.
- Wikidata's top-up never settled 56 titles: an anime filed under raw:anime:
  was looked up under raw:show:, and a title with one stamped row of two
  never wrote the other.

    python tests/test_attention_patterns.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.cache.metadata_cache import MetadataCache  # noqa: E402
from src.routers import enrichment as en  # noqa: E402
from src.services import anime_offline as ao  # noqa: E402
from src.services import archive_backfill as ab  # noqa: E402
from src.services import wikidata as wd  # noqa: E402

from _checks import check  # noqa: E402


TMP = Path(tempfile.mkdtemp(prefix="curatarr-attention-"))
saved_offline = ao.OFFLINE_DB_PATH
ao.OFFLINE_DB_PATH = TMP / "anime_offline.db"


def entry(title, year, anilist, synonyms=()):
    return {"title": title, "synonyms": list(synonyms), "animeSeason": {"year": year},
            "sources": [f"https://anilist.co/anime/{anilist}"] if anilist else [],
            "type": "TV", "episodes": 12}


ao._rebuild([
    entry("Tengoku Daimakyo", 2023, 155783, ["Heavenly Delusion"]),
    entry("Golgo 13: The Professional", 1983, 1760, ["Golgo 13"]),
    entry("Hikyou Tanken Fam & Ihrlie", 1995, 1181, ["Ruin Explorers"]),
    entry("Shikabane Hime: Aka", 2008, 4581, ["Corpse Princess"]),
    entry("Shikabane Hime: Kuro", 2009, 5034, ["Corpse Princess"]),
    entry("Godannar", 2003, None),
    entry("Shinkon Gattai Godannar!!", 2003, 1103, ["Godannar"]),
])

try:
    # ── the wrong-entity check ──────────────────────────────────────────────
    div = en._entity_divergence_reason
    check("a shared TMDB id: the same film, whatever the title says",
          div({"title": "The Road Warrior", "year": 1981, "tmdb_id": 8810}, "movie",
              {"title": "Mad Max 2", "year": 1981, "tmdb_id": 8810}) is None)
    check("...or the year (a regional release)",
          div({"title": "The Evil Dead", "year": 1981, "tmdb_id": 764}, "movie",
              {"title": "The Evil Dead", "year": 1983, "tmdb_id": 764}) is None)
    check("...the same for IMDb ids, case aside",
          div({"title": "X", "year": 1990, "imdb_id": "TT0100"}, "show",
              {"title": "Completely Else", "year": 2010, "imdb_id": "tt0100"}) is None)
    check("a shared id that differs is a wrong entity outright",
          div({"title": "Blown Away", "year": 1994, "tmdb_id": 1}, "movie",
              {"title": "Blown Away", "year": 1994, "tmdb_id": 2}) == "wrong_entity:id")
    check("no id on both sides: the title check, a trailing (year) ignored",
          div({"title": "M (1931)", "year": 1931}, "movie", {"title": "M", "year": 1931}) is None)
    check("...and a really different title still flagged",
          div({"title": "Something Unrelated", "year": 2001}, "movie",
              {"title": "Spirited Away", "year": 2001}) == "wrong_entity:title")

    check("anime: the arr's English title among AniList's names, right year: fine",
          div({"title": "Tengoku Daimakyo", "year": 2023, "anilist_id": 155783, "tmdb_id": 9},
              "anime", {"title": "Heavenly Delusion", "year": 2023, "tmdb_id": 9}) is None)
    check("anime: a TMDB match does NOT clear an AniList entry for another work",
          div({"title": "Golgo 13: The Professional", "year": 1983, "anilist_id": 1760, "tmdb_id": 5},
              "anime", {"title": "Golgo 13", "year": 2008, "tmdb_id": 5}) == "wrong_entity:year")
    check("anime without a snapshot entry: the plain check decides",
          div({"title": "Unknown Show", "year": 2020, "anilist_id": 999999}, "anime",
              {"title": "Unknown Show", "year": 2020}) is None)

    # ── the offline name lookup ─────────────────────────────────────────────
    check("an English arr title resolves through the snapshot's names",
          ao.anilist_for_title("Ruin Explorers", 1995) == 1181
          and ao.anilist_for_title("Ruin Explorers", 1996) == 1181)
    check("...only within a year", ao.anilist_for_title("Ruin Explorers", 1999) is None)
    check("...never without a year to check", ao.anilist_for_title("Ruin Explorers", None) is None)
    check("...never when two works share the name (Aka / Kuro)",
          ao.anilist_for_title("Corpse Princess", 2008) is None)
    check("...an entry without an AniList id is skipped, not a tie",
          ao.anilist_for_title("Godannar", 2003) == 1103)
    names, year = ao.names_for(155783)
    check("names_for: title and synonyms, normalised, with the year",
          {"tengoku daimakyo", "heavenly delusion"} <= names and year == 2023)
    src = (ROOT / "src/services/media_enricher.py").read_text(encoding="utf-8")
    check("both anime resolution paths fall back to the snapshot after anime-lists",
          src.count("anilist_id = await _offline_anilist_id(title, year) or anilist_id") == 2)
finally:
    ao.OFFLINE_DB_PATH = saved_offline

# ── wikidata: filing and siblings ───────────────────────────────────────────
cache = MetadataCache(TMP / "enrichment.db")
cache.set_cache("raw:anime:sonarr:1", {"title": "Some Anime", "media_type": "tv",
                                       "imdb_id": "tt1000001"}, days=30)
entries = {r["_cache_id"]: r for r in ab._live_raw_entries(cache)}
check("a raw row is classified by the category it is FILED under (TMDB says 'tv')",
      entries["sonarr:1"]["_media_type"] == "anime")

cache.set_cache("raw:movie:radarr:7", {"title": "Twin", "media_type": "movie", "imdb_id": "tt2000002",
                                       "wikidata_checked": True, "wikidata_v": wd._LOGIC_VERSION,
                                       "wikidata": {"award_count": 3}}, days=30)
cache.set_cache("raw:movie:Twin", {"title": "Twin", "media_type": "movie", "imdb_id": "tt2000002"},
                days=30)
asked = []


async def fake_fetch(imdb, **k):
    asked.append(imdb)
    return {"award_count": 99}

saved_fetch = wd.fetch_wikidata_facts
wd.fetch_wikidata_facts = fake_fetch
try:
    res = asyncio.run(wd.topup_wikidata("Twin", "movie", imdb_id="tt2000002",
                                        cache_id="radarr:7", cache=cache))
    twin = cache.get_cache("raw:movie:Twin")["response"]
    check("a stamped sibling no longer stops the write: the other row gets its facts",
          twin.get("wikidata_v") == wd._LOGIC_VERSION and twin.get("wikidata") == {"award_count": 3})
    check("...reused from the sibling, without asking Wikidata again", asked == [] and res is True)
    res = asyncio.run(wd.topup_wikidata("Twin", "movie", imdb_id="tt2000002",
                                        cache_id="radarr:7", cache=cache))
    check("all rows stamped: nothing to do", res is False and asked == [])
    asyncio.run(wd.topup_wikidata("Some Anime", "anime", imdb_id="tt1000001",
                                  cache_id="sonarr:1", cache=cache))
    check("the anime row is found under raw:anime: and stamped",
          cache.get_cache("raw:anime:sonarr:1")["response"].get("wikidata_v") == wd._LOGIC_VERSION
          and asked == ["tt1000001"])
    check("...so coverage counts nothing outstanding",
          ab.pending("wikidata", cache=cache) == [])
finally:
    wd.fetch_wikidata_facts = saved_fetch
    cache.close()

# ── music from the Plex index: "no id in the arr" cannot apply ──────────────
from src.services import enrichment_state as ES  # noqa: E402
from src.services import kb_overview as kb  # noqa: E402
plex_artist = {"source": "plex", "mbid": None, "title": "Armin van Buuren & Vini Vici"}
lidarr_artist = {"source": "lidarr", "mbid": None, "title": "Some Band"}
check("a Plex-index artist without an MBID is not 'no id in the arr' (Plex has none to give)",
      kb.has_external_id(plex_artist) is True
      and "no_external_id" not in ES.attention_reasons("never_processed", None,
                                                       has_external_id=kb.has_external_id(plex_artist)))
check("...a Lidarr artist without one still is", kb.has_external_id(lidarr_artist) is False
      and "no_external_id" in ES.attention_reasons("never_processed", None,
                                                   has_external_id=kb.has_external_id(lidarr_artist)))
kb_src = (ROOT / "src/services/kb_overview.py").read_text(encoding="utf-8")
en_src = (ROOT / "src/routers/enrichment.py").read_text(encoding="utf-8")
check("the page and the sidebar count share the one rule",
      "has_external_id=has_external_id(it)" in kb_src and "has_external_id=_kb.has_external_id(i)" in en_src
      and '"source": raw.get("_source") or svc' in kb_src)
row = en._serialize_item({"plex_rating_key": "lidarr:1", "service": "lidarr", "arr_id": 1,
                          "category": "music", "title": "X", "state": "never_processed"})
check("a never-processed row is explained as never processed, not as 'a full profile was written'",
      row["reason"] == ES.STATE_DEFINITIONS["never_processed"]["explainer"])

check.done()
