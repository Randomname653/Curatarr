"""Curatarr — facet index: multi-vector item representation (stage 1).

One embedding per title averages its whole description into a single
point — a contrast query ("cute pastel" + "sociopathic manipulation")
then searches BETWEEN two regions, where bland averages also live
(measured in search round 9). Stage 1 indexes each title's ~6 theme
phrases as individual points in a SEPARATE collection (media_facets_v1),
so a title can stand in the cute region AND the psycho region at once.
Retrieval gains resolution; the deterministic tag-evidence scoring stays
the truth layer.

The separate collection is load-bearing, not cosmetic: mixing facets
into media_knowledge_v2 would break n_results slot math on all five
query sites, let the anchor resolver return a facet vector, silently
truncate embeddings_for_domain's 30k-cap calibration pass (music alone
is 16k docs), and double count the two id-prefix coverage UIs.
"""

import logging

logger = logging.getLogger(__name__)

FACET_KIND = "theme"
_BACKFILL_CURSOR_KEY = "facet_backfill_offset"
_BACKFILL_DONE_KEY = "facet_backfill_done"
_PAGE = 500


def _phrases_from_themes(themes) -> list:
    """Metadata stores themes as a comma-joined string; the enriched cache
    as a list. Accept both, normalize the U+2011 hyphens, drop empties."""
    if isinstance(themes, str):
        parts = themes.split(",")
    else:
        parts = list(themes or [])
    out = []
    for p in parts:
        s = str(p).replace("‑", "-").strip()
        if len(s) >= 3:
            out.append(s)
    return out[:8]


async def write_facets(doc_id: str, title: str, domain: str,
                       genres_str: str, themes, vec_map: dict = None) -> int:
    """(Re)write one title's facet points. Full upsert: delete-by-parent
    then add — a failed embed leaves the title WITHOUT facets rather than
    with stale ones. Returns the number of facet points written.

    ``vec_map`` ({phrase: vector}) lets the backfill pre-embed a whole page
    in batched, deduped calls (themes repeat ~6.7x across the corpus; one
    embed call per title was the backfill's bottleneck). Phrases missing
    from the map are embedded here as stragglers."""
    if not doc_id:
        return 0
    phrases = _phrases_from_themes(themes)
    try:
        from src.services.embed_service import embed_documents, get_profile
        from src.vector_store.chromadb_wrapper import get_chroma_db
        chroma = get_chroma_db()
        # Delete-first even when the new theme set is empty: a re-enriched
        # title whose themes vanished must not keep stale facet points.
        chroma.facets_delete_by_parent(str(doc_id))
        if not phrases:
            return 0
        if vec_map is not None:
            lookup = dict(vec_map)
            missing = [p for p in phrases if not lookup.get(p)]
            if missing:
                mv = await embed_documents(missing)
                lookup.update({p: v for p, v in zip(missing, mv) if v})
            pairs = [(p, lookup[p]) for p in phrases if lookup.get(p)]
        else:
            vecs = await embed_documents(phrases)
            pairs = [(p, v) for p, v in zip(phrases, vecs) if v]
        if not pairs:
            return 0
        # emb_model stamp (external eval catch): without it a profile
        # rollback would probe v1 queries against v2 facet points with no
        # way to tell the spaces apart.
        emb_model = get_profile().get("model") or ""
        ok = chroma.facets_add(
            documents=[p for p, _ in pairs],
            embeddings=[v for _, v in pairs],
            metadatas=[{"parent": str(doc_id), "title": title or "",
                        "domain": domain or "", "genres": genres_str or "",
                        "facet": FACET_KIND, "emb_model": emb_model}
                       for _ in pairs],
            ids=[f"{doc_id}::f{i}" for i in range(len(pairs))])
        return len(pairs) if ok else 0
    except Exception as e:
        logger.debug("[facets] write for %r failed: %s", title, e)
        return 0


async def facet_probe(constraint_vecs: list, domain: str = None,
                      k: int = 20) -> dict:
    """One facets query per constraint vector → aggregation per PARENT:
    {parent_id: {"title", "genres", "sims": {c_idx: max_sim},
                 "phrases": {c_idx: best_phrase}, "hits": n_constraints}}.
    A parent whose DIFFERENT facets hit DIFFERENT constraints is exactly
    the contrast-resolution stage 1 exists for."""
    from src.vector_store.chromadb_wrapper import get_chroma_db
    chroma = get_chroma_db()
    out: dict = {}
    for ci, vec in enumerate(constraint_vecs):
        if not vec:
            continue
        res = chroma.facets_query(
            query_embeddings=[vec], n_results=k,
            where={"domain": domain} if domain else None)
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        for i, meta in enumerate(metas):
            parent = str(meta.get("parent") or "")
            if not parent:
                continue
            sim = 1.0 - dists[i] if i < len(dists) and \
                isinstance(dists[i], (int, float)) else 0.0
            rec = out.setdefault(parent, {
                "title": meta.get("title", ""),
                "genres": meta.get("genres", ""),
                "sims": {}, "phrases": {}})
            if sim > rec["sims"].get(ci, 0.0):
                rec["sims"][ci] = round(sim, 4)
                rec["phrases"][ci] = docs[i] if i < len(docs) else ""
    for rec in out.values():
        rec["hits"] = len(rec["sims"])
    return out


