"""Enrichment + vector-store correctness (2026-09 audit).

Pins six bugs, each with the case that would have caught it:
  1. ensure_verified_data resolved the ids via fast-enrich, then called every
     later top-up (and rebuilt ``data``) with the caller's ORIGINAL None ids
  2. re-enrichment never replaced the stored vector — Chroma's ``add`` with
     an existing id keeps the old one without raising, so the enricher's
     except-branch "refresh" never ran
  3. the taste engine's stored-vector lookup took any doc under str(tmdb_id)
     / str(anilist_id) — another title's vector when the id spaces collide
  4. a one-category force re-enrich wiped EVERY category's emb caches
  5. backfill stop + quick restart revived the old worker (two walkers) and
     the first to finish unregistered the other
  6. after the embedding-migration flip, modules that imported ``chroma_db``
     kept querying the old collection

The vector store is opened in a temp dir (never the live data/chromadb).

    python tests/test_enrichment_vector_correctness.py
"""
import asyncio
import sqlite3
import sys
import tempfile
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


# Before ANY wrapper exists: temp store + a pinned profile (no app_state read).
from src.config import settings  # noqa: E402
settings.CHROMADB_PATH = tempfile.mkdtemp(prefix="curatarr-vec-correct-")
from src.services import embed_service  # noqa: E402
embed_service._profile_cache["value"] = {
    "model": "test-embed", "collection": "test_coll_a", "prefixes": False}

import src.vector_store.chromadb_wrapper as cw  # noqa: E402
from src.services import media_enricher as me  # noqa: E402
from src.services import taste_engine as te  # noqa: E402
from src.routers import enrichment as en  # noqa: E402


def _vec(i, dim=8):
    v = [0.0] * dim
    v[i] = 1.0
    return v


# ── 6. profile flip re-points the wrapper every importer already holds ───────

db = cw.get_chroma_db()
from src.vector_store.chromadb_wrapper import chroma_db as held  # noqa: E402
check("module-level chroma_db is the singleton", held is db)
check("starts on the profile's collection", held.collection_name == "test_coll_a")

# a doc that only exists in the NEW collection
db.client.get_or_create_collection(
    "test_coll_b", metadata={"hnsw:space": "cosine"}).add(
    ids=["only-in-b"], embeddings=[_vec(0)], documents=["b"],
    metadatas=[{"title": "B"}])
embed_service._profile_cache["value"] = {
    "model": "test-embed", "collection": "test_coll_b", "prefixes": False}
cw.refresh_singleton()   # what embed_service.set_profile calls
check("an import-time binding follows the flip (was: stuck on the old collection)",
      held.collection_name == "test_coll_b" and held.get_by_id("only-in-b") is not None)
check("get_chroma_db still returns that same object", cw.get_chroma_db() is held)
try:
    import src.services.recommendations_engine as recs
    check("recommendations_engine's chroma_db sees the new collection",
          recs.chroma_db.collection_name == "test_coll_b")
except Exception as e:   # heavy import — the identity checks above still pin it
    print(f"  (skipped recommendations_engine import: {e})")


# ── 2. re-enrichment replaces the vector ─────────────────────────────────────

raw_add = db.collection
raw_add.add(ids=["probe"], embeddings=[_vec(1)], documents=["old"])
raw_add.add(ids=["probe"], embeddings=[_vec(2)], documents=["new"])
check("premise: chroma add() on an existing id keeps the OLD vector silently",
      list(db.get_by_id("probe")["embedding"])[1] == 1.0)
db.upsert_documents(documents=["new"], embeddings=[_vec(2)],
                    metadatas=[{"title": "p"}], ids=["probe"])
got = db.get_by_id("probe")
check("upsert_documents replaces vector, document and metadata",
      list(got["embedding"])[2] == 1.0 and got["document"] == "new"
      and got["metadata"] == {"title": "p"})


class _FakeGen:
    vec = None

    async def generate_embedding(self, text):
        return _FakeGen.vec

    async def close(self):
        pass


async def _no_facets(*a, **k):
    return None


import src.embeddings.embedding_generator as eg  # noqa: E402
import src.services.facet_index as fi  # noqa: E402
_orig = (eg.EmbeddingGenerator, fi.write_facets, me.summarize_with_small_llm)
eg.EmbeddingGenerator, fi.write_facets = _FakeGen, _no_facets


async def _profile(raw):
    return {"title": "Re-Enriched", "embedding_text": raw["_text"],
            "genres": ["Drama"], "year": 2001}


