"""Curatarr — semantic library search core.

Extracted from chat's ``_get_rag_context`` so the SAME retrieval serves
both the hidden chat-RAG injection and the user-facing
``GET /api/library/semantic-search`` endpoint. The chat path stays
byte-identical via ``format_rag_context()``.
"""

import logging

from src.services.watch_status import watched_lookup, watch_tag

logger = logging.getLogger(__name__)


async def semantic_hits(query: str, n_results: int = 5, domain: str = None,
                        user_id: int = None) -> list:
    """Ranked semantic hits over the ChromaDB vector store.

    When *domain* is given, only vectors tagged with that domain are
    considered — eliminating cross-media-type contamination. Each hit
    carries the watched-status tag (per *user_id*) and the size tag the
    chat context has always shown.
    """
    try:
        from src.vector_store.chromadb_wrapper import ChromaDBWrapper
        from src.embeddings.embedding_generator import EmbeddingGenerator
        gen = EmbeddingGenerator()
        embedding = await gen.generate_embedding(query)
        if not embedding:
            return []
        chroma = ChromaDBWrapper()
        where = {"domain": domain} if domain else None
        results = chroma.query(query_embeddings=[embedding],
                               n_results=n_results, where=where)
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        await gen.close()
        from src.services.size_norms import short_size_tag
        watched = watched_lookup(user_id, [m.get("title", "") for m in metas],
                                 category=domain)
        hits = []
        for doc, meta in zip(docs, metas):
            title = meta.get("title", "Unknown")
            hits.append({
                "title": title,
                "genres": meta.get("genres", ""),
                "themes": meta.get("themes", ""),
                "doc": doc or "",
                "watch_tag": watch_tag(watched.get(title)),
                "size_tag": short_size_tag(tmdb_id=meta.get("tmdb_id"),
                                           tvdb_id=meta.get("tvdb_id"),
                                           plex_rating_key=meta.get("plex_rating_key"),
                                           title=title),
                "tmdb_id": meta.get("tmdb_id"),
                "tvdb_id": meta.get("tvdb_id"),
                "plex_rating_key": meta.get("plex_rating_key"),
            })
        return hits
    except Exception as e:
        logger.debug("semantic search failed: %s", e)
        return []


def format_rag_context(hits: list) -> str:
    """The exact line format the chat prompt has always injected."""
    lines = []
    for h in hits:
        stag = h.get("size_tag") or ""
        themes = h.get("themes") or ""
        lines.append(f"- {h['title']} [{h['watch_tag']}]{(' ' + stag) if stag else ''} "
                     f"({h.get('genres', '')}{', '+themes if themes else ''}): "
                     f"{(h.get('doc') or '')[:200]}")
    return "\n".join(lines)
