"""The embedding migration must never delete the live corpus, and its
rollback must restore the profile that was live before the flip.

Found in the 2026-10 vector/memory audit: v1 (``media_knowledge``) was
dropped on 2026-08-18, so the migration created an EMPTY v1, deleted the
live ``media_knowledge_v2``, found 0 docs to copy and aborted at the eval
gate with the corpus already gone. Rollback re-applied ``_default_profile()``
— which is V2 since the v1 drop — so it changed nothing.

    python tests/test_embedding_migration_guard.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import settings

# A throwaway store: this suite deletes and creates collections.
settings.CHROMADB_PATH = tempfile.mkdtemp(prefix="curatarr-migr-")

import src.services.embedding_migration as em  # noqa: E402
import src.services.embed_service as es  # noqa: E402
import src.services.app_state as app_state  # noqa: E402
import src.vector_store.chromadb_wrapper as cw  # noqa: E402
from src.services.task_monitor import task_monitor  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── preflight (pure) ─────────────────────────────────────────────────────────

V2 = em.V2_PROFILE["collection"]
check("refuses when the target is the live collection",
      em._preflight(V2, {V2, em.LEGACY_COLLECTION}) == "target_is_live_collection")
check("refuses when the v1 source does not exist",
      em._preflight("something_else", {V2}) == "no_legacy_source")
check("allows a run from a live v1 corpus",
      em._preflight(em.LEGACY_COLLECTION, {em.LEGACY_COLLECTION}) is None)


class _Named:
    def __init__(self, name):
        self.name = name


check("collection names: 1.x Collection objects",
      em._collection_names(type("C", (), {"list_collections": lambda s: [_Named("a1b")]})())
      == {"a1b"})
check("collection names: 0.6 plain names",
      em._collection_names(type("C", (), {"list_collections": lambda s: ["a1b"]})())
      == {"a1b"})


# ── end to end against a real store: the live v2 corpus survives ─────────────

es._profile_cache["value"] = dict(em.V2_PROFILE)   # the app runs on v2
live = cw.get_chroma_db()
check("wrapper serves the v2 collection", live.collection_name == V2)
live.upsert_documents(documents=["doc one", "doc two"],
                      embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
                      metadatas=[{"title": "One"}, {"title": "Two"}],
                      ids=["d1", "d2"])


def _run():
    task = task_monitor.create(name="migration guard test", category="test")
    return asyncio.run(em.run_embedding_migration(task))


res = _run()
names = em._collection_names(live.client)
check("run on a live v2 store is refused", res == {"ok": False,
                                                   "reason": "target_is_live_collection"})
check("the live v2 corpus still holds its documents", live.get_count() == 2)
check("no empty v1 collection was minted", em.LEGACY_COLLECTION not in names)

# Live collection elsewhere (e.g. after a rollback), but no v1 to copy from.
es._profile_cache["value"] = dict(em.V2_PROFILE, collection="media_knowledge_v3")
res = _run()
check("missing v1 source is refused", res.get("reason") == "no_legacy_source")
check("…and the v2 corpus was not touched",
      cw.get_chroma_db().client.get_collection(V2).count() == 2)

# v1 exists but is empty — copying nothing must not drop v2 either.
cw.get_chroma_db().client.create_collection(em.LEGACY_COLLECTION,
                                            metadata={"hnsw:space": "cosine"})
res = _run()
check("empty v1 source is refused", res.get("reason") == "legacy_source_empty")
check("…and the v2 corpus was not touched",
      cw.get_chroma_db().client.get_collection(V2).count() == 2)


# ── rollback restores the PREVIOUS profile, not the default ──────────────────

state: dict = {}
flips: list = []
app_state.get_state = lambda k: state.get(k)
app_state.set_state = lambda k, v: state.__setitem__(k, v)
es.set_profile = lambda p: flips.append(p)

es._profile_cache["value"] = dict(em.V2_PROFILE)
check("no recorded previous profile -> refused, nothing flipped",
      em.rollback_embedding_migration()["reason"] == "no_previous_profile_recorded"
      and flips == [])

legacy = {"model": "nomic-embed-text", "collection": em.LEGACY_COLLECTION,
          "prefixes": False, "num_ctx": 8192, "schema": 1}
state[em.PREV_PROFILE_KEY] = json.dumps(legacy)
out = em.rollback_embedding_migration()
check("rollback flips to the recorded previous profile",
      out["ok"] and flips == [legacy])

es._profile_cache["value"] = dict(legacy)
flips.clear()
check("rollback when already on the previous profile is a no-op",
      em.rollback_embedding_migration()["reason"] == "already_on_previous_profile"
      and flips == [])

state[em.PREV_PROFILE_KEY] = "{not json"
check("corrupt recorded profile -> refused",
      em.rollback_embedding_migration()["reason"] == "no_previous_profile_recorded")

# ── wiring: the previous profile is recorded BEFORE the flip ─────────────────

src = (ROOT / "src/services/embedding_migration.py").read_text(encoding="utf-8")
flip = src.index("set_profile(V2_PROFILE)")
check("previous profile saved right before the flip",
      0 < src.index("set_state(PREV_PROFILE_KEY") < flip)
check("preflight runs before any collection is deleted",
      src.index("_preflight(live.collection_name") < src.index("client.delete_collection("))
check("rollback no longer re-applies the default profile",
      "set_profile(_default_profile())" not in src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