me.summarize_with_small_llm = _profile
try:
    for i, text in enumerate(("first pass", "second pass")):
        _FakeGen.vec = _vec(3 + i)
        asyncio.run(me.process_and_save({
            "title": "Re-Enriched", "media_type": "movie", "_text": text,
            "_plex_rating_key": "radarr:777"}))
finally:
    eg.EmbeddingGenerator, fi.write_facets, me.summarize_with_small_llm = _orig
got = db.get_by_id("radarr:777")
check("process_and_save on a re-enrich stores the NEW vector (was: first one kept)",
      got is not None and list(got["embedding"])[4] == 1.0
      and got["document"] == "second pass")
me_src = (ROOT / "src/services/media_enricher.py").read_text(encoding="utf-8")
check("no enricher write site uses add_documents any more",
      "add_documents(" not in me_src)


# ── 3. id-namespace collision in the taste engine's vector lookup ────────────

movie_entry = {"plex_item_id": "radarr:1", "media_type": "movie",
               "title": "The Matrix", "series_title": None}
movie_profile = {"title": "The Matrix", "tmdb_id": 603}
check("a same-domain, same-title doc is accepted",
      te._doc_is_item({"domain": "movie", "title": "The Matrix"}, movie_entry, movie_profile))
check("corpus_repair's 'Title (year)' form is still this item",
      te._doc_is_item({"domain": "movie", "title": "The Matrix (1999)"}, movie_entry, movie_profile))
check("TMDB TV doc under the same numeric id is rejected (movie vs show)",
      not te._doc_is_item({"domain": "show", "title": "The Matrix"}, movie_entry, movie_profile))
check("another title in the same domain is rejected",
      not te._doc_is_item({"domain": "movie", "title": "Dragon Ball"}, movie_entry, movie_profile))
check("legacy 'tv' epoch still counts as a show",
      te._doc_is_item({"domain": "tv", "title": "Lost"},
                      {"media_type": "show", "series_title": "Lost", "title": "Pilot"}, None))
check("a doc without metadata cannot be verified", not te._doc_is_item(None, movie_entry, None))


class _FakeChroma:
    def __init__(self, docs):
        self.docs = docs

    def get_by_id(self, doc_id):
        return self.docs.get(doc_id)


_orig_get = cw.get_chroma_db
try:
    # radarr:1 has no doc; "603" is a SHOW (TMDB TV 603) — the old lookup took it
    cw.get_chroma_db = lambda: _FakeChroma({
        "603": {"embedding": _vec(5), "metadata": {"domain": "show", "title": "Other Show"}},
        "The Matrix": {"embedding": _vec(6), "metadata": {"domain": "movie", "title": "The Matrix"}},
    })
    v = te._chroma_item_vector(movie_entry, movie_profile)
    check("colliding id's vector skipped, the item's own doc used instead",
          v is not None and v[6] == 1.0 and v[5] == 0.0)
    cw.get_chroma_db = lambda: _FakeChroma({
        "603": {"embedding": _vec(5), "metadata": {"domain": "show", "title": "Other Show"}}})
    check("only a colliding doc → no stored vector (caller re-embeds)",
          te._chroma_item_vector(movie_entry, movie_profile) is None)
finally:
    cw.get_chroma_db = _orig_get


# ── 1. ensure_verified_data carries the resolved ids forward ─────────────────

calls = {"build": [], "sig": [], "rec": [], "omdb": []}


def _fake_build(title, media_type, **kw):
    calls["build"].append(kw)
    if len(calls["build"]) == 1:
        return None          # cache miss → fast-enrich path
    return {"title": title, "year": 1999, "imdb_id": "tt0133093"} \
        if kw.get("tmdb_id") == 603 else {"title": title}   # title-only = thin


async def _fake_enrich(**kw):
    return {"tmdb_id": 603}


def _recorder(key):
    async def _f(title, media_type, **kw):
        calls[key].append(kw)
        return True
    return _f


import src.services.reception as rc  # noqa: E402
_o = (me.build_verified_data, me.enrich_media_item, me.topup_significance,
      me.topup_omdb, rc.topup_reception, rc.topup_franchise)
me.build_verified_data, me.enrich_media_item = _fake_build, _fake_enrich
me.topup_significance, me.topup_omdb = _recorder("sig"), _recorder("omdb")
rc.topup_reception, rc.topup_franchise = _recorder("rec"), _recorder("rec")
try:
    data = asyncio.run(me.ensure_verified_data("The Matrix", "movie"))
finally:
    (me.build_verified_data, me.enrich_media_item, me.topup_significance,
     me.topup_omdb, rc.topup_reception, rc.topup_franchise) = _o
