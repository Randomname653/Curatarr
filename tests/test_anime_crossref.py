"""Anime re-resolution through the TVDB -> AniDB -> AniList crossref
(2026-10-25, src/services/anime_crossref.py).

Audit-flagged anime whose Sonarr TVDB id maps to ONE AniDB entry, and whose
AniDB entry names an AniList id (year-checked) other than the profile's, get
an automatic pin and a re-enrichment. Several entries, no year, no answer or
an existing pin: left alone.

    python tests/test_anime_crossref.py
"""
import asyncio
import json
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.database.models import (ArrEnrichmentStatus as AES, Base, EnrichmentFinding as F,  # noqa: E402
                                 EnrichmentStatus as ES, MediaMatchOverride as MO)
from src.services import anime_crossref as X, anime_mapping, anime_offline, kb_overview  # noqa: E402
import src.routers.enrichment as R  # noqa: E402

from _checks import check  # noqa: E402


from _checks import memory_engine  # noqa: E402
engine = memory_engine()
Base.metadata.create_all(engine)
S = sessionmaker(bind=engine)


@contextmanager
def fake_session():
    db = S()
    try:
        yield db
        db.commit()          # the contract of get_db_session: commit on a clean exit
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


X.get_db_session = fake_session

# ── the mapping: TVDB series -> AniDB entries ───────────────────────────────
tmp = Path(tempfile.mkdtemp())
mp = tmp / "anime_mapping.db"
con = sqlite3.connect(mp)
con.execute("CREATE TABLE anime_mapping (anidb_id INTEGER, tvdb_id INTEGER, anilist_id INTEGER, "
            "is_primary INTEGER NOT NULL DEFAULT 0)")
con.executemany("INSERT INTO anime_mapping VALUES (?,?,?,?)", [
    (391, 100, None, 1), (392, 100, None, 0),     # main entry plus a sequel row
    (500, 200, None, 0), (501, 200, None, 0),     # split seasons, no main one
    (600, 300, None, 0),                          # the only row
    (700, 400, None, 1),                          # agrees with the profile
    (800, 500, None, 1),                          # year off
    (900, 600, None, 1),                          # already pinned
])
con.commit()
con.close()
anime_mapping.MAPPING_DB_PATH = mp
check("the main AniDB entry: the one primary row, else the only row, else none",
      X.main_anidb(100) == 391 and X.main_anidb(300) == 600 and X.main_anidb(200) is None
      and X.main_anidb(999) is None)

OFFLINE = {391: (690, 1997), 600: (6379, 2008), 700: (55, 2010), 800: (77, 2001), 900: (88, 2012)}


def fake_lookup(*, anidb_id=None, year=None, **_):
    a, y = OFFLINE.get(anidb_id, (None, None))
    if a is None or (year and abs(y - year) > 1):
        return None
    return {"anidb_id": anidb_id, "anilist_id": a, "year": y}


anime_offline.lookup = fake_lookup
purged = []
R._purge_and_requeue_item = lambda service, arr_id, category: purged.append((service, arr_id, category)) or {}
kb_overview.invalidate = lambda: None

# ── the findings ────────────────────────────────────────────────────────────
db = S()
items = [  # arr_id, tvdb, kind, profile anilist, arr year in the finding
    (1, 100, "wrong_entity:year", 1384, 1997),    # -> pinned 690
    (2, 200, "wrong_entity:year", 11, 2010),      # several entries: left
    (3, 300, "id_conflict:anilist_id", 837, None),  # year from the enrichment context -> 6379
    (4, 400, "wrong_entity:title", 55, 2010),     # agrees: left
    (5, 500, "wrong_entity:year", 12, 2015),      # year off: left
    (6, 600, "wrong_entity:year", 13, 2012),      # pinned already: left
    (7, 300, "wrong_entity:year", 14, None),      # no year anywhere: left
]
for arr_id, tvdb, kind, prof, year in items:
    db.add(AES(service="sonarr", arr_id=arr_id, category="anime", title=f"T{arr_id}", tvdb_id=tvdb))
    detail = {"value": prof} if kind.startswith("id_conflict") else {"profile_anilist_id": prof, "arr_year": year}
    db.add(F(plex_rating_key=f"sonarr:{arr_id}", service="sonarr", arr_id=arr_id, category="anime",
             title=f"T{arr_id}", kind=kind, detail=json.dumps(detail)))
db.add(F(plex_rating_key="sonarr:1", service="sonarr", arr_id=1, category="anime", title="T1",
         kind="zero_rating", detail="{}"))
db.add(F(plex_rating_key="radarr:9", service="radarr", arr_id=9, category="movie", title="M",
         kind="wrong_entity:year", detail="{}"))
db.add(ES(plex_rating_key="sonarr:3", title="T3", media_category="anime", sources_state=json.dumps({"_context": {"arr_year": 2008}})))
db.add(MO(service="sonarr", arr_id=6, category="anime", anilist_id=1, note="pinned by the owner"))
db.commit()
c = {x["arr_id"]: x for x in X.candidates(db)}
db.close()
check("candidates: Sonarr anime with a TVDB id and a year, no pin, one per item",
      sorted(c) == [1, 2, 3, 4, 5] and c[3]["year"] == 2008 and c[3]["profile_anilist_id"] == 837)

res = asyncio.run(X.run_anime_crossref())
db = S()
pins = {r.arr_id: r for r in db.query(MO)}
check("the crossref pins the items it settles, and only those",
      res == {"candidates": 5, "pinned": 2} and pins[1].anilist_id == 690 and pins[3].anilist_id == 6379
      and set(pins) == {1, 3, 6})
check("...as an automatic pin, with no owner on it",
      pins[1].note == X.NOTE and pins[1].created_by is None and pins[1].category == "anime")
check("...closes the item's findings, the other items' stay",
      {f.status for f in db.query(F).filter(F.arr_id == 1)} == {"resolved"}
      and {f.status for f in db.query(F).filter(F.arr_id == 2)} == {"pending"})
check("...and re-enriches them", sorted(purged) == [("sonarr", 1, "anime"), ("sonarr", 3, "anime")])
check("the owner's pin is not touched", pins[6].anilist_id == 1 and pins[6].note == "pinned by the owner")
db.close()

res = asyncio.run(X.run_anime_crossref())
check("a second run does not re-pin (the loop guard)", res["pinned"] == 0 and len(purged) == 2)

src = (ROOT / "src/services/data_custodian.py").read_text(encoding="utf-8")
check("the custodian runs it daily", 'Task("anime_crossref"' in src and "_run_anime_crossref" in src)

check.done()
