"""Deletion-scan throughput: the batched / indexed / memoized paths give the
same answers as the per-item code they replace.

  * retrieve_considerations_many returns, per profile, exactly what
    retrieve_considerations returns for it — and embeds the whole slice in
    one /api/embed call instead of one per candidate
  * embed_queries chunks at _BATCH and keeps one slot per input on failure
  * embeddings_for_domain memoizes the corpus and a write through the
    wrapper drops the memo
  * init_db creates the watch_history lookup indexes, and the owner's watch
    lookup plans as a MULTI-INDEX OR instead of a walk of their rows
  * the deletion loop builds the next candidate's evidence while the
    current verdict runs, and reaps what it built ahead

    python tests/test_scan_throughput.py
"""
import asyncio
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

from src.config import settings  # noqa: E402
settings.ENRICHMENT_CACHE = _TMP / "enrichment.db"

from sqlalchemy import text  # noqa: E402

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import EpisodicMemory, User, WatchHistoryEntry  # noqa: E402

conn_mod.init_db()

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


with conn_mod.get_db_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True, is_active=True))
    db.commit()

# ── considerations: batched == single ────────────────────────────────────────
print("episodic_memory.retrieve_considerations_many")
import src.services.episodic_memory as em  # noqa: E402
import src.services.embed_service as es  # noqa: E402

DIM = 16


def _vec(seed: int) -> list:
    # deterministic, clustered (shared component) like real text embeddings
    return [1.0 + math.sin(seed * 7.1 + k * 1.3) * 0.6 for k in range(DIM)]


CONTENTS = [
    "The user keeps the Naruto franchise and loves it.",
    "The user values whatever their partner enjoys.",
    "The user cherishes horror comedy nights.",
    "The user agreed to delete the old sitcom.",       # negative: never fires
    "Filler note about the weather.",
    "The user loves slow nature documentaries.",
    "The user keeps every partner pick.",              # superseded below
]
with conn_mod.get_db_session() as db:
    for k, c in enumerate(CONTENTS):
        db.add(EpisodicMemory(
            user_id=1, memory_type="explicit_statement", content=c,
            media_category="movie" if k % 2 else None, importance=0.3 + 0.1 * k,
            embedding_json=json.dumps(_vec(k)),
            metadata_json=json.dumps({"superseded_by": 1}) if k == 6 else None))
    # an empty vector scores 0.0, one of another width is skipped
    db.add(EpisodicMemory(user_id=1, memory_type="conversation",
                          content="The user loves an empty vector.",
                          media_category="movie", importance=0.5, embedding_json="[]"))
    db.add(EpisodicMemory(user_id=1, memory_type="conversation",
                          content="The user loves another model.",
                          media_category="movie", importance=0.5,
                          embedding_json=json.dumps([1.0, 0.0])))
    db.commit()

PROFILES = ["Naruto Shippuden — Anime, Action", "Date Night — Comedy, partner pick",
            "Planet Earth — Documentary, nature", "", "Scream — horror comedy"]
QVEC = {p: _vec(i + 3) for i, p in enumerate(PROFILES)}
QVEC[PROFILES[0]] = _vec(0)        # a near-exact match to the franchise memory
embed_calls = []


async def _embed(text, kind="document"):
    return QVEC[text]


async def _post_embed(model, inputs, num_ctx):
    embed_calls.append(len(inputs))
    return [QVEC[t.removeprefix("search_query: ")] for t in inputs]


saved = (em._embed, es._post_embed)
em._embed, es._post_embed = _embed, _post_embed
try:
    single = [asyncio.run(em.retrieve_considerations(1, p, media_category="movie", top_k=3))
              for p in PROFILES]
    many = asyncio.run(em.retrieve_considerations_many(1, PROFILES, media_category="movie",
                                                       top_k=3))
finally:
    em._embed, es._post_embed = saved
check("one list per profile, in order", len(many) == len(PROFILES))
check("every profile scores exactly as the single call scores it", many == single)
check("…and at least one profile actually found a consideration", any(many))
check("an empty profile gets [] and is never embedded", many[3] == [] and
      embed_calls == [len(PROFILES) - 1])
check("the superseded and the negative memory never fire",
      all("every partner pick" not in c["content"] and "delete" not in c["content"]
          for cons in many for c in cons))

# ── embed_queries chunks and keeps its slots ────────────────────────────────
print("embed_service.embed_queries")
sizes = []


async def _ok(model, inputs, num_ctx):
    sizes.append(len(inputs))
    return [[1.0, 0.0] for _ in inputs]


async def _fail(model, inputs, num_ctx):
    return []


