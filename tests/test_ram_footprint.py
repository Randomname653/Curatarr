"""RAM footprint fixes (2026-10 audit of the container's anonymous memory).

Each fix keeps its behaviour and drops memory that was measured, not guessed:
  1. the enrichment cycle no longer loads every play as an ORM entity
  2. the taste rebuild shares equal strings and reads profiles in chunks
  3. SQLite's per-connection page cache is 8 MB, not 64 MB
  4. the arr library cache drops fields nothing reads
  5. the facet index holds one point per phrase (the per-title points were
     60% copies), moved over once without embedding anything again

    python tests/test_ram_footprint.py
"""
import asyncio
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# The vector store lives in a temp dir (never data/chromadb).
from src.config import settings  # noqa: E402
settings.CHROMADB_PATH = tempfile.mkdtemp(prefix="curatarr-ram-")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from src.database.models import Base, WatchHistoryEntry  # noqa: E402

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)


@contextmanager
def fake_db_session():
    s = Session()
    try:
        yield s
        s.commit()
    finally:
        s.close()


NOW = datetime(2026, 10, 1, 12, 0, 0)


def play(i, title, media_type, series=None, tmdb=None, genres="Rock", user=1):
    return WatchHistoryEntry(
        user_id=user, plex_user_id="u", plex_item_id=f"k{i}", title=title,
        media_type=media_type, series_title=series, tmdb_id=tmdb,
        viewed_at=NOW - timedelta(minutes=i), genres=genres, completed=True,
        duration_ms=1000, view_offset_ms=1000)


with fake_db_session() as s:
    rows = []
    i = 0
    for n in range(30):                       # one artist, 30 plays of 3 tracks
        i += 1
        rows.append(play(i, f"Track {n % 3}", "music", series="Some Artist"))
    for n in range(4):                        # the same artist, other case
        i += 1
        rows.append(play(i, "Other", "music", series="  some artist "))
    i += 1
    rows.append(play(i, "No Artist", "music", series=None))      # dropped by the dedupe
    for n in range(12):                       # a series, 12 episodes
        i += 1
        rows.append(play(i, f"Ep {n}", "show", series="The Show", tmdb=77))
    i += 1
    rows.append(play(i, "A Movie", "movie", tmdb=5))
    i += 1
    rows.append(play(i, "A Movie", "movie", tmdb=5))             # replay
    i += 1
    rows.append(play(i, "Someone Else's", "movie", user=2))
    s.add_all(rows)

# ── 1. enrichment: history -> one pitem per title, streamed ─────────────────
print("enrichment history collection")
import src.routers.enrichment as enr  # noqa: E402
enr.get_db_session = fake_db_session


def reference(user_id, categories):
    """The previous implementation: every row, then the dedupe step."""
    items = []
    with fake_db_session() as db:
        for cat in categories:
            for e in db.query(WatchHistoryEntry).filter(
                    WatchHistoryEntry.user_id == user_id,
                    WatchHistoryEntry.media_type == cat).all():
                items.append({"plex_rating_key": e.plex_item_id, "title": e.title,
                              "series_title": e.series_title, "media_type": cat,
                              "tmdb_id": e.tmdb_id, "tvdb_id": None, "imdb_id": None,
                              "source": "watch_history"})
    keys = {(i.get("series_title") or i["title"]).lower().strip() for i in items}
    out, seen = [], set()
    for item in items:
        cat = item["media_type"]
        if cat in ("show", "anime", "music"):
            st = item.get("series_title")
            if not st:
                continue
            key = f"{cat}:{st.lower().strip()}"
        else:
            key = f"movie:{item['title'].lower().strip()}"
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out, keys


cats = ["movie", "show", "anime", "music"]
got_items, got_keys = enr._collect_history_items(1, cats)
ref_items, ref_keys = reference(1, cats)
check("one pitem per title: same list as the load-everything version",
      got_items == ref_items)
check("the arr merge's watch_keys cover EVERY row (also the one the dedupe drops)",
      got_keys == ref_keys and "no artist" in got_keys)
check("a 30-play artist, a 12-episode series and a replayed movie are 1 item each",
      len(got_items) == 3)
check("another user's plays stay out", all(i["title"] != "Someone Else's" for i in got_items))
check("an unknown category yields nothing", enr._collect_history_items(1, ["anime"]) == ([], set()))

# ── 2. taste rebuild: shared strings, chunked profile reads ─────────────────
print("taste rebuild loads")
import src.services.taste_engine as te  # noqa: E402
te.get_db_session = fake_db_session
entries = te._load_watch_entries(1, "music")
check("every play comes back, newest first",
      len(entries) == 35 and entries[0]["viewed_at"] > entries[-1]["viewed_at"])
