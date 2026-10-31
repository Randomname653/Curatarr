"""Silent-failure sweep, round 3 — the Medium items of §2 in
docs/SILENT_FAILURE_SWEEP_2026_10.md (memory, vectors, chat, LLM lane).

Behavioural checks fail on the code before this change:
  * embed_service: a failed profile read is served, NOT cached for the process
  * chroma wrapper: dimension-mismatch / bad-argument errors are WARNED
    (still answered empty); a failed collection switch is a WARNING
  * semantic_search: a hit-building bug is a WARNING, not a silent []; the
    tag-vector memo is dropped on a model change; _dot never truncates
  * corpus_repair: one upsert (no delete-then-add), honest False on failure
  * episodic_memory: dimension mismatch scores like "no embedding"; a
    conflict-verdict writer bug is a WARNING; the flush reports success
  * facet_index: a failed facet write keeps the backfill cursor
  * chat: flip-flop probe failure skips the detector; protection-check bugs
    WARN; "New chat" refuses (409) to delete a thread whose memories failed
    to save unless force=1
  * curator_principles: a failed novelty check stores nothing
  * llm_priority: /api/ps failure is "unknown" (None), never "evicted"
  * app_state: set_state / release_state_lock report a write that did not land

    python tests/test_silent_failures_r3_memory.py
"""
import asyncio
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import src.database.connection as conn  # noqa: E402
import src.services.app_state as app_state  # noqa: E402
from src.config import settings  # noqa: E402
from src.database.models import AppState, Base, ConversationMessage  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


from _checks import memory_engine, with_users  # noqa: E402
engine = memory_engine()
Base.metadata.create_all(engine)
with_users(engine, 1, 2, 9001)
Session = sessionmaker(bind=engine)


@contextmanager
def _session():
    db = Session()
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextmanager
def _logs(name):
    """Capture one logger's records even while the runner disables logging."""
    prev = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    lg = logging.getLogger(name)
    h, old = _Cap(), lg.level
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    try:
        yield h.records
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)
        logging.disable(prev)


def _warned(records, needle=""):
    return any(r.levelno >= logging.WARNING and needle in r.getMessage()
               for r in records)


@contextmanager
def _patched(obj, **attrs):
    old = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


# ── 1. embed_service: no caching of a failed profile read ───────────────────

def test_embed_profile_failed_read_not_cached():
    import src.services.embed_service as es
    stored = {"model": "next-embed", "collection": "media_knowledge_v3"}
    calls = {"n": 0}

    def flaky_get_state(key):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("SELECT", {}, Exception("database is locked"))
        return __import__("json").dumps(stored)

    es._profile_cache["value"] = None
    try:
        with _patched(app_state, get_state=flaky_get_state), _logs(es.__name__) as recs:
            first = es.get_profile()
            cached_after_fail = es._profile_cache["value"]
            second = es.get_profile()
    finally:
        es._profile_cache["value"] = None
    check("failed profile read serves the default", first.get("model") != "next-embed")
    check("...and does NOT cache it", cached_after_fail is None)
    check("the next call reads the stored profile",
          second.get("collection") == "media_knowledge_v3")
    check("failed profile read is a WARNING", _warned(recs, "profile read failed"))


# ── 2. chroma wrapper: caller errors warned, switch failure warned ──────────