es._post_embed = _ok
try:
    out = asyncio.run(es.embed_queries([f"q{i}" for i in range(es._BATCH + 5)]))
    check("chunks at _BATCH", sizes == [es._BATCH, 5] and len(out) == es._BATCH + 5)
    es._post_embed = _fail
    out = asyncio.run(es.embed_queries(["a", "b"]))
    check("a failed call keeps one None slot per input", out == [None, None])
finally:
    es._post_embed = saved[1]

# ── embeddings_for_domain memo ──────────────────────────────────────────────
print("chromadb_wrapper.embeddings_for_domain memo")
import src.vector_store.chromadb_wrapper as cw  # noqa: E402


class _Coll:
    gets = 0

    def get(self, where=None, include=None, limit=None):
        _Coll.gets += 1
        return {"embeddings": [[1.0, 0.0], [0.0, 1.0]], "metadatas": [{}, {}]}

    def update(self, ids, metadatas):
        pass


w = cw.ChromaDBWrapper.__new__(cw.ChromaDBWrapper)
w.collection, w.collection_name = _Coll(), "bench"
a = w.embeddings_for_domain("movie")
b = w.embeddings_for_domain("movie")
check("a repeat pull is served from the memo", _Coll.gets == 1 and a is b)
check("…as a float32 matrix the caller cannot modify",
      str(a.dtype) == "float32" and a.shape == (2, 2) and not a.flags.writeable)
w.update_metadata("x", {"k": 1})
w.embeddings_for_domain("movie")
check("a write through the wrapper drops the memo", _Coll.gets == 2)

# ── watch_history indexes + plan ────────────────────────────────────────────
print("watch_history lookup indexes")
with conn_mod.engine.connect() as c:
    idx = {r[0] for r in c.execute(text(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='watch_history'"))}
check("init_db creates the title / series / lower(series) / tmdb indexes",
      {"ix_wh_title", "ix_wh_series_title", "ix_wh_series_title_lc",
       "ix_wh_tmdb_id", "ix_wh_user_type_viewed"} <= idx)

now = datetime(2026, 1, 1)
with conn_mod.get_db_session() as db:
    db.bulk_insert_mappings(WatchHistoryEntry, [
        dict(user_id=1, plex_user_id="1", plex_item_id=str(i), title=f"track {i % 900}",
             media_type="music", series_title=f"artist {i % 300}",
             viewed_at=now - timedelta(minutes=i)) for i in range(6000)]
        # video rows carry their TMDB id, music rows none — as Plex writes them
        + [dict(user_id=1, plex_user_id="1", plex_item_id=f"v{i}", title=f"ep {i % 400}",
                media_type="show", series_title=f"series {i % 150}", tmdb_id=i % 400,
                viewed_at=now - timedelta(minutes=i)) for i in range(3000)])
    db.commit()
conn_mod._refresh_planner_stats()

from src.services import pillars  # noqa: E402

filters = pillars._watch_filters({"title": "Some Show", "tmdb_id": 42}, "show")
W = WatchHistoryEntry
with conn_mod.get_db_session() as db:
    # The query _owner_watch runs: ``user_id + 0`` keeps the planner off the
    # user_id-led indexes. On the live history (384k rows, the owner holds
    # nearly all) a plain ``user_id ==`` still took ix_wh_user_type_viewed
    # after ANALYZE: 34 ms per title against 0.5 ms for the OR.
    q = db.query(W.viewed_at).filter((W.user_id + 0) == 1, *filters)
    sql = str(q.statement.compile(conn_mod.engine, compile_kwargs={"literal_binds": True}))
    plan = " ".join(r[3] for r in db.execute(text("EXPLAIN QUERY PLAN " + sql)))
check("the owner's watch lookup is a MULTI-INDEX OR, not a walk of their rows",
      "MULTI-INDEX OR" in plan and "ix_wh_title" in plan)
check("...and _owner_watch keeps user_id off the index (user_id + 0)",
      "(WatchHistoryEntry.user_id + 0) == owner_id" in (ROOT / "src/services/pillars.py").read_text(encoding="utf-8"))

# ── the deletion loop pipelines evidence ────────────────────────────────────
print("deletion loop evidence pipelining")
eng = (ROOT / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
loop = eng.split("watch_unknown_skipped = 0")[1].split("return final_proposals")[0]
check("the next candidate's evidence starts before this verdict's judge call",
      loop.index("_ev_ahead[_nx] = asyncio.ensure_future(build_evidence(")
      < loop.index("await adjudicate("))
check("tasks built ahead are cancelled and reaped before the gate is released",
      loop.index("_t.cancel()") < loop.index("curator_done(lane=_run_lane)\n        _msg(")
      and "return_exceptions=True" in loop)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
