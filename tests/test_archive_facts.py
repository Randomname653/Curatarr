"""The two sources behind the archive pillar: Wikidata facts and reception.

    python tests/test_archive_facts.py

Offline — no network, no model. The live behaviour of both was verified by
hand against real titles; what is frozen here is the shape.

The reception check exists because one exit from build_reception returned two
values where every other returned four. The caller unpacked four, so every
film and show raised — and the exception was logged at debug level, which
made a total failure look like a slow walker for months. An arity mismatch
inside one function is invisible to the eye and trivial to assert.
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── every exit from build_reception must agree on how many values it returns ──

tree = ast.parse((ROOT / "src/services/reception.py").read_text(encoding="utf-8"))
fn = next(n for n in ast.walk(tree)
          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
          and n.name == "build_reception")
arities = set()
for node in ast.walk(fn):
    if isinstance(node, ast.Return) and node.value is not None:
        # nested helpers defined inside would skew this; there are none today
        arities.add(len(node.value.elts) if isinstance(node.value, ast.Tuple) else 1)
check(f"build_reception returns one arity everywhere (found {sorted(arities)})",
      len(arities) == 1)
check("...and it is the four the caller unpacks", arities == {4})

src = (ROOT / "src/services/reception.py").read_text(encoding="utf-8")
check("the caller still unpacks four", "rec, rels, staff, finale = await build_reception" in src)

# ── Wikidata: shaping facts for a judge ─────────────────────────────────────

from src.services.wikidata import (
    _LOGIC_VERSION, _QUERY, _is_major, format_wikidata_line)

check("major awards are recognised",
      _is_major("Academy Award for Best Picture") and _is_major("Palme d'Or")
      and _is_major("Primetime Emmy Award for Outstanding Drama Series"))
check("a local festival prize is not",
      not _is_major("Fantasporto Audience Award")
      and not _is_major("Best Newcomer, Someplace Film Week"))
check("nothing is claimed about an empty award", not _is_major(""))

check("an adaptation reads as one",
      format_wikidata_line({"source_author": "John le Carré",
                            "source_work": "The Night Manager"})
      == "adapted from The Night Manager by John le Carré")
check("a known author without a named work still reads",
      "by Joseph Conrad" in format_wikidata_line({"source_author": "Joseph Conrad"}))
check("awards are named, not counted",
      format_wikidata_line({"awards": ["Palme d'Or", "Academy Award for Best Sound"]})
      == "won Palme d'Or, Academy Award for Best Sound")
check("a title with only minor awards reports the count honestly",
      format_wikidata_line({"award_count": 4}) == "4 recorded awards (none major)")
check("both halves combine",
      format_wikidata_line({"source_author": "Mario Puzo", "source_work": "The Godfather",
                            "awards": ["Academy Award for Best Picture"]})
      == "adapted from The Godfather by Mario Puzo; won Academy Award for Best Picture")
check("nothing known -> no line", format_wikidata_line({}) == "")

check("the query version is derived from the query itself",
      _LOGIC_VERSION == __import__("hashlib").sha1(
          _QUERY.encode("utf-8")).hexdigest()[:8])
check("the query asks for the source and its author",
      "wdt:P144" in _QUERY and "wdt:P50" in _QUERY and "wdt:P166" in _QUERY)

# ── wiring ──────────────────────────────────────────────────────────────────

me = (ROOT / "src/services/media_enricher.py").read_text(encoding="utf-8")
check("the facts reach the judge as their own line, beside the distilled prose",
      'add("On record"' in me and '"wikidata":             raw.get("wikidata")' in me)

re_ = (ROOT / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
check("and are warmed before the verdict with the other two sources",
      "topup_wikidata" in re_ and "topup_reception" in re_
      and "topup_significance" in re_)

check("a standalone runner exists to clear the backlog",
      (ROOT / "scripts/facts_speedrunner.py").exists())

# -- the backfill offer, and when it stops being one -----------------------
# A fresh install has none of this data and the daily tick fills it at a pace
# suited to a library that is already running. The panel offers the walkers
# on demand and removes itself once a source is covered, because a button for
# a finished job is clutter.

from src.services import archive_backfill as ab

check("every source declares a label, a blurb and a completeness test",
      all(set(spec) >= {"label", "blurb", "done"} for spec in ab.SOURCES.values()))
check("every archive source is covered",
      set(ab.SOURCES) == {"external_ids", "significance", "reception",
                          "wikidata", "omdb"})

# A title with no IMDb id can never gain OMDb or Wikidata; counting it as
# outstanding would pin coverage below the threshold and leave a button that
# cannot finish its own job.
check("a title that cannot be looked up counts as settled",
      ab.SOURCES["omdb"]["done"]({"title": "x"})
      and ab.SOURCES["wikidata"]["done"]({"title": "x"}))
check("...but one that CAN be looked up and has not been does not",
      not ab.SOURCES["omdb"]["done"]({"title": "x", "imdb_id": "tt1"})
      and not ab.SOURCES["wikidata"]["done"]({"title": "x", "imdb_id": "tt1"}))

check("the threshold is a real ceiling, not 100%", 50 < ab.THRESHOLD_PCT < 100)

_router = (ROOT / "src/routers/enrichment.py").read_text(encoding="utf-8")
for route in ("/backfill-status", "/backfill/{source}", "/backfill/{source}/stop"):
    check(f"endpoint {route} exists", f'"{route}"' in _router)
check("starting a backfill is admin-only",
      "async def start_backfill" in _router and "require_admin" in _router)
check("a running backfill can be asked to stop mid-run",
      "should_stop=lambda: source not in _backfill_running" in _router)

_html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
check("the panel exists and is refreshed with the page",
      'id="backfill-panel"' in _html and "loadBackfillPanel();" in _html)
check("it removes itself when nothing is worth offering",
      "if (!d || !d.any_offer)" in _html)

from src.services import app_context
check("the curator knows the panel disappears on purpose",
      "Finish the backfill" in app_context.APP_MAP_BLOCK
      and "disappears on its own" in app_context.APP_MAP_BLOCK)

_cust = (ROOT / "src/services/data_custodian.py").read_text(encoding="utf-8")
check("a daily tick carries the remainder for the new source",
      "custodian_wikidata" in _cust)

# -- ids the *arr already had ---------------------------------------------
# OMDb and Wikidata both key on the IMDb id, and a quarter of the library had
# none: anime is enriched from AniList and never touches TMDB. Sonarr knew the
# id for 1,833 of those titles all along. Matching is timid on purpose - a
# wrong id is not a gap, it is a confident statement about another work.

from src.services.external_ids import _index, _norm, _unprefixed

check("punctuation is not identity", _norm("Re:ZERO") == _norm("Re Zero"))
check("nothing is normalised out of nothing", _norm("") == "" and _norm(None) == "")

one = _index([{"title": "Solo Leveling", "imdbId": "tt1", "tvdbId": 7}])
check("a single library entry is claimed", one[_norm("Solo Leveling")]["imdb_id"] == "tt1")

alt = _index([{"title": "Kimetsu no Yaiba", "imdbId": "tt2",
               "alternateTitles": [{"title": "Demon Slayer"}]}])
check("alternate titles are indexed too — anime is filed under romanisations",
      _norm("Demon Slayer") in alt)

clash = _index([{"title": "Twins", "imdbId": "tt3"}, {"title": "Twins", "imdbId": "tt4"}])
check("two entries sharing a name are refused, not guessed",
      _norm("Twins") not in clash)

same = _index([{"title": "Solo Leveling", "imdbId": "tt1"},
               {"title": "solo  leveling!", "imdbId": "tt1"}])
check("...but the same work listed twice is still claimed",
      same.get(_norm("Solo Leveling"), {}).get("imdb_id") == "tt1")

check("an entry with no ids at all contributes nothing",
      _index([{"title": "Nameless"}]) == {})

check("the stored key is unprefixed before writing back",
      _unprefixed("v2:raw:anime:x") == "raw:anime:x"
      and _unprefixed("raw:anime:x") == "raw:anime:x")

# -- and the year, which decides which of two same-named works it is -------
# Audited against an independent anidb->tvdb mapping, the name step agreed on
# 1,788 of 1,796 checkable anime. The one true error was a cached entry dated
# 2013 (the CGI film) claiming the id of the 1978 series of the same name.
# Both records carried a year; nothing looked at it.

from src.services.external_ids import FAMILIES, _by_id, _match, _year_ok

check("a year can only veto, never invent agreement",
      _year_ok(None, 1978) and _year_ok(2013, None) and _year_ok(None, None))
check("a season straddling New Year still matches", _year_ok(2013, 2014))
check("thirty-five years apart is a different work", not _year_ok(2013, 1978))

_SERIES = [{"title": "Space Pirate Captain Harlock", "year": 1978,
            "tvdbId": 80886, "imdbId": "tt0182646"}]
_FILMS = [{"title": "Space Pirate Captain Harlock", "year": 2013,
           "tmdbId": 149871, "imdbId": "tt2668134"}]


def _idx(series, films):
    return {"tv": {"by_name": _index(series), "by_id": _by_id(series)},
            "movie": {"by_name": _index(films), "by_id": _by_id(films)}}


BOTH, TV_ONLY = _idx(_SERIES, _FILMS), _idx(_SERIES, [])

check("anime is looked for in Radarr as well as Sonarr — anime films live there",
      FAMILIES["anime"] == ("tv", "movie") and FAMILIES["show"] == ("tv",))

rec, how = _match({"title": "Space Pirate Captain Harlock", "year": 2013},
                  BOTH, FAMILIES["anime"])
check("the film's year picks the film, not the same-named series",
      how == "title" and rec["imdb_id"] == "tt2668134")

rec, how = _match({"title": "Space Pirate Captain Harlock", "year": 1978},
                  BOTH, FAMILIES["anime"])
check("...and the series' year picks the series", rec["imdb_id"] == "tt0182646")

rec, how = _match({"title": "Space Pirate Captain Harlock", "year": 2013},
                  TV_ONLY, FAMILIES["anime"])
check("with only the wrong-year work on offer, nothing is claimed",
      rec is None and how is None)

rec, how = _match({"title": "Space Pirate Captain Harlock"},
                  TV_ONLY, FAMILIES["anime"])
check("an entry with no year of its own is still matched by name",
      how == "title" and rec["imdb_id"] == "tt0182646")

# ONE PIECE: the series is 1999 and the first film is 2000, so the film
# survives the one-year slack and both candidates stand. An exact year is the
# stronger claim and takes it.
_OP_TV = [{"title": "ONE PIECE", "year": 1999, "tvdbId": 81797,
           "imdbId": "tt0388629"}]
_OP_FILM = [{"title": "ONE PIECE", "year": 2000, "tmdbId": 39121,
             "imdbId": "tt0814243"}]
rec, how = _match({"title": "ONE PIECE", "year": 1999},
                  _idx(_OP_TV, _OP_FILM), FAMILIES["anime"])
check("an exact year beats a neighbour that only survived the slack",
      how == "title" and rec["imdb_id"] == "tt0388629")

# Space Adventure Cobra is a 1982 series AND a 1982 film. Nothing separates
# them, so nothing is claimed.
_SAME = [{"title": "Space Adventure Cobra", "year": 1982, "tvdbId": 82971,
          "imdbId": "tt0235138"}]
_SAME_F = [{"title": "Space Adventure Cobra", "year": 1982, "tmdbId": 1,
            "imdbId": "tt0163494"}]
rec, how = _match({"title": "Space Adventure Cobra", "year": 1982},
                  _idx(_SAME, _SAME_F), FAMILIES["anime"])
check("two works of the same name AND the same year stay refused",
      rec is None and how is None)

rec, how = _match({"title": "A Name Nobody Files It Under", "tvdb_id": 80886},
                  BOTH, FAMILIES["anime"])
check("an id join needs no title at all, and is reported as exact",
      how == "tvdb" and rec["imdb_id"] == "tt0182646")

check("an id two works claim identifies nothing",
      _by_id([{"title": "A", "tvdbId": 5, "imdbId": "tt1"},
              {"title": "B", "tvdbId": 5, "imdbId": "tt2"}]) == {})

check("how the match was made is recorded, not merely that it was",
      'fields["ids_from_arr"] = how' in
      (ROOT / "src/services/external_ids.py").read_text(encoding="utf-8"))
check("claims made under the older, looser rule are re-judged",
      "def _recheck" in
      (ROOT / "src/services/external_ids.py").read_text(encoding="utf-8"))

check("the id harvest is offered before the sources that need it",
      list(ab.SOURCES).index("external_ids") < list(ab.SOURCES).index("omdb")
      and list(ab.SOURCES).index("external_ids") < list(ab.SOURCES).index("wikidata"))

# -- addressing a title, rather than guessing where it lives ----------------
# Raw entries are filed under the LIBRARY's title while the stored "title" is
# the enriched one, so "Frieren: Beyond Journey's End" holds a row titled
# "Sousou no Frieren", and "Dan Da Dan" one titled "DAN DA DAN". Every top-up
# rebuilt its lookup key from title[:40], missed, found no row to write, and
# returned "nothing to do" — so the walker reported success and the title
# stayed outstanding for ever. Measured: 342 of 400 pending Wikidata titles
# were unreachable; carrying the key the row was READ from leaves 18.

import inspect
from src.services.media_enricher import topup_omdb, topup_significance
from src.services.reception import topup_reception
from src.services.wikidata import topup_wikidata

check("the key a row was read from survives into the walker's work list",
      '"cache_id": raw.get("_cache_id")' in
      (ROOT / "src/services/archive_backfill.py").read_text(encoding="utf-8"))

for fn in (topup_significance, topup_omdb, topup_reception, topup_wikidata):
    check(f"{fn.__name__} accepts it",
          "cache_id" in inspect.signature(fn).parameters)

_ab = (ROOT / "src/services/archive_backfill.py").read_text(encoding="utf-8")
for mod in ("src/services/media_enricher.py", "src/services/reception.py",
            "src/services/wikidata.py"):
    src_ = (ROOT / mod).read_text(encoding="utf-8")
    check(f"{Path(mod).name} tries it FIRST, before any rebuilt key",
          "(cache_id, anilist_id" in src_)

# Titles carry colons — "Code Geass: Lelouch of the Rebellion" — so the split
# that recovers the key tail has to be bounded.
check("a key tail survives colons in the title",
      ab._key_tail("v2:raw:anime:Code Geass: Lelouch of the Rebellion")
      == "Code Geass: Lelouch of the Rebellion")
check("...with or without the cache-version prefix",
      ab._key_tail("raw:movie:Alien") == "Alien")
check("a malformed key yields nothing rather than a wrong address",
      ab._key_tail("raw:movie") == "" and ab._key_tail("nonsense") == "")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
