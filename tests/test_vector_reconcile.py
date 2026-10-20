"""SQLite <-> ChromaDB reconciliation (2026-10 lifecycle audit).

A curatarr.db restored over a wiped vector store used to go unnoticed: the
pre-filter skips enriched rows, the zombie walk only heals docs without a
profile, and the stats read vector_ready out of SQLite. reconcile_vectors
joins the three stores by each profile's own plex_rating_key (every keying:
Plex ratingKeys, radarr:N, music titles) and heals what is missing.

    python tests/test_vector_reconcile.py
"""
import asyncio
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.cache.metadata_cache as mcache
import src.database.connection as conn_mod
import src.services.media_enricher as me
import src.services.vector_reconcile as vr
import src.vector_store.chromadb_wrapper as cw
from src.config import settings
from src.database.models import Base, EnrichmentStatus, ProactiveMessage, User

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


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


conn_mod.get_db_session = fake_db_session
_tmp = tempfile.TemporaryDirectory()
settings.ENRICHMENT_CACHE = Path(_tmp.name) / "enrichment.db"

present: set = set()      # the fake vector store's doc ids
chroma_broken = [False]
embedded: list = []


def fake_chroma_ids():
    if chroma_broken[0]:
        raise RuntimeError("chroma read failed")
    return set(present)


async def fake_run_chroma(fn, *a, **kw):
    return fn(*a, **kw)


async def fake_store(profile, *, media_type, fallback_title=""):
    embedded.append((vr.doc_id_for(profile), media_type))
    present.add(vr.doc_id_for(profile))
    return True


async def fake_cache_set(key, prof, days=7):
    def _set():     # a worker thread, as the real acache_set
        c = mcache.MetadataCache()
        try:
            c.set_cache(key, prof, days=days)
        finally:
            c.close()
    await asyncio.to_thread(_set)


vr._chroma_ids = fake_chroma_ids
cw.run_chroma = fake_run_chroma
me.store_profile_vector = fake_store
mcache.acache_set = fake_cache_set


def seed(rows, profiles):
    with fake_db_session() as db:
        db.query(EnrichmentStatus).delete()
        db.query(ProactiveMessage).delete()
        if not db.query(User).first():
            db.add(User(id=1, plex_user_id="1", plex_username="admin", is_admin=True))
        for i, (key, cat, ready, error) in enumerate(rows, start=1):
            db.add(EnrichmentStatus(id=i, plex_rating_key=key, title=f"T{i}",
                                    media_category=cat, enriched=True,
                                    vector_ready=ready, error=error))
    c = mcache.MetadataCache()
    try:
        c.conn.execute("DELETE FROM api_cache")
        c.conn.commit()
        for key, prof in profiles.items():
            c.set_cache(key, prof, days=30)
    finally:
        c.close()


def row(key):
    with fake_db_session() as db:
        r = db.query(EnrichmentStatus).filter_by(plex_rating_key=key).one()
        return r.enriched, r.vector_ready


def run(coro):
    return asyncio.run(coro)


# ── 1. a wiped store: every keying is found and healed ──────────────────────
seed([("12345", "movie", True, None),             # a Plex ratingKey
      ("radarr:42", "movie", False, None),        # an arr doc id
      ("777", "music", True, None),               # music artist, no embedding_text
      ("888", "show", True, None),                # profile gone too
      ("999", "show", True, "rule-based only")],  # not "done": never touched
     {"enriched:movie:550": {"title": "Fight Club", "plex_rating_key": "12345",
                             "embedding_text": "a movie", "media_type": "movie"},
      "enriched:movie:603": {"title": "The Matrix", "plex_rating_key": "radarr:42",
                             "embedding_text": "a movie", "media_type": "movie",
                             "vector_pending": True},
      "enriched:music:Boards of Canada": {"title": "Boards of Canada",
                                          "plex_rating_key": "777", "genres": ["idm"]}})
present.clear()
embedded.clear()
done = run(vr.reconcile_vectors())
ids = {d for d, _ in embedded}
check("Plex-keyed, arr-keyed and music docs are all re-embedded",
      ids == {"12345", "radarr:42", "777"})
check("music re-embeds as music (category from the cache key, not 'movie')",
      ("777", "music") in embedded)