same = [e for e in entries if e["title"] == "Track 0"]
check("equal strings are ONE object (30 plays, 3 titles)",
      len(same) == 10 and all(e["title"] is same[0]["title"] for e in same)
      and all(e["genres"] is entries[0]["genres"] for e in entries))
check("the dict shape is unchanged",
      set(entries[0]) == {"plex_item_id", "title", "series_title", "media_type", "genres",
                          "duration_ms", "view_offset_ms", "completed", "viewed_at"})

calls = []


class FakeCache:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_cache_many(self, keys):
        calls.append(list(keys))
        return {k: {"response": {"genres": ["g"], "themes": ["t"], "mood": ["m"],
                                 "overview": "x" * 1000}}
                for k in keys if not k.endswith(":9")}


import src.cache.metadata_cache as mc  # noqa: E402
_orig_mc = mc.MetadataCache
mc.MetadataCache = FakeCache
try:
    keys = [f"enriched:music:{n % 2500}" for n in range(7000)] + [""]
    tags = te._bulk_profile_tags(iter(keys))
finally:
    mc.MetadataCache = _orig_mc
check("keys are de-duplicated before the read (7000 plays, 2500 titles)",
      sum(len(c) for c in calls) == 2500)
check("profiles are read a chunk at a time, never all at once",
      len(calls) == 3 and max(len(c) for c in calls) <= 1000)
check("only the three tag lists are kept, and a missing profile is absent",
      tags["enriched:music:5"] == (["g"], ["t"], ["m"]) and "enriched:music:9" not in tags)

# ── 3. SQLite page cache per connection ─────────────────────────────────────
print("sqlite page cache")
from src.database import connection as dbconn  # noqa: E402
con = sqlite3.connect(":memory:")
dbconn._apply_sqlite_pragmas(con)
size = con.execute("PRAGMA cache_size").fetchone()[0]
check("per-connection page cache is 8 MB (negative = KiB)",
      size == -dbconn.SQLITE_CACHE_KB == -8000)
check("twelve pooled connections stay under 100 MB of page cache",
      12 * dbconn.SQLITE_CACHE_KB / 1000 < 100)
con.close()

# ── 4. arr library cache: unread fields dropped ─────────────────────────────
print("arr library cache")
import src.routers.library as lib  # noqa: E402
items = [{"id": 1, "title": "A", "translations": [1] * 50, "images": [{"coverType": "poster"}],
          "alternateTitles": [{"title": "B"}]}]
lib._slim_arr_items("sonarr", items)
check("sonarr translations dropped, the fields the app reads stay",
      "translations" not in items[0] and items[0]["images"] and items[0]["alternateTitles"])
rad = [{"id": 2, "movieFile": {"size": 1}, "alternateTitles": [{"title": "X"}], "title": "M"}]
lib._slim_arr_items("radarr", rad)
check("radarr movieFile dropped, alternateTitles (external_ids) kept",
      "movieFile" not in rad[0] and rad[0]["alternateTitles"])
lid = [{"id": 3, "lastAlbum": {"x": 1}, "links": [1], "artistName": "Z"}]
lib._slim_arr_items("lidarr", lid)
check("lidarr album/link blobs dropped", lid[0] == {"id": 3, "artistName": "Z"})
check("an unknown service is left alone", lib._slim_arr_items("plex", [{"a": 1}]) == [{"a": 1}])
import re  # noqa: E402
readers = [str(p.relative_to(ROOT)) for p in (ROOT / "src").rglob("*.py")
           if p.name != "library.py"
           and re.search(r"""[\[(]\s*["'](translations|movieFile|lastAlbum|nextAlbum)["']""",
                         p.read_text(encoding="utf-8", errors="ignore"))]
check("no other module in src/ reads the dropped fields", readers == [])

# ── 5. facet index: one point per phrase ────────────────────────────────────
print("facet index")
import numpy as np  # noqa: E402

import src.vector_store.chromadb_wrapper as cw  # noqa: E402
import src.services.facet_index as fi  # noqa: E402

wr = cw.ChromaDBWrapper(collection_name="ram_test_main")
DIM = 8
POOL = [f"phrase {i}" for i in range(20)]       # themes repeat across titles


def vec(seed):
    return np.random.RandomState(seed).rand(DIM).astype("float32")


PV = {p: vec(1000 + i) for i, p in enumerate(POOL)}


def put(parent, phrases, domain="movie"):
    ids = [f"{parent}::f{i}" for i in range(len(phrases))]
    metas = [{"parent": parent, "title": parent.upper(), "domain": domain, "genres": "Drama",
              "facet": "theme", "emb_model": "m1"} for _ in ids]
    return wr.facets_replace(parent, phrases, [PV.get(p, vec(hash(p) % 999)) for p in phrases],
                             metas, ids)


