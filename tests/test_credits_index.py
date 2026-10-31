"""The credits index: who made what, queryable and durable.

Directors, writers, cast, voice actors, studios and companies lived only as
JSON inside the raw API cache (90-day TTL), and TMDB's production
companies, networks, collection and person ids were fetched and dropped.
The TMDB fetch now keeps them, and a daily walker files every live raw
document into durable tables.

    python tests/test_credits_index.py
"""
import asyncio
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import sessionmaker  # noqa: E402

import src.services.credits_index as ci  # noqa: E402
import src.services.media_enricher as me  # noqa: E402
from src.cache.metadata_cache import MetadataCache  # noqa: E402
from src.database.models import Base, MediaCompany, MediaCredit, MediaWork  # noqa: E402

from _checks import check  # noqa: E402


# ── the TMDB fetch keeps what it used to drop ───────────────────────────────
PAYLOADS = {
    "/movie/949": {"title": "Heat", "release_date": "1995-12-15", "genres": [],
                   "production_companies": [{"id": 508, "name": "Regency Enterprises"},
                                            {"id": 675, "name": "Forward Pass"}],
                   "belongs_to_collection": None},
    "/movie/949/credits": {
        "cast": [{"id": 1158, "name": "Al Pacino", "character": "Vincent Hanna"},
                 {"id": 380, "name": "Robert De Niro", "character": "Neil McCauley"}],
        "crew": [{"id": 638, "name": "Michael Mann", "job": "Director"},
                 {"id": 638, "name": "Michael Mann", "job": "Screenplay"},
                 {"id": 2, "name": "Elliot Goldenthal", "job": "Original Music Composer"},
                 {"id": 3, "name": "Gaffer Guy", "job": "Gaffer"}]},
    "/movie/949/keywords": {"keywords": []},
    "/movie/949/external_ids": {"imdb_id": "tt0113277"},
}


async def fake_tmdb_get(client, path, params=None):
    return PAYLOADS.get(path, {})


me._tmdb_get = fake_tmdb_get
raw = asyncio.run(me.fetch_tmdb_full(949, "movie"))
roles = {(c["role"], c["name"]) for c in raw["credits"]}
check("the fetch keeps directors, writers, the composer and the billed cast, with ids",
      {("director", "Michael Mann"), ("writer", "Michael Mann"), ("composer", "Elliot Goldenthal"),
       ("cast", "Al Pacino"), ("cast", "Robert De Niro")} <= roles
      and ("cast", "Al Pacino") in roles and all(c["id"] for c in raw["credits"]))
check("...a crew job nobody asks about stays out", not any(c["name"] == "Gaffer Guy" for c in raw["credits"]))
check("...the cast with billing order and character",
      [(c["name"], c["ord"], c["character"]) for c in raw["credits"] if c["role"] == "cast"]
      == [("Al Pacino", 0, "Vincent Hanna"), ("Robert De Niro", 1, "Neil McCauley")])
check("...production companies with ids", raw["companies"] == [
    {"kind": "production", "id": 508, "name": "Regency Enterprises"},
    {"kind": "production", "id": 675, "name": "Forward Pass"}])
check("...and the flat fields stay as they were", raw["director"] == "Michael Mann"
      and raw["cast"] == ["Al Pacino", "Robert De Niro"])
PAYLOADS["/tv/1399"] = {"name": "Show", "created_by": [{"id": 9, "name": "Creator One"}],
                        "networks": [{"id": 49, "name": "HBO"}],
                        "belongs_to_collection": {"id": 7, "name": "Saga"}}
tv = asyncio.run(me.fetch_tmdb_full(1399, "tv"))
check("a show keeps its creators, its networks and a collection",
      {"role": "creator", "id": 9, "name": "Creator One"} in tv["credits"]
      and {"kind": "network", "id": 49, "name": "HBO"} in tv["companies"]
      and tv["collection"] == {"id": 7, "name": "Saga"})

# ── what one raw document says ──────────────────────────────────────────────
check("names join across spellings", ci.name_key("Hayáo  Miyazaki") == ci.name_key("hayao miyazaki"))
legacy = ci.extract("movie", {"tmdb_id": 155, "title": "The Dark Knight", "year": 2008,
                              "director": "Christopher Nolan",
                              "writer": "Jonathan Nolan (screenplay), Christopher Nolan (screenplay), N/A",
                              "cast": ["Christian Bale", "Heath Ledger"], "source_author": "Bob Kane"})
check("an older row's names become credits, OMDb's parentheses and N/A dropped",
      [(p["role"], p["name"]) for p in legacy["people"]] == [
          ("director", "Christopher Nolan"), ("writer", "Jonathan Nolan"), ("writer", "Christopher Nolan"),
          ("author", "Bob Kane"), ("cast", "Christian Bale"), ("cast", "Heath Ledger")]
      and not legacy["structured"])