check("significance top-up writes under the resolved tmdb_id",
      calls["sig"] and calls["sig"][0].get("tmdb_id") == 603)
check("reception top-up writes under the resolved tmdb_id",
      calls["rec"] and all(c.get("tmdb_id") == 603 for c in calls["rec"]))
check("OMDb top-up writes under the resolved tmdb_id",
      calls["omdb"] and calls["omdb"][0].get("tmdb_id") == 603)
check("every rebuild after the fast-enrich uses the resolved id",
      len(calls["build"]) > 2 and all(c.get("tmdb_id") == 603 for c in calls["build"][1:]))
check("the returned record is the id-resolved one, not a title-only rebuild",
      data and data.get("imdb_id") == "tt0133093")


# ── 4. force re-enrich clears only the requested categories' emb caches ─────

from src.cache.metadata_cache import _CACHE_VERSION as CV  # noqa: E402
conn = sqlite3.connect(":memory:")
conn.execute("CREATE TABLE api_cache (cache_key TEXT PRIMARY KEY, v TEXT)")
keys = {
    "movie_emb":   f"{CV}:emb:nomic-embed-text-v2-moe:101",
    "movie_ts":    f"{CV}:emb_ts:nomic-embed-text-v2-moe:101",
    "arr_emb":     f"{CV}:emb:nomic-embed-text:latest:sonarr:3176",
    "legacy_emb":  f"{CV}:emb:101",
    "anime_emb":   f"{CV}:emb:nomic-embed-text-v2-moe:202",
    "anime_ts":    f"{CV}:emb_ts:nomic-embed-text-v2-moe:202",
    "enriched":    f"{CV}:enriched:movie:101",
}
conn.executemany("INSERT INTO api_cache VALUES (?, '')", [(k,) for k in keys.values()])
n_emb, n_ts = en._clear_emb_caches(conn, {"101", "sonarr:3176"})
left = {r[0] for r in conn.execute("SELECT cache_key FROM api_cache")}
check("requested items' emb/emb_ts rows cleared (model tags with colons too)",
      not ({keys["movie_emb"], keys["movie_ts"], keys["arr_emb"], keys["legacy_emb"]} & left)
      and (n_emb, n_ts) == (3, 1))
check("other categories' embedding caches survive (was: all wiped)",
      {keys["anime_emb"], keys["anime_ts"]} <= left)
check("non-embedding cache rows untouched", keys["enriched"] in left)
check("no item keys → nothing deleted", en._clear_emb_caches(conn, set()) == (0, 0))
en_src = (ROOT / "src/routers/enrichment.py").read_text(encoding="utf-8")
check("the blanket '%:emb:%' wipe is gone", '"%:emb:%"' not in en_src
      and "(f\"%:emb:%\",)" not in en_src)


# ── 5. backfill stop + quick restart: one worker, state kept ────────────────

import src.services.archive_backfill as ab  # noqa: E402
from fastapi import BackgroundTasks  # noqa: E402

_SRC = next(iter(ab.SOURCES))
live = {"n": 0}


async def _fake_run_source(source, *, task=None, should_stop=None, **kw):
    live["n"] += 1
    try:
        while not should_stop():
            await asyncio.sleep(0.01)
    finally:
        live["n"] -= 1
    return {"added": 0, "visited": 0}


async def _backfill_scenario():
    orig = ab.run_source
    ab.run_source = _fake_run_source
    try:
        def _launch(bt):
            t = bt.tasks[-1]
            return asyncio.create_task(t.func(*t.args, **t.kwargs))

        bt = BackgroundTasks()
        await en.start_backfill(_SRC, bt, user=None)
        w1 = _launch(bt)
        await asyncio.sleep(0.03)
        await en.stop_backfill(_SRC, user=None)
        bt2 = BackgroundTasks()
        await en.start_backfill(_SRC, bt2, user=None)   # before w1 noticed
        w2 = _launch(bt2)
        await asyncio.sleep(0.1)
        r = {"old_exited": w1.done(), "one_worker": live["n"] == 1,
             "still_running": _SRC in en._backfill_running and not w2.done()}
        await en.stop_backfill(_SRC, user=None)
        await asyncio.wait_for(w2, 2)
        r["cleared_after_stop"] = _SRC not in en._backfill_running
        return r
    finally:
        ab.run_source = orig


r = asyncio.run(_backfill_scenario())
check("the stopped worker exits even after a quick restart", r["old_exited"])
check("exactly one walker runs", r["one_worker"])
check("the old worker finishing does not unregister the new one", r["still_running"])
check("stopping the new one clears the flag", r["cleared_after_stop"])


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
