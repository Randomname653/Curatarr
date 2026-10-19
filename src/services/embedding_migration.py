"""
Curatarr — the ONE-SHOT embedding migration (eval package 2).

Builds the v2 corpus (nomic-embed-text-v2-moe, task prefixes, unit norms,
model-tagged metadata) in a PARALLEL Chroma collection, re-embeds the two
DB-side vector spaces (episodic memories, curator principles), gates on a
neighborhood sanity eval, then flips the embedding profile atomically —
model + prefixes + collection switch in one call. The legacy collection
stays untouched for rollback. Never partial: until the flip, the app runs
fully on v1; after it, fully on v2.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

LEGACY_COLLECTION = "media_knowledge"

V2_PROFILE = {
    "model": "nomic-embed-text-v2-moe",
    "collection": "media_knowledge_v2",
    "prefixes": True,
    "num_ctx": 512,      # v2-moe native max sequence length
    "schema": 2,
}

_EVAL_SAMPLE = 30
_EVAL_GATE = 0.8         # similar-beats-random rate required to flip
_BATCH = 200             # docs per source page

# The profile that was live before the flip — what rollback restores.
# _default_profile() is V2 since the 2026-08-18 v1 drop, so "roll back to
# the default" re-applied the profile the rollback was meant to leave.
PREV_PROFILE_KEY = "embedding_profile_prev"


def _cos(a, b) -> float:
    return sum(float(x) * float(y) for x, y in zip(a, b))


def _collection_names(client) -> set:
    """chromadb 0.6 lists names, 1.x lists Collection objects."""
    return {getattr(c, "name", c) for c in client.list_collections()}


def _preflight(live_collection: str, names: set) -> str | None:
    """Why the migration must NOT start, or None. Checked before anything
    is created or deleted: the run below drops the target collection to
    start fresh, and the v1 source was itself dropped on 2026-08-18 — run
    as it stood, the migration created an empty v1, deleted the LIVE v2
    corpus, found 0 docs to copy and aborted at the eval gate with the
    corpus already gone."""
    if live_collection == V2_PROFILE["collection"]:
        return "target_is_live_collection"
    if LEGACY_COLLECTION not in names:
        return "no_legacy_source"
    return None


def _neighborhood_eval(src_collection, dst, sample) -> tuple[int, int]:
    """(hits, tried) for the eval gate. A pair that RAISES counts as tried
    and missed: skipping it let the gate pass on whichever pairs happened to
    survive, so a half-broken v2 corpus could still be flipped live."""
    hits = tried = errors = 0
    for n, (doc_id, old_emb) in enumerate(sample):
        counted = False
        try:
            nb = src_collection.query(query_embeddings=[list(old_emb)],
                                      n_results=3)
            nb_ids = [i for i in (nb.get("ids") or [[]])[0] if i != doc_id]
            if not nb_ids:
                continue
            a = dst.get_by_id(doc_id)
            b = dst.get_by_id(nb_ids[0])
            r = dst.get_by_id(sample[(n + 7) % len(sample)][0])   # unrelated
            if not (a and b and r) or a["embedding"] is None:
                continue
            tried += 1
            counted = True
            if _cos(a["embedding"], b["embedding"]) > _cos(a["embedding"],
                                                           r["embedding"]):
                hits += 1
        except Exception as e:
            if not counted:
                tried += 1
            errors += 1
            if errors == 1:
                logger.warning("[emb-migration] eval pair failed (counted as a "
                               "miss): %r", e, exc_info=True)
            else:
                logger.debug("[emb-migration] eval pair failed: %s", e)
    return hits, tried


async def run_embedding_migration(task) -> dict:
    """Full migration under a task_monitor task. Honors cancellation
    (checked between batches — no flip happens on a cancelled run)."""
    from src.services.app_state import set_state
    from src.services.embed_service import embed_documents, get_profile, set_profile
    from src.services.task_monitor import task_monitor
    from src.vector_store.chromadb_wrapper import ChromaDBWrapper, get_chroma_db

    live = get_chroma_db()
    refusal = _preflight(live.collection_name, _collection_names(live.client))
    if refusal:
        task_monitor.update(task, message=f"NOT migrating: {refusal} — nothing "
                                          "was created or deleted", level="error")
        return {"ok": False, "reason": refusal}

    src = ChromaDBWrapper(LEGACY_COLLECTION)
    total = src.get_count()
    if total == 0:
        task_monitor.update(task, message=f"NOT migrating: {LEGACY_COLLECTION} "
                                          "is empty — nothing was deleted",
                            level="error")
        return {"ok": False, "reason": "legacy_source_empty"}
    task_monitor.update(task, total=total,
                        message=f"Source: {total} docs in {LEGACY_COLLECTION}")

    # ── target collection: fresh every run (re-runs must not mix states) ──
    client = src.client
    try:
        client.delete_collection(V2_PROFILE["collection"])
    except Exception:  # noqa: BLE001 - absent on a first run; create below fails loud
        pass
    dst_coll = client.create_collection(
        name=V2_PROFILE["collection"],
        metadata={"hnsw:space": "cosine",
                  "emb_model": V2_PROFILE["model"],
                  "emb_dim": 768,
                  "text_schema": V2_PROFILE["schema"],
                  "migrated_from": LEGACY_COLLECTION},
    )

    # ── re-embed the corpus, page by page ────────────────────────────────
    done, failed = 0, 0
    offset = 0
    while True:
        if task.status.value == "skipped":
            return {"ok": False, "cancelled": True}
        page = src.collection.get(include=["documents", "metadatas"],
                                  limit=_BATCH, offset=offset)
        ids = page.get("ids") or []
        if not ids:
            break
        docs = page.get("documents") or []
        metas = page.get("metadatas") or []
        vecs = await embed_documents(list(docs), profile=V2_PROFILE)
        ok_idx = [i for i, v in enumerate(vecs) if v]
        failed += len(ids) - len(ok_idx)
        if ok_idx:
            dst_coll.add(
                ids=[ids[i] for i in ok_idx],
                documents=[docs[i] for i in ok_idx],
                embeddings=[vecs[i] for i in ok_idx],
                metadatas=[{**(metas[i] or {}), "emb_model": V2_PROFILE["model"]}
                           for i in ok_idx],
            )
        done += len(ids)
        offset += _BATCH
        task_monitor.update(task, processed=done,
                            message=f"Re-embedded {done}/{total} ({failed} failed)")
    if failed > total * 0.05:
        task_monitor.update(task, message=f"ABORT: {failed} embed failures "
                                          f"(> 5% of corpus) — NOT flipping",
                            level="error")
        return {"ok": False, "reason": "too_many_failures", "failed": failed}

    # ── neighborhood eval gate ───────────────────────────────────────────
    # For sampled docs: their OLD nearest neighbor should still sit closer
    # than an unrelated doc in the NEW space. Sanity, not identity — the
    # new model may (should) re-rank, but similar must beat random.
    sample = []
    page = src.collection.get(include=["documents", "embeddings"],
                              limit=1000, offset=0)
    for i, doc in enumerate(page.get("documents") or []):
        if doc and len(doc) > 80:
            sample.append((page["ids"][i], page["embeddings"][i]))
        if len(sample) >= _EVAL_SAMPLE:
            break
    dst = ChromaDBWrapper(V2_PROFILE["collection"])
    hits, tried = _neighborhood_eval(src.collection, dst, sample)
    rate = (hits / tried) if tried else 0.0
    task_monitor.update(task, message=f"Eval gate: {hits}/{tried} "
                                      f"similar-beats-random ({rate:.0%})")
    if tried < 10 or rate < _EVAL_GATE:
        task_monitor.update(task, message="ABORT: eval gate failed — "
                                          "NOT flipping", level="error")
        return {"ok": False, "reason": "eval_gate", "rate": rate}

    # ── DB-side vector spaces: memories + principles ─────────────────────
    from src.database.connection import get_db_session
    from src.database.models import CuratorPrinciple, EpisodicMemory
    from src.database.runner import run_read, run_write

    # Plain (id, text) pairs out, vectors back in by id: the session used to
    # stay open across every embed call.
    def _load(model_cls, text_attr):
        with get_db_session() as db:
            return [(r.id, getattr(r, text_attr) or "") for r in
                    db.query(model_cls).filter(model_cls.embedding_json.isnot(None)).all()]

    def _store(model_cls, vec_by_id: dict) -> None:
        with get_db_session() as db:
            for r in db.query(model_cls).filter(model_cls.id.in_(list(vec_by_id))).all():
                r.embedding_json = json.dumps(vec_by_id[r.id])
            db.commit()

    mem_n = 0
    for model_cls, text_attr, label in ((EpisodicMemory, "content", "memories"),
                                        (CuratorPrinciple, "text", "principles")):
        rows = await run_read(_load, model_cls, text_attr)
        for i in range(0, len(rows), 32):
            if task.status.value == "skipped":
                return {"ok": False, "cancelled": True}
            chunk = rows[i:i + 32]
            vecs = await embed_documents([text for _, text in chunk],
                                         profile=V2_PROFILE)
            got = {rid: v for (rid, _), v in zip(chunk, vecs) if v}
            mem_n += len(got)
            await run_write(_store, model_cls, got)
        task_monitor.update(task, message=f"Re-embedded {label} "
                                          f"({len(rows)} rows)")

    # ── THE FLIP ─────────────────────────────────────────────────────────
    # Both writes on the writer thread, in this order: the previous profile
    # is on record before the switch.
    def _flip():
        set_state(PREV_PROFILE_KEY, json.dumps(get_profile()))
        set_profile(V2_PROFILE)

    await run_write(_flip)
    task_monitor.update(task, message="Profile flipped — app now runs on "
                                      "v2-moe + prefixes + new collection")

    # ── taste rebuild: lookups against the new corpus, no LLM ────────────
    from src.database.models import User
    from src.services.taste_engine import compute_all_taste_vectors
    def _active_ids():
        with get_db_session() as db:
            return [u_id for (u_id,) in db.query(User.id).filter(User.is_active == True).all()]  # noqa: E712

    user_ids = await run_read(_active_ids)
    for uid in user_ids:
        try:
            await compute_all_taste_vectors(uid, skip_summaries=True)
            task_monitor.update(task, message=f"Taste vectors rebuilt for user {uid}")
        except Exception as e:
            logger.warning("[emb-migration] taste rebuild failed for %d: %s", uid, e)

    return {"ok": True, "docs": done, "failed": failed,
            "eval_rate": round(rate, 3), "db_vectors": mem_n,
            "users_rebuilt": len(user_ids)}


def rollback_embedding_migration() -> dict:
    """Point the profile back at the corpus that was live before the flip.

    Honest caveat: the DB-side memory/principle vectors were rewritten to
    v2 during migration and are NOT rolled back here — after a rollback,
    v1 queries run against v2-stored memory vectors, so memory retrieval
    degrades until the migration runs again. The Chroma path (the big
    one) is fully clean either way."""
    from src.services.app_state import get_state
    from src.services.embed_service import get_profile, set_profile
    try:
        prev = json.loads(get_state(PREV_PROFILE_KEY) or "{}")
    except (TypeError, ValueError):
        prev = {}
    if not prev.get("collection") or not prev.get("model"):
        return {"ok": False, "reason": "no_previous_profile_recorded"}
    if prev.get("collection") == get_profile().get("collection"):
        return {"ok": False, "reason": "already_on_previous_profile"}
    set_profile(prev)
    return {"ok": True, "profile": prev["collection"],
            "caveat": "memory/principle vectors remain v2 until re-run"}