anime = ci.extract("anime", {"anilist_id": 1, "tmdb_id": 30991, "cast": ["Kouichi Yamadera"],
                             "studios": ["Sunrise"]})
check("anime is filed under AniList first; its cast is the voice cast; studios are companies",
      anime["ids"][0] == ("anilist", "1") and anime["people"][0]["role"] == "voice"
      and anime["companies"] == [{"kind": "studio", "company_id": None, "name": "Sunrise"}])
check("a row without an id, or without people and companies, says nothing",
      ci.extract("movie", {"title": "x", "cast": ["A"]}) is None
      and ci.extract("movie", {"tmdb_id": 5, "overview": "..."}) is None
      and ci.extract("music", {"tmdb_id": 5, "cast": ["A"]}) is None)

# ── the walk over a real cache file ─────────────────────────────────────────
tmp = Path(tempfile.mkdtemp()) / "enrichment.db"
cache = MetadataCache(tmp)
cache.set_cache("raw:movie:949", dict(raw, tmdb_id=949), days=90)
cache.set_cache("raw:movie:Heat", {"tmdb_id": 949, "title": "Heat", "cast": ["Al Pacino"],
                                   "director": "Michael Mann"}, days=90)
cache.set_cache("raw:anime:1", {"anilist_id": 1, "title": "Cowboy Bebop", "studios": ["Sunrise"]}, days=90)
cache.set_cache("raw:anime:sonarr:12", {"tmdb_id": 30991, "anilist_id": 1, "title": "Cowboy Bebop",
                                        "cast": ["Kouichi Yamadera", "Unshou Ishizuka"]}, days=90)
cache.set_cache("raw:show:old", {"tmdb_id": 77, "title": "Expired", "cast": ["X"]}, days=-1)
cache.set_cache("raw:music:artist", {"tmdb_id": 1, "cast": ["Y"]}, days=90)
works = {(w["category"], w["id_source"], w["work_id"]): w for w in ci.scan_cache(cache)}
check("one entry per title: the id row and the title row of Heat merge",
      ("movie", "tmdb", "949") in works and len([k for k in works if k[0] == "movie"]) == 1)
check("...the structured row's credits win over bare names",
      works[("movie", "tmdb", "949")]["structured"] and any(
          p["person_id"] == 1158 for p in works[("movie", "tmdb", "949")]["people"]))
bebop = works.get(("anime", "anilist", "1"))
check("an anime's AniList studio row and TMDB voice-cast row join through the shared id",
      bebop is not None and [c["name"] for c in bebop["companies"]] == ["Sunrise"]
      and [p["name"] for p in bebop["people"]] == ["Kouichi Yamadera", "Unshou Ishizuka"]
      and ("tmdb", "30991") in bebop["aliases"])
check("expired rows and music stay out",
      not any(k[0] == "show" for k in works) and not any(k[0] == "music" for k in works))

# ── persisted, durable, written only when it moved ──────────────────────────
from _checks import memory_engine  # noqa: E402
engine = memory_engine()
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, autoflush=False)


@contextmanager
def fake_session():
    db = Session()
    try:
        yield db
    finally:
        db.close()


ci.get_db_session = fake_session
# an earlier walk filed Bebop under its TMDB id, before the bridging row existed
ci.persist([ci.merge([ci.extract("anime", {"tmdb_id": 30991, "title": "Cowboy Bebop",
                                            "cast": ["Kouichi Yamadera"]})])])
first = ci.persist(list(works.values()))
with fake_session() as db:
    heat = ci.credits_of(db, "movie", "tmdb", 949)
    bebop_rows = db.query(MediaWork).filter_by(category="anime").all()
check("the index answers who made it, by role and in billing order",
      heat["people"]["director"] == ["Michael Mann"]
      and heat["people"]["cast"][:2] == ["Al Pacino", "Robert De Niro"]
      and heat["companies"]["production"] == ["Regency Enterprises", "Forward Pass"])
check("a title filed earlier under its other id is folded into one entry",
      first["folded"] == 1 and [(w.id_source, w.work_id) for w in bebop_rows] == [("anilist", "1")])
again = ci.persist(list(works.values()))
check("an unchanged walk writes nothing", again == {"new": 0, "updated": 0, "unchanged": 2, "folded": 0})
w = dict(works[("movie", "tmdb", "949")])
w["people"] = w["people"][:1]
w["fingerprint"] = "changed"
ci.persist([w])
with fake_session() as db:
    n = db.query(MediaCredit).join(MediaWork, MediaCredit.work_ref == MediaWork.id).filter(
        MediaWork.work_id == "949").count()
    comp = db.query(MediaCompany).count()
check("a changed title replaces its credits instead of piling them up", n == 1 and comp == 3)


def _registry_job():
    from src.services.data_custodian import _registry
    return next((t for t in _registry() if t.job_id == "credits_index"), None)


job = _registry_job()
check("the custodian walks it daily, no model needed",
      job is not None and not job.needs_llm and job.takes_task and job.cadence_h == 24.0)

check.done()
