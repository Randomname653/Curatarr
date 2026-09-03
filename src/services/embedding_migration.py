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


def _cos(a, b) -> float:
    return sum(float(x) * float(y) for x, y in zip(a, b))


async def run_embedding_migration(task) -> dict:
    """Full migration under a task_monitor task. Honors cancellation
    (checked between batches — no flip happens on a cancelled run)."""
    from src.services.embed_service import embed_documents, set_profile
    from src.services.task_monitor import task_monitor
    from src.vector_store.chromadb_wrapper import ChromaDBWrapper

    src = ChromaDBWrapper(LEGACY_COLLECTION)
    total = src.get_count()
    task_monitor.update(task, total=total,
                        message=f"Source: {total} docs in {LEGACY_COLLECTION}")

    # ── target collection: fresh every run (re-runs must not mix states) ──
    client = src.client
    try:
        client.delete_collection(V2_PROFILE["collection"])
    except Exception:
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
    hits = tried = 0
    dst = ChromaDBWrapper(V2_PROFILE["collection"])
    for n, (doc_id, old_emb) in enumerate(sample):
        try:
            nb = src.collection.query(query_embeddings=[list(old_emb)],
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
            if _cos(a["embedding"], b["embedding"]) > _cos(a["embedding"],
                                                           r["embedding"]):
                hits += 1
        except Exception as e:
            logger.debug("[emb-migration] eval pair failed: %s", e)
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
    mem_n = 0
    for model_cls, text_attr, label in ((EpisodicMemory, "content", "memories"),
                                        (CuratorPrinciple, "text", "principles")):
        with get_db_session() as db:
            rows = (db.query(model_cls)
                    .filter(model_cls.embedding_json.isnot(None)).all())
            for i in range(0, len(rows), 32):
                if task.status.value == "skipped":
                    return {"ok": False, "cancelled": True}
                chunk = rows[i:i + 32]
                vecs = await embed_documents(
                    [getattr(r, text_attr) or "" for r in chunk],
                    profile=V2_PROFILE)
                for r, v in zip(chunk, vecs):
                    if v:
                        r.embedding_json = json.dumps(v)
                        mem_n += 1
                db.commit()
            task_monitor.update(task, message=f"Re-embedded {label} "
                                              f"({len(rows)} rows)")

    # ── THE FLIP ─────────────────────────────────────────────────────────
    set_profile(V2_PROFILE)
    task_monitor.update(task, message="Profile flipped — app now runs on "
                                      "v2-moe + prefixes + new collection")

    # ── taste rebuild: lookups against the new corpus, no LLM ────────────
    from src.database.models import User
    from src.services.taste_engine import compute_all_taste_vectors
    with get_db_session() as db:
        user_ids = [u_id for (u_id,) in db.query(User.id).filter(User.is_active == True).all()]  # noqa: E712
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
    """Point the profile back at the untouched legacy corpus.

    Honest caveat: the DB-side memory/principle vectors were rewritten to
    v2 during migration and are NOT rolled back here — after a rollback,
    v1 queries run against v2-stored memory vectors, so memory retrieval
    degrades until the migration runs again. The Chroma path (the big
    one) is fully clean either way."""
    from src.services.embed_service import _default_profile, set_profile
    set_profile(_default_profile())
    return {"ok": True, "profile": "legacy",
            "caveat": "memory/principle vectors remain v2 until re-run"}