def backfill_done() -> bool:
    try:
        from src.services.app_state import get_state
        return get_state(_BACKFILL_DONE_KEY) == "1"
    except Exception:
        return False


async def run_facet_backfill(task=None) -> bool:
    """Custodian-driven, resumable backfill: page media_knowledge_v2,
    embed each title's themes (CPU — nomic runs num_gpu=0 everywhere),
    write facet points. Cursor in app_state; returns True when complete
    (the custodian stamps the task done), False to continue next tick.
    Runs IN-process — the chroma process lock forbids sidecar scripts.
    ``task`` is the custodian's Activity card (task_monitor Task) — page
    progress goes there so the run is visible outside the console."""
    from src.services.app_state import get_state, set_state
    from src.vector_store.chromadb_wrapper import get_chroma_db

    if backfill_done():
        return True
    chroma = get_chroma_db()
    try:
        offset = int(get_state(_BACKFILL_CURSOR_KEY) or 0)
    except Exception:
        offset = 0
    total = chroma.get_count()

    start_offset = offset

    def _report(written: int = None, within: int = 0):
        # Progress/rate/ETA are for THIS tick's slice — reporting the global
        # cursor as `processed` on a resumed run would inflate the rate with
        # titles done in earlier ticks and show a nonsense ETA. The global
        # position goes in the message instead. ``within`` = titles finished
        # inside the current page (a 500-title page embeds for minutes — one
        # update per page left the bar frozen between updates).
        if task is None:
            return
        try:
            from src.services.task_monitor import task_monitor
            slice_total = min(pages_per_run * _PAGE, max(total - start_offset, 0))
            pos = min(offset + within, total)
            msg = f"{pos:,}/{total:,} titles indexed overall"
            if written is not None:
                msg += f" (+{written} facet points)"
            task_monitor.update(task, processed=offset - start_offset + within,
                                total=slice_total, message=msg)
        except Exception:
            pass
    # Budget per custodian tick: one page batch keeps a tick short; the
    # 30-min custodian interval finishes ~27k titles in a few days, or
    # instantly via the manual maintenance button loop.
    pages_per_run = 6
    _report()
    for _ in range(pages_per_run):
        if offset >= total:
            set_state(_BACKFILL_DONE_KEY, "1")
            logger.info("[facets] backfill COMPLETE — %d facet points",
                        chroma.facets_count())
            _report()
            return True
        try:
            page = chroma.collection.get(
                include=["metadatas"], limit=_PAGE, offset=offset)
        except Exception as e:
            logger.warning("[facets] backfill page read failed at %d: %s",
                           offset, e)
            return False
        ids = page.get("ids") or []
        metas = page.get("metadatas") or []
        # Pre-embed the page's phrases: dedupe (themes repeat heavily across
        # titles) + chunked batch calls — turns ~500 per-title embed calls
        # per page into a handful, which is where the 39-min ticks went.
        page_phrases: list = []
        for meta in metas:
            page_phrases.extend(_phrases_from_themes((meta or {}).get("themes", "")))
        uniq = list(dict.fromkeys(page_phrases))
        vec_map: dict = {}
        from src.services.embed_service import embed_documents
        _CHUNK = 256
        for j in range(0, len(uniq), _CHUNK):
            chunk = uniq[j:j + _CHUNK]
            _report(within=0)   # keep the card's clock moving during embeds
            try:
                vecs = await embed_documents(chunk)
                vec_map.update({p: v for p, v in zip(chunk, vecs) if v})
            except Exception as e:
                logger.debug("[facets] page embed chunk failed: %s", e)
        written = 0
        for i, (doc_id, meta) in enumerate(zip(ids, metas)):
            meta = meta or {}
            written += await write_facets(
                str(doc_id), meta.get("title", ""),
                meta.get("domain", "") or meta.get("media_type", ""),
                meta.get("genres", ""), meta.get("themes", ""),
                vec_map=vec_map)
            if (i + 1) % 25 == 0:
                _report(written, within=i + 1)
        offset += len(ids) if ids else _PAGE
        set_state(_BACKFILL_CURSOR_KEY, str(offset))
        logger.info("[facets] backfill %d/%d titles (+%d points this page)",
                    min(offset, total), total, written)
        _report(written)
    return False
