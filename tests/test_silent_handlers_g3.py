"""Silent-handler sweep, group 3: the guards that now fail closed.

Each check fails on the code before this change:
  * the judge's taste line: an unreadable taste profile, or one that cannot
    be rendered, defers the title (owner_signal_unknown) instead of judging
    with the Ego pillar blind
  * the judge's FORM guard: a cache failure behind the documentary /
    spoken-word line defers the title (significance_unchecked) instead of
    judging a documentary by the yardstick of drama
  * proactive nudges: an unreadable opt-out list disables every trigger
    instead of switching all of them back on

    python tests/test_silent_handlers_g3.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = FAIL = 0


def check(name, cond, info=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  [{info}]" if info is not None else ""))


import _harness  # noqa: E402,F401  (first: temp stores and .env, never the live data/)
import src.cache.metadata_cache as mcache  # noqa: E402
import src.services.app_state as app_state  # noqa: E402
import src.services.episodic_memory as em  # noqa: E402
import src.services.media_enricher as me  # noqa: E402
import src.services.recommendations_engine as reng  # noqa: E402
import src.services.size_norms as sn  # noqa: E402
import src.services.subtitle_signals as ss  # noqa: E402
from src.config import settings  # noqa: E402
from src.database.models import TasteVectorEntry  # noqa: E402
from src.services import pillars  # noqa: E402
from src.services import proactive_messages as pm  # noqa: E402

# ── stubs: nothing below reaches the network or a real database ─────────────


async def _no_considerations(*a, **k):
    return []


async def _verified(*a, **k):
    # significance checked and present: the only way significance_unchecked
    # can turn on below is the guard under test
    return {"title": "Mayday", "year": 2003, "genres": ["Documentary"],
            "overview": "Air-crash investigations, reconstructed.",
            "significance": "Long-running factual series.",
            "significance_checked": True}


async def _wiki(*a, **k):
    return ""


em.retrieve_considerations = _no_considerations
me.ensure_verified_data = _verified
me.fetch_wikipedia_summary = _wiki
ss.subtitle_facts = lambda *a, **k: ""
sn.tech_profile_for = lambda **k: None
settings.PRINCIPLES_ENABLED = False

ITEM = {"title": "Mayday", "year": 2003, "genres": ["Documentary"]}
CLEAN_DBF = {"ow": None, "ow_unknown": False, "others": [],
             "taste_summary": "", "taste_unknown": False,
             "fb_blob": None, "fb_unknown": False}


def _evidence(dbf=None, category="show"):
    orig = pillars._read_db_facts
    pillars._read_db_facts = lambda *a, **k: dict(dbf or CLEAN_DBF)
    try:
        return asyncio.run(pillars.build_evidence(dict(ITEM), 1, category, db=object()))
    finally:
        pillars._read_db_facts = orig


# ── 1. the taste profile is owner evidence ──────────────────────────────────


class _TasteBrokenDB:
    """Every read answers except the taste-vector one."""
    class _Q:
        def filter(self, *a, **k):
            return self

        def first(self):
            return None

    def query(self, model, *a, **k):
        if model is TasteVectorEntry:
            raise RuntimeError("database is locked")
        return self._Q()


_ow, _others = pillars._owner_watch, pillars._other_users_watch
pillars._owner_watch = lambda *a, **k: None
pillars._other_users_watch = lambda *a, **k: []
try:
    dbf = pillars._read_db_facts(_TasteBrokenDB(), ITEM, 1, "show", "Mayday")
finally:
    pillars._owner_watch, pillars._other_users_watch = _ow, _others
check("an unreadable taste profile is reported, not read as 'no taste'",
      dbf.get("taste_unknown") is True, dbf)
check("...and the other reads still answered", dbf["fb_unknown"] is False)

reng_cached = reng._get_cached_rating
reng._get_cached_rating = lambda *a, **k: (None, None, [])
try:
    ev = _evidence()
    check("baseline: clean evidence is judgeable",
          not ev["flags"]["owner_signal_unknown"]
          and not ev["flags"]["significance_unchecked"], ev["flags"])

    ev = _evidence({**CLEAN_DBF, "taste_unknown": True})
    check("build_evidence defers a title whose taste profile could not be read",
          ev["flags"]["owner_signal_unknown"], ev["flags"])

    _taste = reng._taste_section

    def _boom(*a, **k):
        raise KeyError("movie")

    reng._taste_section = _boom
    try:
        ev = _evidence({**CLEAN_DBF, "taste_summary": "Loves slow cinema."})
    finally:
        reng._taste_section = _taste
    check("a taste profile that cannot be rendered defers the title too",
          ev["flags"]["owner_signal_unknown"], ev["flags"])
finally:
    reng._get_cached_rating = reng_cached

# ── 2. the FORM guard is protective evidence ────────────────────────────────


class _CacheDown:
    def __init__(self, *a, **k):
        raise RuntimeError("enrichment cache unavailable")


_MC = mcache.MetadataCache
mcache.MetadataCache = _CacheDown
try:
    check("the pitch path still reads a cache failure as 'nothing cached'",
          reng._get_cached_rating(ITEM, "show") == (None, None, []))
    try:
        reng._get_cached_rating(ITEM, "show", strict=True)
        strict_raised = False
    except RuntimeError:
        strict_raised = True
    check("strict callers see the cache failure", strict_raised)

    ev = _evidence(category="show")
    check("a documentary whose form could not be checked is deferred",
          ev["flags"]["significance_unchecked"], ev["flags"])
    ev = _evidence(category="music")
    check("...and so is a music artist (the spoken-word guard)",
          ev["flags"]["significance_unchecked"], ev["flags"])
finally:
    mcache.MetadataCache = _MC

# ── 3. an unreadable opt-out list keeps nudges off ──────────────────────────

_get_state = app_state.get_state
try:
    app_state.get_state = lambda key: '["night_owl"]'
    check("a readable opt-out list is honoured as stored",
          pm.get_disabled_triggers(7) == {"night_owl"})
    app_state.get_state = lambda key: None
    check("no stored list means every trigger is on", pm.get_disabled_triggers(7) == set())
    app_state.get_state = lambda key: '["night_owl", '
    check("a corrupt opt-out list disables every trigger, never none",
          pm.get_disabled_triggers(7) == pm.TRIGGER_TYPE_NAMES)
    app_state.get_state = lambda key: '{"night_owl": true}'
    check("...and so does a list of the wrong shape",
          pm.get_disabled_triggers(7) == pm.TRIGGER_TYPE_NAMES)
finally:
    app_state.get_state = _get_state

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