def test_chroma_query_dimension_mismatch_warns():
    import src.vector_store.chromadb_wrapper as cw
    from chromadb.errors import InvalidArgumentError

    class _Coll:
        def query(self, **k):
            raise InvalidArgumentError("Collection expecting embedding with dimension of 768, got 384")

        def get(self, **k):
            raise ValueError("bad id")

        def count(self):
            return 1

    w = object.__new__(cw.ChromaDBWrapper)
    w.collection = _Coll()
    w._phrases = _Coll()            # the facet index: one point per phrase
    cw._caller_error_counts.clear()
    with _logs(cw.__name__) as recs:
        res = w.query([[0.1] * 384], n_results=3)
        got = w.get_by_id("radarr:1")
        fres = w.facets_query([[0.1] * 384])
    check("dimension mismatch still answers an empty result",
          res["ids"] == [[]] and got is None and fres["ids"] == [[]])
    check("dimension mismatch on query is a WARNING", _warned(recs, "dimension of 768"))
    check("bad argument on get_by_id is a WARNING", _warned(recs, "bad id"))
    check("facets_query caller error is a WARNING",
          any(r.levelno >= logging.WARNING and "facets_query" in r.getMessage()
              for r in recs))

    # Rate limit: a per-item loop must not flood the log.
    cw._caller_error_counts.clear()
    with _logs(cw.__name__) as recs:
        for _ in range(10):
            w.query([[0.1]], n_results=1)
    check("repeat caller errors are rate-limited (1 warning per 10 hits)",
          sum(1 for r in recs if r.levelno >= logging.WARNING
              and "rejected its arguments" in r.getMessage()) == 1)

    class _Down:
        def query(self, **k):
            raise RuntimeError("segment reader hiccup")
    w.collection = _Down()
    cw._caller_error_counts.clear()
    with _logs(cw.__name__) as recs:
        w.query([[0.1]], n_results=1)
    check("a backend hiccup stays below WARNING (counted by the fallback tally)",
          not any(r.levelno >= logging.WARNING and "rejected" in r.getMessage()
                  for r in recs))


def test_chroma_collection_switch_failure_warns():
    import src.vector_store.chromadb_wrapper as cw

    class _Inst:
        collection_name = "media_knowledge_v2"

        def use_collection(self, name):
            raise RuntimeError("cannot open media_knowledge_v3")

    inst = _Inst()
    with _patched(cw, _cached_wrapper=lambda: inst,
                  _wanted_collection=lambda: "media_knowledge_v3"), \
            _logs(cw.__name__) as recs:
        got = cw.get_chroma_db()
    check("failed switch still returns the wrapper", got is inst)
    check("failed collection switch is a WARNING naming the old collection",
          _warned(recs, "media_knowledge_v2"))


# ── 3. semantic_search ───────────────────────────────────────────────────────

def test_semantic_hits_builder_bug_warns():
    import src.services.embed_service as es
    import src.services.semantic_search as ss
    import src.vector_store.chromadb_wrapper as cw

    async def _q(text):
        return [1.0, 0.0]

    class _C:
        def query(self, **k):
            return {"ids": [["a"]], "documents": [["d"]],
                    "metadatas": [[{"title": "T"}]], "distances": [[0.1]]}

    def _boom(*a, **k):
        raise KeyError("watched_lookup bug")

    with _patched(es, embed_query=_q), _patched(cw, get_chroma_db=lambda: _C()), \
            _patched(ss, _hits_from_results=_boom), _logs(ss.__name__) as recs:
        hits = asyncio.run(ss.semantic_hits("dark fantasy", n_results=3))
    check("hit-building bug still answers []", hits == [])
    check("hit-building bug is a WARNING (not an outage DEBUG)",
          _warned(recs, "building hits failed"))

    async def _down(text):
        raise ConnectionError("ollama down")
    with _patched(es, embed_query=_down), _logs(ss.__name__) as recs:
        hits = asyncio.run(ss.semantic_hits("dark fantasy"))
    check("an embedder outage stays quiet (DEBUG) and answers []",
          hits == [] and not _warned(recs))


