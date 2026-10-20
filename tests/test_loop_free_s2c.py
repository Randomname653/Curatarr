"""Memory retrieval, chat context, semantic search, the taste rebuild and
arr ranking keep their blocking work off the event loop.

  * retrieve_considerations scores every memory (JSON decode + cosine) in
    the read worker, not on the loop
  * _run_memory_extraction classifies a fact's category (a DB lookup and
    the library-title index) in a thread
  * episode_context resolves the Sonarr series id in a thread, and a repeat
    lookup reuses the title index until the cached blob changes
  * the discuss-context block attaches the verified dossier from a thread
  * get_starters' background refill is a tracked task
  * semantic_hits / curated_search build, decorate and score hits off the
    loop (main-DB reads via run_read, the evidence math in a thread)
  * the taste rebuild's stored-vector lookup runs on the Chroma thread
  * score_arr_items ranks the candidates in one worker thread

Each "off the loop" check fails on the code before this change.

    python tests/test_loop_free_s2c.py
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

from src.config import settings  # noqa: E402
settings.ENRICHMENT_CACHE = _TMP / "enrichment.db"

import httpx  # noqa: E402

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import (Base, EncryptedTasteVector, EnrichmentStatus,  # noqa: E402
                                 EpisodicMemory, ProactiveMessage, TasteVectorEntry,
                                 User, WatchHistoryEntry)

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


def _on_loop_thread() -> bool:
    # asyncio.run drives the loop on the main thread
    return threading.current_thread() is threading.main_thread()


with conn_mod.get_db_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True, is_active=True))
    db.commit()


# ── A9. episodic_memory.retrieve_considerations ─────────────────────────────
print("episodic_memory.retrieve_considerations")
import src.services.episodic_memory as em  # noqa: E402

with conn_mod.get_db_session() as db:
    keep = EpisodicMemory(user_id=1, memory_type="explicit_statement",
                          content="The user loves the Naruto franchise and keeps it.",
                          media_category="anime", importance=0.8,
                          embedding_json=json.dumps([1.0, 0.0, 0.0]))
    db.add(keep)
    for i in range(3):
        db.add(EpisodicMemory(user_id=1, memory_type="conversation",
                              content=f"Filler note {i} about the weather.",
                              media_category="anime", importance=0.5,
                              embedding_json=json.dumps([0.0, 1.0, 0.0])))
    db.commit()
    keep_id = keep.id


async def _embed(text, kind="document"):
    return [1.0, 0.0, 0.0]


_cos_threads = []
_real_load, _real_score = em._load_consideration_rows, em._score_considerations


def _spy_load(*a):
    _cos_threads.append(_on_loop_thread())
    return _real_load(*a)


def _spy_score(*a):
    _cos_threads.append(_on_loop_thread())
    return _real_score(*a)


_saved_em = (em._embed, em._load_consideration_rows, em._score_considerations)
em._embed, em._load_consideration_rows, em._score_considerations = (
    _embed, _spy_load, _spy_score)
try:
    cons = asyncio.run(em.retrieve_considerations(
        1, "Naruto Shippuden — Action, Adventure", media_category="anime", top_k=3))
finally:
    em._embed, em._load_consideration_rows, em._score_considerations = _saved_em
check("every memory was decoded and scored off the loop thread",
      len(_cos_threads) == 2 and not any(_cos_threads))
check("the ranking is unchanged (the franchise memory, sem 1.0, strength 0.506)",
      [c["id"] for c in cons] == [keep_id]
      and cons[0]["sem"] == 1.0 and cons[0]["strength"] == 0.506
      and cons[0]["overlap"] == ["naruto"])


# ── A10. episodic_memory._run_memory_extraction → classify ──────────────────
print("episodic_memory._run_memory_extraction")


class _LLMResp:
    status_code = 200

    def __init__(self, content):
        self._c = content

    def json(self):
        return {"message": {"content": self._c}}


class _LLMClient:
    content = "[]"

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _LLMResp(_LLMClient.content)


_written = []


async def _write(**k):
    _written.append(k)
    return 900 + len(_written)


async def _no_conflicts(**k):
    return None


_cat_threads = []
_real_title_cat = em._title_category


def _spy_title_cat(t):
    _cat_threads.append(_on_loop_thread())
    return _real_title_cat(t)


FACT = "The user loves the anime series 'Zzyzx Qwerty Moon'."
_LLMClient.content = json.dumps([{"content": FACT, "type": "preference", "title": ""}])
_saved_em = (em.httpx.AsyncClient, em.write_memory, em.resolve_memory_conflicts,
             em._title_category)
em.httpx.AsyncClient, em.write_memory, em.resolve_memory_conflicts = _LLMClient, _write, _no_conflicts
em._title_category = _spy_title_cat
try:
    ok = asyncio.run(em._run_memory_extraction(1, "prompt", media_category=None))
finally:
    (em.httpx.AsyncClient, em.write_memory, em.resolve_memory_conflicts,
     em._title_category) = _saved_em
check("the title lookup (DB + library index) ran off the loop thread",
      _cat_threads and not any(_cat_threads))
check("the fact was written with the same category",
      ok is True and len(_written) == 1 and _written[0]["media_category"] == "anime"
      and em.classify_memory_category(FACT, "") == "anime")


# ── A11. episode_context: series id off the loop + memo ─────────────────────
print("episode_context.series_availability / stop_point_context")
import src.services.episode_context as ec  # noqa: E402
from src.cache.metadata_cache import MetadataCache  # noqa: E402

SHOW = "Frieren: Beyond Journey's End"


def _seed_sonarr(sid):
    rows = [{"title": f"Filler Show {i}", "id": 100 + i} for i in range(50)]
    rows.insert(20, {"title": SHOW, "id": sid})
    with MetadataCache() as c:
        c.set_cache("arr_library:sonarr", {"items_raw": rows})


_seed_sonarr(7)
EPISODES = [
    {"seasonNumber": 0, "episodeNumber": 1, "title": "Special", "hasFile": False,
     "monitored": False, "airDateUtc": "2023-01-01T00:00:00Z"},
    {"seasonNumber": 1, "episodeNumber": 1, "title": "The Journey's End", "hasFile": True,
     "monitored": True, "airDateUtc": "2023-09-29T00:00:00Z", "overview": "The hero party returns."},
    {"seasonNumber": 1, "episodeNumber": 2, "title": "It Didn't Have to Be Magic", "hasFile": False,
     "monitored": True, "airDateUtc": "2023-09-29T01:00:00Z"},
]
_asked_ids = []


def _sonarr(request):
    _asked_ids.append(request.url.params.get("seriesId"))
    return httpx.Response(200, json=EPISODES)


_norm_threads = []
_real_norm = ec._norm


def _spy_norm(s):
    _norm_threads.append(_on_loop_thread())
    return _real_norm(s)


_saved_ec = (ec.httpx, ec._norm, settings.SONARR_URL, settings.SONARR_API_KEY)
ec.httpx = SimpleNamespace(AsyncClient=lambda **kw: httpx.AsyncClient(
    transport=httpx.MockTransport(_sonarr)))
ec._norm = _spy_norm
settings.SONARR_URL, settings.SONARR_API_KEY = "http://sonarr", "k"


async def _episode_ctx():
    av = await ec.series_availability(SHOW)
    n_first = len(_norm_threads)
    sp = await ec.stop_point_context(SHOW, 1, 1)
    n_repeat = len(_norm_threads) - n_first
    await asyncio.to_thread(_seed_sonarr, 8)   # Sonarr re-synced: the series got another id
    av2 = await ec.series_availability(SHOW)
    return av, sp, n_first, n_repeat, av2


try:
    av, sp, n_first, n_repeat, av2 = asyncio.run(_episode_ctx())
finally:
    ec.httpx, ec._norm, settings.SONARR_URL, settings.SONARR_API_KEY = _saved_ec
check("the series-id lookup (cache decode + title normalising) ran off the loop",
      _norm_threads and not any(_norm_threads))
check("availability line unchanged",
      av == "2 episodes known, 2 aired, 2 monitored, 1 on disk — "
            "1 monitored & aired but MISSING: S1E2")
check("stop-point line unchanged",
      sp == "stopped at S1E1 'The Journey's End' — The hero party returns.\n"
            "Next up: S1E2 'It Didn't Have to Be Magic' (episode NOT on disk)")
check(f"a repeat lookup reuses the title index (normalised {n_repeat} names, "
      f"first lookup {n_first})", n_first >= 51 and n_repeat == 1)
check("a rewritten Sonarr blob is picked up (new series id asked)",
      _asked_ids == ["7", "7", "8"] and av2 == av)


# ── A12. chat: the discuss block's verified dossier off the loop ────────────
print("chat._build_discuss_context_block → _verified_block_for")
import src.routers.chat as chat  # noqa: E402
import src.services.media_enricher as me  # noqa: E402

with conn_mod.get_db_session() as db:
    db.add(ProactiveMessage(id=11, user_id=1, trigger_type="rewatch",
                            trigger_data=json.dumps({"title": "Frieren", "media_type": "anime"}),
                            message="Rewatching Frieren?", read=False))
    db.commit()

_vd_threads = []


def _spy_vd(title, dom, **kw):
    _vd_threads.append(_on_loop_thread())
    return {"title": title} if dom == "anime" else None


_saved_me = (me.build_verified_data, me.format_verified_block)
me.build_verified_data = _spy_vd
me.format_verified_block = lambda vd: f"VERIFIED DOSSIER: {vd['title']}"
ctx = SimpleNamespace(kind="proactive_message", type=None, action=None, category="anime",
                      message_id=11, proposal_id=None, title=None)
try:
    block, anchor, cat = asyncio.run(chat._build_discuss_context_block(ctx, 1))
finally:
    me.build_verified_data, me.format_verified_block = _saved_me
check("build_verified_data (cache + Discogs reads) ran off the loop",
      _vd_threads and not any(_vd_threads))
check("the dossier is still attached to the block",
      "\nVERIFIED DOSSIER: Frieren\n" in block and anchor == "Frieren" and cat == "anime")


# ── A24. chat.get_starters: tracked background refill ───────────────────────
print("chat.get_starters")
import src.services.bg_tasks as bg  # noqa: E402
import src.services.chat_starters as cs  # noqa: E402
import src.services.process_monitor as pm  # noqa: E402

_refilled = []


async def _gen(uid, n=5):
    await asyncio.sleep(0)
    _refilled.append(uid)
    return 1


_saved_cs = (cs.pick_starters, cs.pool_fresh_count, cs.generate_starters, pm.is_game_running)
cs.pick_starters = lambda uid, limit=3: [{"id": 1, "text": "hi"}]
cs.pool_fresh_count = lambda uid: 0
cs.generate_starters = _gen
pm.is_game_running = lambda: False


async def _starters():
    out = await chat.get_starters(user=SimpleNamespace(id=1))
    tracked = [t for t in bg._BG_TASKS if t.get_name() == "starters_refill"]
    for t in tracked:
        await t
    return out, tracked


try:
    out, tracked = asyncio.run(_starters())
finally:
    cs.pick_starters, cs.pool_fresh_count, cs.generate_starters, pm.is_game_running = _saved_cs
check("the refill runs as a tracked task named starters_refill", len(tracked) == 1)
check("starters served and the pool refilled",
      out == {"starters": [{"id": 1, "text": "hi"}]} and _refilled == [1]
      and 1 not in chat._starter_gen_running)


# ── A13. semantic_search ────────────────────────────────────────────────────
print("semantic_search.semantic_hits / curated_search")
import src.services.embed_service as es  # noqa: E402
import src.services.facet_index as fi  # noqa: E402
import src.services.semantic_search as ss  # noqa: E402
import src.services.size_norms as size_norms  # noqa: E402
import src.vector_store.chromadb_wrapper as cw  # noqa: E402

TITLES = ["Irregular Witch", "Nanoha", "Raising Project", "Madoka"]


class _Chroma:
    def query(self, query_embeddings=None, n_results=10, where=None):
        n = min(n_results, len(TITLES))
        return {
            "ids": [[f"sonarr:{i}" for i in range(n)]],
            "documents": [[f"doc {t}" for t in TITLES[:n]]],
            "metadatas": [[{"title": t, "genres": "Mahou Shoujo", "themes": "", "year": 2010 + i}
                           for i, t in enumerate(TITLES[:n])]],
            "distances": [[0.1 * (i + 1) for i in range(n)]],
        }

    def get_by_id(self, doc_id):
        return {"id": doc_id, "document": "probe doc",
                "metadata": {"title": "Probe Title", "genres": "Drama"}}


_wl_threads, _size_threads, _dot_threads = [], [], []


def _spy_watched(uid, titles, category=None):
    _wl_threads.append(_on_loop_thread())
    return {t: {"count": 1, "completed": True} for t in titles if t == "Nanoha"}


def _spy_size(**kw):
    _size_threads.append(_on_loop_thread())
    return "[size: 4K]" if kw.get("title") == "Madoka" else ""


_real_dot = ss._dot


def _spy_dot(a, b):
    _dot_threads.append(_on_loop_thread())
    return _real_dot(a, b)


async def _embed_query(q):
    return [1.0, 0.0]


TAGS = {"Irregular Witch": ["Primarily Adult Cast"], "Nanoha": ["Magic"],
        "Raising Project": ["Found Family"], "Madoka": ["Tragedy"],
        "Probe Title": ["Adult Cast Drama"]}
VECS = {"adult cast": [1, 0, 0], "magic": [0, 0, 1], "found family": [0.6, 0, 0.8],
        "tragedy": [0, 1, 0], "primarily adult cast": [1, 0, 0]}


async def _vectors(texts, query_side=False):
    for t in texts:
        k = ss._norm_tag(t)
        if k in VECS:
            ss._vec_memo[k] = VECS[k]
    return {ss._norm_tag(t): VECS.get(ss._norm_tag(t)) for t in texts}


async def _parse(query, domain=None):
    return {"anchor_title": None, "constraints": ["adult cast"], "search_text": query}


_probe = {}


async def _facet_probe(cvecs, domain=None):
    return dict(_probe)


_saved_ss = (ss.watched_lookup, ss._dot, ss._parse_query, ss._candidate_tags,
             ss._texts_to_vectors, size_norms.short_size_tag, cw.get_chroma_db,
             es.embed_query, fi.facet_probe)
ss.watched_lookup, ss._dot, ss._parse_query = _spy_watched, _spy_dot, _parse
ss._candidate_tags = lambda hits, domain=None: [TAGS.get(h["title"], []) for h in hits]
ss._texts_to_vectors = _vectors
size_norms.short_size_tag = _spy_size
cw.get_chroma_db = lambda: _Chroma()
es.embed_query = _embed_query
fi.facet_probe = _facet_probe
try:
    hits = asyncio.run(ss.semantic_hits("magical girls", 3, "anime", user_id=1))
    hits_threads = list(_wl_threads + _size_threads)
    _wl_threads.clear(); _size_threads.clear()
    legacy = asyncio.run(ss.curated_search("adult cast magical girls", 4, "anime", user_id=1))
    legacy_threads = list(_wl_threads + _size_threads + _dot_threads)
    _wl_threads.clear(); _size_threads.clear(); _dot_threads.clear()
    _probe["parent:9"] = {"title": "Probe Title", "genres": "Drama",
                          "sims": {"adult cast": 0.7}, "hits": 2}
    facet = asyncio.run(ss.curated_search("adult cast magical girls", 6, "anime", user_id=1))
    facet_threads = list(_wl_threads + _size_threads + _dot_threads)
finally:
    (ss.watched_lookup, ss._dot, ss._parse_query, ss._candidate_tags,
     ss._texts_to_vectors, size_norms.short_size_tag, cw.get_chroma_db,
     es.embed_query, fi.facet_probe) = _saved_ss
check("semantic_hits: watched + size lookups ran off the loop",
      len(hits_threads) == 4 and not any(hits_threads))
check("semantic_hits: hits unchanged",
      [(h["title"], h["doc_id"], h["score"], h["size_tag"]) for h in hits]
      == [("Irregular Witch", "sonarr:0", 0.9, ""), ("Nanoha", "sonarr:1", 0.8, ""),
          ("Raising Project", "sonarr:2", 0.7, "")]
      and hits[1]["watch_tag"] == "watched")
check("curated_search: both hit builds and the evidence math ran off the loop",
      legacy_threads and any(_t is False for _t in legacy_threads)
      and not any(legacy_threads))
check("curated_search: evidence ranking unchanged",
      legacy["mode"] == "evidence"
      and [(h["title"], h["fit"]) for h in legacy["results"]]
      == [("Irregular Witch", 10), ("Nanoha", 2), ("Raising Project", 1), ("Madoka", 0)]
      and legacy["results"][0]["fit_note"] == "adult cast ↔ Primarily Adult Cast (1.00)"
      and legacy["results"][2]["fit_note"] == "adult cast: unbelegt (best: Found Family 0.60)")
check("curated_search: facet-hit decoration ran off the loop",
      facet_threads and not any(facet_threads))
_ph = next((h for h in facet["results"] if h["title"] == "Probe Title"), None)
check("curated_search: the facet hit is decorated and scored",
      _ph is not None and _ph["watch_tag"] == ss.watch_tag(None) and _ph["size_tag"] == ""
      and _ph["doc_id"] == "parent:9")


# ── A21. recommendations_engine.score_arr_items ─────────────────────────────
print("recommendations_engine.score_arr_items")
import src.services.recommendations_engine as reng  # noqa: E402

with conn_mod.get_db_session() as db:
    db.add(TasteVectorEntry(user_id=1, genre_affinity=json.dumps(
        {"movie": {"genre_affinity": {"Drama": 1.0}}})))
    db.add(EncryptedTasteVector(user_id=1, media_category="movie", salt="",
                                encrypted_blob=json.dumps(
                                    {"embedding": [1.0, 0.0], "disliked_titles": ["Bad"]})))
    db.commit()

_embs = {"a": {"embedding": [1.0, 0.0]}, "b": {"embedding": [0.0, 1.0]},
         "c": {"embedding": [0.6, 0.8]}, "bad": {"embedding": [1.0, 0.0]}}
_items = [{"title": "B", "plex_rating_key": "b"}, {"title": "Bad", "plex_rating_key": "bad"},
          {"title": "Genre", "plex_rating_key": "g", "genres": "Drama"},
          {"title": "A", "plex_rating_key": "a"}, {"title": "C", "plex_rating_key": "c"}]
_nv_threads = []
_real_nv = reng._normalize_vec


def _spy_nv(v):
    _nv_threads.append(_on_loop_thread())
    return _real_nv(v)


_saved_re = (cw.get_chroma_db, reng._normalize_vec)
cw.get_chroma_db = lambda: SimpleNamespace(
    get_by_ids=lambda ids: {i: _embs[i] for i in ids if i in _embs})
reng._normalize_vec = _spy_nv
try:
    ranked = asyncio.run(reng.score_arr_items(1, "movie", _items, top_n=10))
finally:
    cw.get_chroma_db, reng._normalize_vec = _saved_re
# the first call normalises the user's taste vector (cheap, on the loop)
check("every item embedding was normalised and scored off the loop",
      len(_nv_threads) == 5 and _nv_threads[0] is True and not any(_nv_threads[1:]))
check("ranking unchanged: cosine order, dislikes last, genre fallback after",
      [it["title"] for it in ranked] == ["A", "C", "B", "Bad", "Genre"])


# ── A14. taste_engine.embed_entry → _chroma_item_vector ─────────────────────
print("taste_engine.compute_taste_vector_for_user → _chroma_item_vector")
import numpy as np  # noqa: E402

import src.cache.metadata_cache as mc_mod  # noqa: E402
import src.services.taste_engine as te  # noqa: E402

HOUR = 3_600_000
NOW = datetime.utcnow()
with conn_mod.get_db_session() as db:
    for i in range(4):
        db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id=f"sit-{i}",
                                 title=f"Ep {i}", series_title="Sitcom", media_type="show",
                                 genres="Comedy", duration_ms=HOUR, view_offset_ms=HOUR,
                                 completed=True, viewed_at=NOW - timedelta(hours=i)))
        db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id=f"mini-{i}",
                                 title=f"Part {i}", series_title="Miniseries", media_type="show",
                                 genres="Drama", duration_ms=HOUR, view_offset_ms=HOUR,
                                 completed=True, viewed_at=NOW - timedelta(hours=i)))
    db.add(EnrichmentStatus(plex_rating_key="sonarr:1", title="Sitcom",
                            media_category="show", enriched=True, enriched_at=NOW))
    db.add(EnrichmentStatus(plex_rating_key="sonarr:2", title="Miniseries",
                            media_category="show", enriched=True, enriched_at=NOW))
    db.commit()

SIT, MINI = [1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]


def _profile_for(key):
    if "sit" in key or key.endswith("sonarr:1"):
        return {"title": "Sitcom", "genres": ["Comedy"], "embedding_text": "SITCOM"}
    if "mini" in key or key.endswith("sonarr:2"):
        return {"title": "Miniseries", "genres": ["Drama"], "embedding_text": "MINI"}
    return None


class _FakeCache:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def close(self): pass
    def set_cache(self, *a, **k): pass

    def get_cache(self, key):
        if key.startswith("enriched:"):
            p = _profile_for(key)
            return {"response": p} if p else None
        return None

    def get_cache_many(self, keys):
        return {k: self.get_cache(k) for k in keys if self.get_cache(k)}


_embedded = []


async def _fake_embed(texts, profile=None):
    _embedded.extend(texts)
    return [list(SIT) if "SITCOM" in t else list(MINI) for t in texts]


_gbi_threads = []


def _get_by_id(self, doc_id):
    _gbi_threads.append(_on_loop_thread())
    if doc_id == "Sitcom":   # the stored index vector, title-keyed
        return {"embedding": np.asarray(SIT), "metadata": {"title": "Sitcom", "domain": "show"}}
    return None


_saved_te = (te.MetadataCache, mc_mod.MetadataCache, es.embed_documents, cw.get_chroma_db)
te.MetadataCache = mc_mod.MetadataCache = _FakeCache
es.embed_documents = _fake_embed
cw.get_chroma_db = lambda: type("C", (), {
    "get_by_id": _get_by_id, "embeddings_for_domain": lambda s, d, limit=30000: []})()
try:
    res = asyncio.run(te.compute_taste_vector_for_user(1, category="show"))
finally:
    te.MetadataCache, mc_mod.MetadataCache, es.embed_documents, cw.get_chroma_db = _saved_te
check("every stored-vector lookup ran on the Chroma thread",
      _gbi_threads and not any(_gbi_threads))
check("the stored vector was used for the sitcom, the miniseries re-embedded",
      res and res["embedding_items"] == 2
      and not any("SITCOM" in t for t in _embedded) and any("MINI" in t for t in _embedded))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
