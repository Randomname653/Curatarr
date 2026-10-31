"""SQLite <-> ChromaDB reconciliation against a REAL Chroma collection.

tests/test_vector_reconcile.py drives the pass through a fake vector store
that keys its docs with ``vr.doc_id_for`` — the reader's own function — so a
writer that stored docs under another id stayed green there. Here only the
embedder (the Ollama boundary) is faked: Chroma, the app's migrated SQLite,
the metadata cache, store_profile_vector and the vector_ready flags are all
real, so the doc id the pass looks for is the one the writer actually wrote.

    python tests/test_vector_reconcile_live.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio
import json

conn = app_db()
from src.services import embed_service  # noqa: E402
embed_service._profile_cache["value"] = {
    "model": "test-embed", "collection": "reconcile_live", "prefixes": False}

from src.cache.metadata_cache import MetadataCache  # noqa: E402
from src.database.models import EnrichmentStatus, User  # noqa: E402
from src.embeddings.embedding_generator import EmbeddingGenerator  # noqa: E402
from src.services import app_state  # noqa: E402
from src.services.data_custodian import TaskFailed  # noqa: E402
import src.services.vector_reconcile as vr  # noqa: E402
import src.vector_store.chromadb_wrapper as cw  # noqa: E402

# The ONLY fake: no Ollama in CI. ``down`` holds the embedding texts that fail.
down: set = set()
embedded: list = []


async def _embed(self, text):
    embedded.append(text)
    return [] if ("*" in down or text in down) else [float(len(text) % 7 + 1)] + [0.0] * 7

EmbeddingGenerator.generate_embedding = _embed

ROWS = {"12345": ("Fight Club", 550, "a fight"), "radarr:42": ("The Matrix", 603, "a simulation")}
with conn.get_db_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="admin", is_admin=True))
    for key, (title, _tmdb, _text) in ROWS.items():
        db.add(EnrichmentStatus(plex_rating_key=key, title=title, media_category="movie",
                                enriched=True, vector_ready=True))
c = MetadataCache()
for key, (title, tmdb, text) in ROWS.items():
    # A real profile carries its TMDB id too; the doc must still land under
    # the library key the status row knows.
    c.set_cache(f"enriched:movie:{tmdb}", {"title": title, "plex_rating_key": key, "tmdb_id": tmdb,
                                           "embedding_text": text, "media_type": "movie"}, days=30)
c.close()


def docs() -> set:
    return set(cw.get_chroma_db().collection.get(include=[])["ids"])


def flags() -> dict:
    with conn.get_db_session() as db:
        return {r.plex_rating_key: (r.enriched, r.vector_ready) for r in db.query(EnrichmentStatus)}


def run(**kw):
    return asyncio.run(vr.reconcile_vectors(**kw))


# ── 1. a wiped store heals into real docs under the ids the pass reads ──────
check("starts with an empty store", docs() == set())
rep: dict = {}
check("the pass reports done", run(report=rep) is True)
check("the real store holds both docs under their library keys",
      docs() == {"12345", "radarr:42"}, f"docs={docs()}")
check("vector_ready matches the store", flags() == {"12345": (True, True), "radarr:42": (True, True)})
check("the report counts what it did", rep.get("reembedded") == 2 and rep.get("failed") == 0, rep)

# ── 2. idempotent on real state ─────────────────────────────────────────────
embedded.clear()
run()
check("a healed store embeds nothing on the next pass", embedded == [], embedded)

# ── 3. embedder fully down: loud failure, honest flags ──────────────────────
cw.get_chroma_db().collection.delete(ids=["12345"])
down.add("*")
try:
    run()
    raised = None
except TaskFailed as e:
    raised = str(e)
check("every re-embed failing raises TaskFailed (the custodian retries)",
      raised is not None and "re-embeds failed" in raised, raised)
check("...and the row without a doc is no longer flagged ready",
      flags()["12345"] == (True, False) and flags()["radarr:42"] == (True, True))
check("...and nothing was re-queued for a full re-enrichment", flags()["12345"][0] is True)
down.clear()
check("embedder back: the next pass heals it", run() is True and "12345" in docs()
      and flags()["12345"] == (True, True))

# ── 4. partial failure: no raise, the failed row stays honest, task stays due
cw.get_chroma_db().collection.delete(ids=["12345", "radarr:42"])
down.add("a fight")
rep = {}
try:
    done = run(report=rep)
    raised = False
except TaskFailed:
    raised = True
check("one of two failing is not a failed task", raised is False)
check("...the pass reports the failure and is not done",
      done is False and rep.get("failed") == 1 and rep.get("reembedded") == 1, rep)
check("...only the landed doc is flagged ready",
      flags() == {"12345": (True, False), "radarr:42": (True, True)} and docs() == {"radarr:42"})
down.clear()
run()
check("...and the next pass finishes the job", docs() == {"12345", "radarr:42"})

# ── 5. a corrupt re-queue memory is read as empty, not a crash ──────────────
app_state.set_state(vr._REQUEUED_KEY, "{not json")
check("a corrupt re-queue memory does not stop the pass", run() is True)

# ── 6. an orphan doc (no status row) is left alone ──────────────────────────
cw.get_chroma_db().collection.upsert(ids=["orphan:1"], embeddings=[[1.0] + [0.0] * 7],
                                     documents=["x"], metadatas=[{"title": "Orphan"}])
before = flags()
check("a doc without a status row neither crashes the pass nor adds a row",
      run() is True and flags() == before and "orphan:1" in docs())

# ── 7. writer and reader share one id ───────────────────────────────────────
check.table("doc_id_for", [
    ("library key wins over TMDB", {"plex_rating_key": "7", "tmdb_id": 1}, "7"),
    ("TMDB without a key", {"tmdb_id": 550}, "550"),
    ("AniList next", {"anilist_id": 9, "title": "X"}, "9"),
    ("title last", {"title": "Boards of Canada"}, "Boards of Canada"),
], vr.doc_id_for)
check("the fallback title is used only when the profile has none",
      vr.doc_id_for({}, "Fallback") == "Fallback" and vr.doc_id_for({"title": "T"}, "F") == "T")
json.dumps(rep)  # the report stays JSON-serialisable for the custodian card

check.done()