def test_semantic_dot_and_memo_model_key():
    import src.cache.metadata_cache as mcache
    import src.services.embed_service as es
    import src.services.semantic_search as ss

    check("_dot of equal-length unit vectors unchanged", ss._dot([1.0, 0.0], [1.0, 0.0]) == 1.0)
    ss._dot_mismatch_warned.clear()
    with _logs(ss.__name__) as recs:
        mixed = ss._dot([1.0, 0.0], [1.0, 0.0, 0.0])
        ss._dot([1.0, 0.0], [1.0, 0.0, 0.0])
    check("length mismatch scores 0.0 instead of a truncated cosine", mixed == 0.0)
    check("length mismatch warned exactly once",
          sum(1 for r in recs if r.levelno >= logging.WARNING) == 1)

    posted = []

    async def _post(model, inputs, num_ctx):
        posted.append(model)
        return [[0.0, 1.0, 0.0] for _ in inputs]

    class _NoCache:
        def __init__(self):
            raise RuntimeError("no cache in this test")

    ss._vec_memo.clear()
    ss._vec_memo_model["model"] = "old-embed"
    ss._vec_memo["gore"] = [1.0, 0.0]          # an OLD-model vector
    try:
        with _patched(es, get_profile=lambda: {"model": "new-embed", "prefixes": False},
                      _post_embed=_post), _patched(mcache, MetadataCache=_NoCache):
            out = asyncio.run(ss._texts_to_vectors(["Gore"]))
    finally:
        ss._vec_memo.clear()
        ss._vec_memo_model["model"] = None
    check("a model change re-embeds instead of serving the old-model vector",
          posted == ["new-embed"] and out.get("gore") == [0.0, 1.0, 0.0])


# ── 4. corpus_repair: upsert, honest failure ─────────────────────────────────

def _corpus_repair_env(chroma):
    import src.cache.metadata_cache as mcache
    import src.services.embed_service as es
    import src.services.facet_index as fi
    import src.vector_store.chromadb_wrapper as cw

    class _MC:
        conn = None

        def get_cache(self, key):
            return {"response": {
                "title": "Batman Beyond", "year": 1999, "media_type": "tv",
                "overview": "In a cyberpunk future Gotham, teenager Terry McGinnis becomes the new Batman.",
                "keywords": ["cyberpunk"], "genres": ["Animation"]}}

        def close(self):
            pass

    async def _embed(texts, profile=None):
        return [[1.0, 0.0] for _ in texts]

    facet_writes = []

    async def _wf(*a, **k):
        facet_writes.append(a[0])
        return 1

    return (_patched(mcache, MetadataCache=_MC), _patched(es, embed_documents=_embed),
            _patched(fi, write_facets=_wf), _patched(cw, get_chroma_db=lambda: chroma),
            facet_writes)


def test_corpus_repair_upserts_and_reports_failure():
    import src.services.corpus_repair as cr

    class _Coll:
        def __init__(self):
            self.deleted = []

        def delete(self, ids):
            self.deleted += ids

        def add(self, **k):
            raise AssertionError("add must not be used")

    class _Chroma:
        def __init__(self, fail):
            self.collection = _Coll()
            self.fail = fail
            self.upserts = []

        def upsert_documents(self, documents, embeddings, metadatas, ids):
            if self.fail:
                raise RuntimeError("chroma write failed")
            self.upserts.append(ids[0])
            return True

    ok_chroma = _Chroma(fail=False)
    a, b, c, d, facets = _corpus_repair_env(ok_chroma)
    with a, b, c, d:
        ok = asyncio.run(cr.rebuild_doc_from_prefetch("sonarr:629", "tv"))
    check("rebuild writes through ONE upsert",
          ok is True and ok_chroma.upserts == ["sonarr:629"])
    check("rebuild never deletes the doc first", ok_chroma.collection.deleted == [])

    bad_chroma = _Chroma(fail=True)
    a, b, c, d, facets = _corpus_repair_env(bad_chroma)
    with a, b, c, d, _logs(cr.__name__) as recs:
        ok = asyncio.run(cr.rebuild_doc_from_prefetch("sonarr:629", "tv"))
    check("a failed write returns False with the old doc never deleted",
          ok is False and bad_chroma.collection.deleted == [])
    check("a failed write skips the facet refresh", facets == [])
    check("a failed write is a WARNING", _warned(recs, "write failed"))


# ── 5. episodic_memory ───────────────────────────────────────────────────────

