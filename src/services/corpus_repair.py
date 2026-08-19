"""Curatarr — corpus repair: deterministic doc rebuild from cached raw data.

The generalized Batman-Beyond repair (2026-08-18): a chroma doc whose
enriched cache row expired can carry YEARS-old poisoned content (the
confabulated 'Devil of Darkness' profile) with no path to self-heal —
the profile audit only scanned cache rows, and gone-from-arr items never
re-enrich. Owner's directive: keep the knowledge, fix the ASSIGNMENT,
never delete.

Rebuild = title + genres + synopsis + keywords straight from the cached
raw_prefetch entry (TMDB-sourced, trustworthy), embedded and written over
the old doc, facets refreshed. NO LLM anywhere — no confabulation surface.
"""

import logging

logger = logging.getLogger(__name__)

# Same guard class as media_enricher's merge: an adult genre in a bare
# prefetch genre list (arr/TVDB-fed) is exactly how the poisoning started.
_ADULT_GENRES = {"hentai", "erotica"}


async def rebuild_doc_from_prefetch(doc_id: str, fallback_domain: str = "") -> bool:
    """Rebuild ONE parent doc from its raw_prefetch cache entry. Returns True
    when the doc (and its facets) were rewritten; False when no trustworthy
    prefetch data exists (the doc is left untouched — knowledge preserved)."""
    if not doc_id:
        return False
    try:
        from src.cache.metadata_cache import MetadataCache
        mc = MetadataCache()
        try:
            hit = mc.get_cache(f"raw_prefetch:{doc_id}")
        finally:
            mc.close()
        raw = (hit or {}).get("response")
        if not isinstance(raw, dict):
            return False
        title = (raw.get("title") or "").strip()
        overview = (raw.get("overview") or raw.get("synopsis") or "").strip()
        if not title or len(overview) < 40:
            return False   # nothing trustworthy to rebuild from
        year = raw.get("year") or 0
        keywords = [str(k) for k in (raw.get("keywords") or []) if k][:10]
        genres = [str(g) for g in (raw.get("genres") or [])
                  if str(g).lower() not in _ADULT_GENRES]

        from src.services.media_enricher import _domain_for_write
        domain = _domain_for_write(
            raw.get("media_type") or fallback_domain or "movie", genres)

        doc_text = (f"{title} ({year}) — {', '.join(genres)}. {overview} "
                    f"Keywords: {', '.join(keywords)}.")
        from src.services.embed_service import embed_documents
        vecs = await embed_documents([doc_text])
        if not vecs or not vecs[0]:
            return False

        from src.vector_store.chromadb_wrapper import get_chroma_db
        chroma = get_chroma_db()
        meta = {"title": f"{title} ({year})" if year else title,
                "media_type": domain, "domain": domain,
                "genres": ", ".join(genres),
                "themes": ", ".join(keywords[:6]),
                "mood": "", "year": year}
        chroma.collection.delete(ids=[str(doc_id)])
        chroma.collection.add(ids=[str(doc_id)], documents=[doc_text],
                              embeddings=[vecs[0]], metadatas=[meta])
        from src.services.facet_index import write_facets
        await write_facets(str(doc_id), meta["title"], domain,
                           meta["genres"], keywords[:6])
        logger.info("[repair] rebuilt %s from prefetch (%s, %d keywords)",
                    doc_id, domain, len(keywords))
        return True
    except Exception as e:
        logger.warning("[repair] rebuild failed for %s: %s", doc_id, e)
        return False