check("re-embedded rows read vector_ready=1 again",
      row("12345") == (True, True) and row("radarr:42") == (True, True))
check("a row whose profile is gone too is re-queued for enrichment",
      row("888") == (False, False))
check("a rule-based row (error set) is left alone", row("999") == (True, True))
c = mcache.MetadataCache()
try:
    pend = (c.get_cache("enriched:movie:603") or {}).get("response", {})
finally:
    c.close()
check("a vector_pending profile loses the flag once its vector landed",
      "vector_pending" not in pend)
check("pass reports done when nothing actionable is left", done is True)
with fake_db_session() as db:
    alerts = db.query(ProactiveMessage).filter_by(trigger_type="vector_desync").count()
check("a tiny library raises no desync alert", alerts == 0)

# ── 2. second pass on the healed state is a no-op ────────────────────────────
embedded.clear()
check("a healthy state re-embeds nothing", run(vr.reconcile_vectors()) is True
      and embedded == [])

# ── 3. drift alarm + batch cap + honest flags ────────────────────────────────
N = 400
seed([(f"k{i}", "movie", True, None) for i in range(N)],
     {f"enriched:movie:{i}": {"title": f"M{i}", "plex_rating_key": f"k{i}",
                              "embedding_text": "x", "media_type": "movie"}
      for i in range(N)})
present.clear()
present.update({"k0", "k1"})
embedded.clear()
done = run(vr.reconcile_vectors())
check("the re-embed backlog is capped per run and the task stays due",
      len(embedded) == vr._REEMBED_BATCH and done is False)
with fake_db_session() as db:
    flagged = db.query(EnrichmentStatus).filter_by(vector_ready=True).count()
    alerts = db.query(ProactiveMessage).filter_by(trigger_type="vector_desync").count()
check("vector_ready is cleared on rows still without a doc (stats tell the truth)",
      flagged == 2 + vr._REEMBED_BATCH)
check("a store missing most docs raises exactly one admin alert", alerts == 1)
run(vr.reconcile_vectors())
with fake_db_session() as db:
    alerts = db.query(ProactiveMessage).filter_by(trigger_type="vector_desync").count()
check("the next pass finishes the backlog without a second alert this week",
      alerts == 1 and len(present) == N)

# ── 4. a failed Chroma read is a failure, never "everything is missing" ─────
seed([("a1", "movie", True, None)], {})
chroma_broken[0] = True
try:
    run(vr.reconcile_vectors())
    raised = False
except RuntimeError:
    raised = True
chroma_broken[0] = False
check("an unreadable vector store raises instead of re-queueing the library",
      raised and row("a1") == (True, True))

# ── 5. drift rule ────────────────────────────────────────────────────────────
check("drift: most docs missing in a real library", vr.is_drift(1000, 600))
check("no drift: half present (strict)", not vr.is_drift(1000, 500))
check("no drift: tiny library", not vr.is_drift(10, 10))

# ── 6. boot check clears the stamp only on drift ─────────────────────────────
import src.services.app_state as app_state  # noqa: E402

state = {"job_last_run:vector_reconcile": "2026-10-19T00:00:00"}
app_state.set_state = lambda k, v: state.__setitem__(k, v)


class _CountOnly:
    def __init__(self, n):
        self.n = n

    def get_count(self):
        return self.n


seed([(f"b{i}", "movie", True, None) for i in range(100)], {})
cw.get_chroma_db = lambda: _CountOnly(100)
run(vr.boot_drift_check())
check("boot: matching counts keep the stamp",
      state["job_last_run:vector_reconcile"] == "2026-10-19T00:00:00")
cw.get_chroma_db = lambda: _CountOnly(0)
run(vr.boot_drift_check())
check("boot: an empty store with 100 enriched rows clears the stamp",
      state["job_last_run:vector_reconcile"] == "")

# ── 7. wiring ────────────────────────────────────────────────────────────────
from src.services.data_custodian import _registry  # noqa: E402

t = next((t for t in _registry() if t.job_id == vr.JOB_ID), None)
check("registered as a background wrapper task fed with its card",
      t is not None and t.takes_task and not t.reports_own and not t.needs_llm)
main_src = (Path(__file__).resolve().parents[1] / "src/main.py").read_text(encoding="utf-8")
check("startup runs the drift check", "boot_drift_check()" in main_src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