for p in range(60):
    put(f"d{p}", [POOL[(p + k) % 20] for k in range(5)], "movie" if p % 2 else "anime")
check("60 titles x 5 facets: 300 facets on 20 phrase points",
      wr.facets_count() == 300 and wr._phrases_collection().count() == 20)
put("d3", ["phrase 1", "phrase 2", "a phrase of its own"])
rows = wr._map_rows("SELECT idx, phrase FROM facet_map WHERE parent = 'd3' ORDER BY idx")
check("a rewrite replaces the title's facets; only the new phrase is a new point",
      rows == [(0, "phrase 1"), (1, "phrase 2"), (2, "a phrase of its own")]
      and wr.facets_count() == 298 and wr._phrases_collection().count() == 21)
check("a failed write (vectors of the wrong width) reports False and keeps the old facets",
      wr.facets_replace("d3", ["brand new"], [[1.0, 2.0]], [{"parent": "d3"}], ["d3::f0"]) is False
      and wr.facets_count() == 298)
res = wr.facets_query([PV["phrase 7"].tolist()], n_results=5)
near = [m["parent"] for m in res["metadatas"][0]]
check("a query answers the titles behind the nearest phrase, in the per-title shape",
      len(set(near)) >= 5 and res["documents"][0][0] == "phrase 7"
      and all(i.count("::f") == 1 for i in res["ids"][0]))
res = wr.facets_query([PV["phrase 7"].tolist()], n_results=5, where=cw.domain_where("movie"))
check("...and only those in the domain asked for",
      res["metadatas"][0] and all(m["domain"] == "movie" for m in res["metadatas"][0]))
page = wr.facets_page(0, 7)
again = wr.facets_by_ids(page["ids"])
check("a page and the same ids by id carry the phrase vectors",
      len(page["ids"]) == 7 and page["ids"] == again["ids"]
      and np.allclose(np.asarray(page["embeddings"][0]), PV[page["documents"][0]]))
known = wr.facets_phrase_vectors(["phrase 3", "never seen"], "m1")
check("the vectors of phrases already stored are handed back (no embed for them)",
      list(known) == ["phrase 3"] and wr.facets_phrase_vectors(["phrase 3"], "other model") == {})
wr.facets_delete_by_parent("d3")
check("a deleted title's own phrase is swept, the shared ones stay",
      wr.facets_sweep() == 1 and wr._phrases_collection().count() == 20
      and wr.facets_count() == 295)

# ── the move from the old per-title collection ──────────────────────────────
legacy = wr.client.create_collection(wr.LEGACY_FACETS, metadata={"hnsw:space": "cosine"})
L = {}
for t in range(30):
    for k in range(4):
        L[f"old{t}::f{k}"] = (POOL[(t * 3 + k) % 20], f"old{t}")
legacy.add(ids=list(L), embeddings=[PV[p] for p, _ in L.values()], documents=[p for p, _ in L.values()],
           metadatas=[{"parent": par, "title": par, "domain": "show", "genres": "", "facet": "theme",
                       "emb_model": "m1"} for _, par in L.values()])
before = wr.facets_count()
first = wr.facets_migrate_step(page=50)       # old12 straddles the page boundary
put("old20", ["phrase 9"], "show")            # written fresh while the move runs
steps = 1
while True:
    r = wr.facets_migrate_step(page=50)
    steps += 1
    if r["done"]:
        break
rows = {(p, ph) for p, ph in wr._map_rows("SELECT parent, phrase FROM facet_map WHERE parent LIKE 'old%'")}
want = {(par, p) for p, par in L.values() if par != "old20"} | {("old20", "phrase 9")}
check("every old facet moved, a title split across pages complete, a fresh write kept",
      not first["done"] and steps == 3 and rows == want
      and wr.facets_count() == before + len(want))
check("the old collection is dropped and no phrase point was added for known phrases",
      wr.facets_legacy_count() is None and wr._phrases_collection().count() == 20)
cw.get_chroma_db = lambda: wr
check("the custodian job is a no-op check once the move is done",
      asyncio.run(fi.migrate_facets()) is True and wr.facets_legacy_count() is None)
src = (ROOT / "src/services/data_custodian.py").read_text(encoding="utf-8")
check("the custodian runs it weekly; the compaction is gone",
      'Task("facet_phrases"' in src and "facet_compact" not in src
      and "FACET_COMPACT" not in (ROOT / "src/config.py").read_text(encoding="utf-8"))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
