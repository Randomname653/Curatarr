"""The vector store and the Discogs scan stay off the event loop.

  * Chroma calls from semantic search, the facet index, the enricher's
    upsert and the scorer's embedding prefetch run on the Chroma thread
    (run_chroma), not on the loop thread; the static ratchet
    (tests/test_loop_db_ratchet.py) covers every other coroutine
  * importing recommendations_engine builds no Chroma client; the lifespan
    builds it before serving
  * the Discogs dump is decompressed and scanned on a worker, chunk by
    chunk, and the result is the same

Each behaviour check fails on the code before this change.

    python tests/test_loop_free_vector.py
"""
import asyncio
import gzip
import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

from src.config import settings  # noqa: E402
settings.ENRICHMENT_CACHE = _TMP / "enrichment.db"
settings.CHROMADB_PATH = _TMP / "chromadb"

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import Base  # noqa: E402

Base.metadata.create_all(conn_mod.engine)

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── 0. import builds nothing ────────────────────────────────────────────────
print("Chroma client lifecycle")
import src.vector_store.chromadb_wrapper as cw  # noqa: E402
import src.services.recommendations_engine as reng  # noqa: E402

check("importing recommendations_engine builds no Chroma client",
      cw._cached_wrapper.cache_info().currsize == 0 and not hasattr(reng, "chroma_db"))
main_src = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
life = main_src[main_src.index("async def lifespan"):main_src.index("loop_guard.serving(True)")]
check("the lifespan builds it at startup, before serving", "get_chroma_db()" in life)

# ── 1. Chroma calls run on the Chroma thread ────────────────────────────────
print("Chroma calls off the loop")
calls = []          # (method, thread name)


class _Recorder:
    """Every wrapper method records the thread it runs on."""
    collection_name = "test"
    client = None

    def __getattr__(self, name):
        def _m(*a, **k):
            calls.append((name, threading.current_thread().name))
            if name in ("query", "facets_query"):
                return {"ids": [["d1"]], "documents": [["doc"]],
                        "metadatas": [[{"title": "Film", "genres": "Drama", "parent": "d1"}]],
                        "distances": [[0.1]]}
            if name == "get_by_ids":
                return {}
            if name == "get_by_id":
                return {"id": a[0] if a else "", "embedding": [1.0, 0.0], "metadata": {}}
            return True
        return _m


cw.get_chroma_db = lambda: _Recorder()
import src.services.embed_service as es  # noqa: E402


async def _embed_one(*a, **k):
    return [1.0, 0.0]


async def _embed_many(texts, *a, **k):
    return [[1.0, 0.0] for _ in texts]


es.embed_query = _embed_one
es.embed_documents = _embed_many
es.get_profile = lambda *a, **k: {"model": "test-embed", "collection": "test"}


def _off_loop(method_names) -> bool:
    seen = [t for m, t in calls if m in method_names]
    return bool(seen) and all(t != "MainThread" for t in seen)


from src.services import semantic_search as ss  # noqa: E402

calls.clear()
asyncio.run(ss.semantic_hits("slow drama", 3))
check("semantic search queries Chroma off the loop thread", _off_loop({"query"}))

from src.services import facet_index as fi  # noqa: E402

calls.clear()
asyncio.run(fi.write_facets("d1", "Film", "movie", "Drama", ["grief", "family"]))
check("the facet index deletes and adds off the loop thread",
      _off_loop({"facets_delete_by_parent", "facets_replace"}))

from src.services import media_enricher as me  # noqa: E402

calls.clear()
try:
    asyncio.run(me.store_profile_vector(
        {"title": "Film", "embedding_text": "a film about grief", "genres": ["Drama"]},
        media_type="movie"))
except Exception as e:  # noqa: BLE001 - report the crash as a failed check below
    print(f"  (store_profile_vector raised {e!r})")
check("the enricher upserts its vector off the loop thread", _off_loop({"upsert_documents"}))

import json  # noqa: E402
from src.database.models import EncryptedTasteVector, TasteVectorEntry, User  # noqa: E402

with conn_mod.get_db_session() as _db:     # a taste to score against
    _db.add(User(id=1, plex_user_id="u1", plex_username="owner"))
    _db.flush()
    _db.add(TasteVectorEntry(user_id=1, genre_affinity=json.dumps(
        {"movie": {"genre_affinity": {"Drama": 1.0}}})))
    _db.add(EncryptedTasteVector(user_id=1, media_category="movie", salt="",
                                 encrypted_blob=json.dumps({"embedding": [1.0, 0.0]})))
calls.clear()
asyncio.run(reng.score_arr_items(1, "movie", [{"title": "Film", "plex_rating_key": "k1"}]))
check("the scorer's embedding prefetch runs off the loop thread", _off_loop({"get_by_ids"}))

# ── 2. the Discogs scan ─────────────────────────────────────────────────────
print("Discogs dump scan")
import httpx  # noqa: E402
from src.services import discogs_offline as dg  # noqa: E402

xml = (b"<masters>" + b"".join(
    b'<master id="%d"><artist><id>1</id><name>Crypton</name></artist>'
    b"<title>T%d</title><year>2001</year><genre>Electronic</genre>"
    b"<style>Hardcore</style></master>" % (i, i) for i in range(40)) + b"</masters>")
payload = gzip.compress(xml * 50)
scan_threads = set()
_orig_norm = dg._norm


def _norm_spy(s):
    scan_threads.add(threading.current_thread().name)
    return _orig_norm(s)


real_client = httpx.AsyncClient
saved = (dg.STYLES_DB_PATH, dg._latest_masters_key, dg._library_artists,
         httpx.AsyncClient, dg._gaming, dg._norm)
dg.STYLES_DB_PATH = _TMP / "discogs_styles.db"
dg._latest_masters_key = lambda: "discogs_masters.xml.gz"
dg._library_artists = lambda: {"crypton": (set(), set())}
dg._gaming = lambda: False
dg._norm = _norm_spy
httpx.AsyncClient = lambda **kw: real_client(transport=httpx.MockTransport(
    lambda req: httpx.Response(200, content=payload)))
try:
    res = asyncio.run(dg.refresh_discogs_styles(force=True))
finally:
    (dg.STYLES_DB_PATH, dg._latest_masters_key, dg._library_artists,
     httpx.AsyncClient, dg._gaming, dg._norm) = saved
con = sqlite3.connect(str(_TMP / "discogs_styles.db"))
styles = con.execute("SELECT styles FROM artist_styles WHERE artist_norm='crypton'").fetchone()
con.close()
check("every master block is scanned off the loop thread",
      scan_threads and "MainThread" not in scan_threads)
check("the result is unchanged (2,000 masters matched, the style stored)",
      res.get("matched_masters") == 2000 and styles and "Hardcore" in styles[0])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