def test_cosine_mismatch_is_no_embedding():
    import src.services.episodic_memory as em
    em._dim_mismatch_warned.clear()
    with _logs(em.__name__) as recs:
        sim = em._cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])
        em._cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])
    check("dimension mismatch is None (no embedding), not 0.0", sim is None)
    check("dimension mismatch warned once",
          sum(1 for r in recs if r.levelno >= logging.WARNING) == 1)
    check("matching dimensions unchanged",
          abs(em._cosine_similarity([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-9)
    src = (ROOT / "src/services/episodic_memory.py").read_text(encoding="utf-8")
    check("retrieval scores a mismatch with the same neutral as no embedding",
          "if sem_score is None:\n                        sem_score = _NEUTRAL_SEM" in src)


def test_embed_failure_warns():
    import src.services.embed_service as es
    import src.services.episodic_memory as em

    async def _boom(texts, profile=None):
        raise RuntimeError("profile corrupt")
    with _patched(es, embed_documents=_boom), _logs(em.__name__) as recs:
        v = asyncio.run(em._embed("the user hates laugh tracks"))
    check("memory embed failure → None", v is None)
    check("memory embed failure is a WARNING", _warned(recs, "embedding failed"))


def test_conflict_writer_bug_warns():
    import src.database.runner as runner
    import src.services.episodic_memory as em

    async def _read(fn, *a, **k):
        return [(11, "The user loves laugh tracks.")]

    async def _write(fn, *a, **k):
        raise AttributeError("ORM writer bug")

    class _Resp:
        status_code = 200

        def json(self):
            return {"message": {"content": '{"status": "CONTRADICTION", "reason": "x"}'}}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    with _patched(runner, run_read=_read, run_write=_write), \
            _patched(em.httpx, AsyncClient=_Client), _logs(em.__name__) as recs:
        asyncio.run(em.resolve_memory_conflicts(
            1, 12, "The user hates laugh tracks.", {"title": "Friends"}))
    check("conflict-verdict writer bug is a WARNING (not an LLM-hiccup DEBUG)",
          _warned(recs, "could not be applied"))


def test_flush_reports_success():
    import src.services.episodic_memory as em
    results = iter([False, True, None])

    async def _extract(uid, tid, media_category=None):
        return next(results)

    async def _raises(uid, tid, media_category=None):
        raise RuntimeError("boom")

    with _patched(em, extract_memories_from_thread=_extract):
        failed = asyncio.run(em.flush_thread_extraction(1, "t-fail"))
        ok = asyncio.run(em.flush_thread_extraction(1, "t-ok"))
        legacy = asyncio.run(em.flush_thread_extraction(1, "t-none"))
    with _patched(em, extract_memories_from_thread=_raises), _logs(em.__name__):
        crashed = asyncio.run(em.flush_thread_extraction(1, "t-crash"))
    check("flush returns False when extraction failed", failed is False)
    check("flush returns True on success / nothing to do", ok is True and legacy is True)
    check("flush returns False when extraction raised", crashed is False)

    # Nothing to extract → True (the delete is safe).
    with _patched(em, get_db_session=_session), _patched(conn, get_db_session=_session), \
            _patched(app_state, get_db_session=_session):
        nothing = asyncio.run(em.extract_memories_from_thread(424242, "t-empty"))
    check("extraction with nothing new reports True", nothing is True)


# ── 6. facet_index: a failed write keeps the backfill cursor ─────────────────

def test_facet_backfill_failed_write_keeps_cursor():
    import src.services.embed_service as es
    import src.services.facet_index as fi
    import src.vector_store.chromadb_wrapper as cw

    class _Chroma:
        def __init__(self):
            self.fail = True
            self.added = []

        def facets_delete_by_parent(self, parent):
            return True

        def facets_add(self, documents, embeddings, metadatas, ids):
            if self.fail:
                return False
            self.added.append(ids)
            return True

        def facets_replace(self, parent_id, documents, embeddings, metadatas, ids):
            return self.facets_add(documents, embeddings, metadatas, ids)

        def facets_count(self):
            return len(self.added)

        def get_count(self):
            return 2

        @property
        def collection(self):
            class _C:
                def get(self, include=None, limit=500, offset=0):
                    if offset:
                        return {"ids": [], "metadatas": []}
                    return {"ids": ["x1", "x2"],
                            "metadatas": [{"title": "T1", "domain": "anime", "themes": "alpha"},
                                          {"title": "T2", "domain": "anime", "themes": "beta"}]}
            return _C()

    async def _embed(texts, profile=None):
        return [[1.0, 0.0] for _ in texts]

    state = {}
    fake = _Chroma()
    with _patched(cw, get_chroma_db=lambda: fake), _patched(es, embed_documents=_embed), \
            _patched(app_state, get_state=state.get,
                     set_state=lambda k, v: state.__setitem__(k, v)), \
            _logs(fi.__name__) as recs:
        done = asyncio.run(fi.run_facet_backfill())
        cursor_after_fail = state.get(fi._BACKFILL_CURSOR_KEY)
        done_flag_after_fail = state.get(fi._BACKFILL_DONE_KEY)
        fake.fail = False
        done2 = asyncio.run(fi.run_facet_backfill())
    check("a failed facet write ends the tick unfinished", done is False)
    check("...without advancing the cursor or stamping done",
          cursor_after_fail is None and done_flag_after_fail is None)
    check("a failed facet write is a WARNING", _warned(recs, "cursor kept"))
    check("the page is retried and completes once the write works",
          done2 is True and state.get(fi._BACKFILL_DONE_KEY) == "1"
          and len(fake.added) == 2)

    # Embedder down for a whole page → no advance either.
    async def _down(texts, profile=None):
        return [None for _ in texts]
    state.clear()
    fake.added.clear()
    with _patched(cw, get_chroma_db=lambda: fake), _patched(es, embed_documents=_down), \
            _patched(app_state, get_state=state.get,
                     set_state=lambda k, v: state.__setitem__(k, v)), _logs(fi.__name__):
        done = asyncio.run(fi.run_facet_backfill())
    check("embedder down for a page keeps the cursor",
          done is False and state.get(fi._BACKFILL_CURSOR_KEY) is None)

    check("write_facets keeps its int contract on failure",
          asyncio.run(_write_facets_int(fi, cw, es)) == 0)


async def _write_facets_int(fi, cw, es):
    class _Bad:
        def facets_delete_by_parent(self, p):
            return True

        def facets_add(self, **k):
            return False

    async def _embed(texts, profile=None):
        return [[1.0, 0.0] for _ in texts]
    with _patched(cw, get_chroma_db=lambda: _Bad()), _patched(es, embed_documents=_embed), \
            _logs(fi.__name__):
        return await fi.write_facets("s:1", "T", "anime", "", ["alpha"])


# ── 7. chat ──────────────────────────────────────────────────────────────────

def test_flipflop_probe_failure_skips_detection():
    import src.routers.chat as chat
    import src.services.episodic_memory as em

    def _broken():
        raise OperationalError("SELECT", {}, Exception("disk I/O error"))

    called = []

    async def _detect(*a, **k):
        called.append(a)
        return "protected"

    with _patched(conn, get_db_session=_broken), \
            _patched(em, detect_and_handle_protection=_detect), _logs(chat.__name__) as recs:
        probe = chat._is_in_flipflop_loop(1, "Friends", thread_id="deletion_proposal:3")
        asyncio.run(chat._check_protection_intent_bg(
            1, "fine, keep it", "Verdict: retain", anchor_title="Friends",
            thread_id="deletion_proposal:3"))
    check("flip-flop probe failure reads as UNSETTLED", probe is True)
    check("...so the protection detector does not run this turn", called == [])
    check("flip-flop probe failure is a WARNING", _warned(recs, "flip-flop probe failed"))


def test_protection_check_bug_warns():
    import src.routers.chat as chat
    import src.services.episodic_memory as em

    async def _detect(*a, **k):
        raise TypeError("prompt builder bug")

    with _patched(em, detect_and_handle_protection=_detect), _logs(chat.__name__) as recs:
        asyncio.run(chat._check_protection_intent_bg(1, "keep it", "ok"))
    check("a protection-detector bug is a WARNING with traceback",
          any(r.levelno >= logging.WARNING and r.exc_info
              and "Protection intent check failed" in r.getMessage() for r in recs))
    src = (ROOT / "src/routers/chat.py").read_text(encoding="utf-8")
    check("stance blocks + learned principles log at WARNING",
          'logger.warning("[chat] stance check (deletion) failed' in src
          and 'logger.warning("[chat] stance check (resolution log) failed' in src
          and 'logger.warning("[chat] stance check (protected) failed' in src
          and 'logger.warning("[chat] learned-principles injection failed' in src)


def test_clear_history_refuses_when_flush_failed():
    from fastapi import HTTPException

    import src.routers.chat as chat
    import src.services.episodic_memory as em

    uid = 9001
    with _session() as db:
        for i in range(3):
            db.add(ConversationMessage(user_id=uid, role="user", content=f"m{i}",
                                       thread_id="deletion_proposal:7"))

    def _count():
        with _session() as db:
            return db.query(ConversationMessage).filter(
                ConversationMessage.user_id == uid).count()

    flush_ok = {"v": False}

    async def _flush(user_id, thread_id, media_category=None):
        return flush_ok["v"]

    user = SimpleNamespace(id=uid)
    status = None
    # clear_chat_history opens its own session (src.database.connection)
    with _patched(em, flush_thread_extraction=_flush), _logs(chat.__name__), \
            _patched(conn, get_db_session=_session):
        try:
            asyncio.run(chat.clear_chat_history(
                thread_id="deletion_proposal:7", force=False, user=user))
        except HTTPException as e:
            status, detail = e.status_code, e.detail
        check("failed memory flush → 409, nothing deleted",
              status == 409 and _count() == 3)
        check("409 explains and names the force escape",
              status == 409 and "could not be saved" in detail and "force=1" in detail)

        res = asyncio.run(chat.clear_chat_history(
            thread_id="deletion_proposal:7", force=True, user=user))
        check("force=1 clears anyway", res["deleted"] == 3 and _count() == 0)

        with _session() as db2:
            db2.add(ConversationMessage(user_id=uid, role="user", content="x",
                                        thread_id="general"))
        flush_ok["v"] = True
        res = asyncio.run(chat.clear_chat_history(
            thread_id="general", force=False, user=user))
        check("a successful flush clears without force", res["deleted"] == 1)

    js = (ROOT / "frontend/js/chat.js").read_text(encoding="utf-8")
    check("New chat surfaces the 409 and offers a forced clear",
          "e.status !== 409" in js and "&force=1" in js)


# ── 8. curator_principles: failed novelty check stores nothing ──────────────

def test_principles_novelty_failure_fails_closed():
    import src.services.curator_principles as cp

    calls = []

    async def _cj(system, user, schema, num_predict=800, host=None):
        calls.append(system)
        if len(calls) == 1:
            return {"principles": [{"principle": "Keep comfort rewatches.", "basis": "user"}]}
        raise ConnectionError("curator down")

    stored = []

    @contextmanager
    def _no_db():
        stored.append("opened")
        yield None

    async def _emb(text):
        return None

    with _patched(settings, PRINCIPLES_ENABLED=True), \
            _patched(cp, _thread_text=lambda u, t: ("OWNER: x\nCURATOR: y", 10),
                     _curator_json=_cj, _existing_context=lambda u: ([(1, "r")], "", []),
                     _embed=_emb, get_db_session=_no_db), _logs(cp.__name__) as recs:
        out = asyncio.run(cp.capture_principles_from_thread(1, "deletion_proposal:5"))
    check("novelty failure stores NO candidate", out == [] and stored == [])
    check("novelty failure is a WARNING", _warned(recs, "capture skipped"))

    calls.clear()

    async def _cj_empty(system, user, schema, num_predict=800):
        calls.append(system)
        if len(calls) == 1:
            return {"principles": [{"principle": "Keep comfort rewatches.", "basis": "user"}]}
        return {}   # unparseable verdict JSON
    with _patched(settings, PRINCIPLES_ENABLED=True), \
            _patched(cp, _thread_text=lambda u, t: ("OWNER: x\nCURATOR: y", 10),
                     _curator_json=_cj_empty, _existing_context=lambda u: ([], "", []),
                     _embed=_emb, get_db_session=_no_db), _logs(cp.__name__):
        out = asyncio.run(cp.capture_principles_from_thread(1, "deletion_proposal:5"))
    check("an empty verdict set also stores nothing", out == [] and stored == [])


# ── 9. llm_priority: /api/ps failure is unknown, not "evicted" ──────────────

def test_loaded_models_unknown_on_failure():
    import src.services.llm_priority as lp

    class _Down:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            raise ConnectionError("ollama down")

        async def post(self, *a, **k):
            return SimpleNamespace(status_code=200)

    lp._ps_failing = False
    with _patched(lp.httpx, AsyncClient=_Down), _logs(lp.__name__) as recs:
        loaded = asyncio.run(lp.loaded_models())
        confirmed = asyncio.run(lp._evict_model("curatarr-summarizer", timeout=0.5))
    lp._ps_failing = False
    check("/api/ps failure → None (unknown), not []", loaded is None)
    check("eviction is NOT confirmed while /api/ps is unreadable", confirmed is False)
    check("no false 'Confirmed evicted' log",
          not any("Confirmed evicted" in r.getMessage() for r in recs))
    check("/api/ps failure warned once per outage",
          sum(1 for r in recs if r.levelno >= logging.WARNING
              and "/api/ps unreadable" in r.getMessage()) == 1)

    evicted = []

    async def _none(url=None):
        return None

    async def _ev(name, timeout=8.0):
        evicted.append(name)
        return True
    with _patched(lp, loaded_models=_none, _evict_model=_ev), \
            _patched(settings, CURATOR_MODEL="cur", SUMMARIZER_MODEL="summ",
                     PITCHER_MODEL=""), _logs(lp.__name__) as recs:
        asyncio.run(lp.evict_others("cur"))
        asyncio.run(lp.evict_if_resident("summ"))
    check("unknown residency evicts nothing blind (would LOAD absent models)",
          evicted == [])
    check("unknown residency is a WARNING, not 'no other big model resident'",
          _warned(recs, "residency unknown")
          and not any("no other big model resident" in r.getMessage() for r in recs))


# ── 10. app_state: a write that did not land is reported ────────────────────

def test_release_state_lock_reports_failure():
    with _patched(app_state, get_db_session=_session):
        check("set_state reports a landed write", app_state.set_state("r3_k", "1") is True)
        check("release_state_lock reports a landed release",
              app_state.release_state_lock("r3_k") is True)
        with _session() as db:
            val = db.query(AppState).filter(AppState.key == "r3_k").first().value
        check("...and the key is '0'", val == "0")

    @contextmanager
    def _locked():
        raise OperationalError("UPDATE", {}, Exception("database is locked"))
        yield  # pragma: no cover

    with _patched(app_state, get_db_session=_locked, force_set_state=lambda *a, **k: False), \
            _logs(app_state.__name__) as recs:
        wrote = app_state.set_state("r3_lock", "0")
        released = app_state.release_state_lock("r3_lock")
    check("set_state returns False when the database stayed locked", wrote is False)
    check("release_state_lock returns False when the release did not land",
          released is False)
    check("a release that did not land is logged at ERROR",
          any(r.levelno >= logging.ERROR and "NOT released" in r.getMessage()
              for r in recs))

    async def _aset():
        with _patched(app_state, get_db_session=_session):
            return await app_state.aset_state("r3_async", "x")
    check("aset_state passes the result through", asyncio.run(_aset()) is True)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            fn()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
