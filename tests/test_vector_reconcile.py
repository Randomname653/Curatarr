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

from sqlalchemy.orm import sessionmaker

import _harness  # noqa: E402,F401  (first: temp stores and .env, never the live data/)
import src.cache.metadata_cache as mcache
import src.database.connection as conn_mod
import src.services.media_enricher as me
import src.services.vector_reconcile as vr
import src.vector_store.chromadb_wrapper as cw
from src.config import settings
from src.database.models import Base, EnrichmentStatus, ProactiveMessage, User

from _checks import check  # noqa: E402


from _checks import memory_engine  # noqa: E402
engine = memory_engine()
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
                                          "plex_rating_key": "777", "genres": ["idm"]},
      # a not-found sentinel is no profile: 888 still counts as knowledge-less
      "enriched:show:888": {"_not_found": True, "plex_rating_key": "888",
                            "title": "T4", "embedding_text": "nothing"}})
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

# ── 3b. one profile, two library entries (Plex watch + Radarr) ──────────────
seed([("5555", "movie", True, None), ("radarr:7", "movie", True, None)],
     {"enriched:movie:27205": {"title": "Inception", "plex_rating_key": "5555",
                               "embedding_text": "dreams", "media_type": "movie"}})
with fake_db_session() as db:       # the Radarr row knows the title the profile has
    db.query(EnrichmentStatus).filter_by(plex_rating_key="radarr:7").update({"title": "Inception"})
present.clear()
embedded.clear()
run(vr.reconcile_vectors())
check("a profile shared by two rows is embedded once",
      embedded == [("5555", "movie")])
check("... and covers both rows (no re-queue of the second one)",
      row("radarr:7") == (True, True) and row("5555") == (True, True))

# ── 3c. a re-queue that re-enrichment cannot cover is not looped ────────────
seed([("orphan", "show", True, None)], {})
present.clear()
run(vr.reconcile_vectors())
check("first pass re-queues the row", row("orphan") == (False, False))
with fake_db_session() as db:       # enrichment ran: cache hit, no doc written
    db.query(EnrichmentStatus).filter_by(plex_rating_key="orphan").update({"enriched": True})
run(vr.reconcile_vectors())
check("second pass leaves it enriched (reported, not re-queued daily)",
      row("orphan") == (True, False))
present.add("orphan")
run(vr.reconcile_vectors())
import src.services.app_state as _as  # noqa: E402
check("a row that healed drops out of the re-queue memory",
      _as.get_state(vr._REQUEUED_KEY) == "[]")

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

from _checks import real_state  # noqa: E402
state = real_state()                    # the real app_state table, not a dict
state["job_last_run:vector_reconcile"] = "2026-10-19T00:00:00"


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

# ── 6b. the pass reports its counts ─────────────────────────────────────────
seed([("r1", "movie", True, None)],
     {"enriched:movie:1": {"title": "R", "plex_rating_key": "r1",
                           "embedding_text": "x", "media_type": "movie"}})
present.clear()
rep = {}
run(vr.reconcile_vectors(report=rep))
check("report carries the counts and the summary line",
      rep.get("reembedded") == 1 and rep.get("left") == 0 and "re-embedded 1" in rep["summary"])

# ── 6c. the deprecated backfill script runs the same pass ───────────────────
import importlib.util  # noqa: E402
import subprocess  # noqa: E402

script = Path(__file__).resolve().parents[1] / "scripts/revectorize_backfill.py"
spec = importlib.util.spec_from_file_location("revectorize_backfill", script)
rb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rb)

passes = []


def fake_pass(outcomes):
    async def _p(task=None, report=None):
        done, rep_ = outcomes[len(passes)]
        passes.append(1)
        report.update(rep_, summary="s")
        return done
    return _p


_real_pass = vr.reconcile_vectors
try:
    passes.clear()
    vr.reconcile_vectors = fake_pass([(False, {"reembedded": 300, "requeued": 0}),
                                      (True, {"reembedded": 12, "requeued": 0})])
    code = run(rb.main(max_passes=10))
    check("script: drains the backlog pass by pass, then exits 0",
          code == 0 and len(passes) == 2)
    passes.clear()
    vr.reconcile_vectors = fake_pass([(False, {"reembedded": 0, "requeued": 0, "left": 9})] * 3)
    code = run(rb.main(max_passes=10))
    check("script: a pass without progress stops it (exit 1), no spinning",
          code == 1 and len(passes) == 1)

    from src.services.data_custodian import TaskFailed  # noqa: E402

    async def _boom(task=None, report=None):
        raise TaskFailed("embedder down")
    vr.reconcile_vectors = _boom
    check("script: a failed pass exits 1", run(rb.main(max_passes=3)) == 1)
finally:
    vr.reconcile_vectors = _real_pass

out = subprocess.run([sys.executable, str(script), "--dry-run"], capture_output=True,
                     text=True, cwd=str(script.parents[1]), timeout=120)
check("script: the removed flags are refused with an explanation, nothing runs",
      out.returncode != 0 and "removed" in out.stderr and "deprecated" in out.stdout)
src_script = script.read_text(encoding="utf-8")
check("script: the plex-key profile lookup is gone",
      "enriched:{row['media_category']}:{row['plex_rating_key']}" not in src_script)

# ── 7. wiring ────────────────────────────────────────────────────────────────
from src.services.data_custodian import _registry  # noqa: E402

t = next((t for t in _registry() if t.job_id == vr.JOB_ID), None)
check("registered as a background wrapper task fed with its card",
      t is not None and t.takes_task and not t.reports_own and not t.needs_llm)
main_src = (Path(__file__).resolve().parents[1] / "src/main.py").read_text(encoding="utf-8")
check("startup runs the drift check", "boot_drift_check()" in main_src)

check.done()
