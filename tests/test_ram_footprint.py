"""RAM footprint fixes (2026-10 audit of the container's anonymous memory).

Each fix keeps its behaviour and drops memory that was measured, not guessed:
  1. the enrichment cycle no longer loads every play as an ORM entity
  2. the taste rebuild shares equal strings and reads profiles in chunks
  3. SQLite's per-connection page cache is 8 MB, not 64 MB
  4. the arr library cache drops fields nothing reads
  5. the facet writer upserts in place (delete + add burned an HNSW slot per
     point), and a bloated index can be rebuilt without losing a point

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

# ── 5. facet index: in-place rewrite + compaction ───────────────────────────
print("facet index")
import src.vector_store.chromadb_wrapper as cw  # noqa: E402
import src.services.facet_index as fi  # noqa: E402

wr = cw.ChromaDBWrapper(collection_name="ram_test_main")
DIM = 8


def vec(seed):
    import numpy as np
    return np.random.RandomState(seed).rand(DIM).astype("float32")


def put(parent, n, seed0, method="replace"):
    ids = [f"{parent}::f{i}" for i in range(n)]
    docs = [f"phrase {parent} {i}" for i in range(n)]
    embs = [vec(seed0 + i) for i in range(n)]
    metas = [{"parent": parent, "title": parent, "domain": "movie", "facet": "theme"} for _ in ids]
    if method == "replace":
        return wr.facets_replace(parent, docs, embs, metas, ids)
    wr.facets_delete_by_parent(parent)
    return wr.facets_add(docs, embs, metas, ids)


for p in range(60):
    put(f"d{p}", 5, p * 10)
check("60 titles x 5 facets written", wr.facets_count() == 300)
put("d3", 3, 900)
have = sorted(wr._facets_collection().get(where={"parent": "d3"}, include=[])["ids"])
check("a rewrite with fewer facets removes exactly the surplus ids",
      have == ["d3::f0", "d3::f1", "d3::f2"] and wr.facets_count() == 298)
got = wr._facets_collection().get(ids=["d3::f0"], include=["embeddings", "documents"])
import numpy as np  # noqa: E402
check("the rewritten point carries the NEW vector and document",
      np.allclose(got["embeddings"][0], vec(900)) and got["documents"][0] == "phrase d3 0")
check("a failed write reports False (embeddings of the wrong width)",
      wr.facets_replace("d3", ["x"], [[1.0, 2.0]], [{"parent": "d3"}], ["d3::f0"]) is False)

# Inflate the index the old way (delete + add) so a rebuild has work to do.
for rnd in range(3):
    for p in range(60, 100):
        put(f"d{p}", 5, p * 10 + rnd, method="old")
live_before = wr.facets_count()
check("setup: 298 + 40 x 5 live facets", live_before == 498)
snapshot = wr._facets_collection().get(include=["embeddings", "documents", "metadatas"])
want = {i: (np.asarray(e, dtype="float32"), d, m["parent"])
        for i, e, d, m in zip(snapshot["ids"], snapshot["embeddings"],
                              snapshot["documents"], snapshot["metadatas"])}

fi._COMPACT_MIN_LIVE = 10
stats = {"v": (live_before + 10, live_before)}
wr.facets_slot_stats = lambda: stats["v"]
cw.get_chroma_db = lambda: wr
check("compaction is off by default (it costs recall), whatever the dead share",
      settings.FACET_COMPACT is False
      and "FACET_COMPACT: bool = False" in (ROOT / "src/config.py").read_text(encoding="utf-8"))
settings.FACET_COMPACT = True       # the rest of this section runs it
check("a healthy index is left alone (dead share below the threshold)",
      asyncio.run(fi.compact_facets_if_needed()) is True
      and wr._compact is None and wr.facets_count() == live_before)

# a write that lands in the middle of a compaction must survive it
stats["v"] = (live_before * 2, live_before)
orig_step = wr.facets_compact_step
state = {"n": 0}


def step_with_write(page=2000):
    state["n"] += 1
    if state["n"] == 1:
        put("d5", 2, 5000)               # rewritten while the copy is running
        put("late", 3, 7000)             # brand new title
        wr.facets_delete_by_parent("d6")  # a title removed meanwhile
    return orig_step(page=100)           # small pages: several steps


wr.facets_compact_step = step_with_write
ok = asyncio.run(fi.compact_facets_if_needed())
wr.facets_compact_step = orig_step
check("the rebuild finishes and reports success", ok is True and wr._compact is None)
check("the rebuilt collection took over the name; the temporary ones are gone",
      sorted(c.name for c in wr.client.list_collections()) ==
      sorted([wr.collection_name, wr.FACETS_COLLECTION]))
after = wr._facets_collection().get(include=["embeddings", "documents", "metadatas"])
got = {i: (np.asarray(e, dtype="float32"), d, m["parent"])
       for i, e, d, m in zip(after["ids"], after["embeddings"],
                             after["documents"], after["metadatas"])}
expect = dict(want)
for k in [k for k in expect if expect[k][2] in ("d5", "d6")]:
    del expect[k]
for i in range(2):
    expect[f"d5::f{i}"] = (vec(5000 + i), f"phrase d5 {i}", "d5")
for i in range(3):
    expect[f"late::f{i}"] = (vec(7000 + i), f"phrase late {i}", "late")


def _same(a, b):
    return bool(np.allclose(a[0], b[0], atol=1e-5)) and a[1:] == b[1:]


check("every point survived with its vector, document and parent; writes made "
      "during the copy are in; the removed title is out",
      set(got) == set(expect) and all(_same(got[k], expect[k]) for k in expect))
res = wr.facets_query([vec(900).tolist()], n_results=3)
check("queries are served by the rebuilt index", res["ids"][0][0] == "d3::f0")
check("fresh client after the swap answers (release_indexes)",
      wr.release_indexes() is True and wr.facets_count() == len(expect)
      and wr.get_count() == 0)

# a failing copy keeps the old collection in service
orig_get = wr.facets_compact_step
boom = {"n": 0}


def failing_step(page=2000):
    raise RuntimeError("disk full")


wr.facets_compact_step = failing_step
stats["v"] = (len(expect) * 2, len(expect))
check("a copy that fails returns False and drops the half-built collection",
      asyncio.run(fi.compact_facets_if_needed()) is False and wr._compact is None
      and wr.facets_count() == len(expect)
      and wr.client.list_collections() is not None
      and all(c.name != wr._COMPACT_TMP for c in wr.client.list_collections()))
wr.facets_compact_step = orig_get

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
