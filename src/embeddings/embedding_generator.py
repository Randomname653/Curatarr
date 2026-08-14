"""
ARR Suite LLM - Embedding Generator (legacy facade)

Eval package 2: the actual embedding logic (model, endpoint, prefixes,
batching, normalization, migration profile) lives in
src/services/embed_service.py. This class remains as a thin
DOCUMENT-side facade for its existing callers (media_enricher's index
writers); query-side callers use embed_service.embed_query directly.
"""

import logging
from typing import List

logger = logging.getLogger(__name__)


class EmbeddingGenerator:
    """Thin facade over embed_service.embed_documents (document side)."""

    async def generate_embedding(self, text: str) -> List[float]:
        """Embed one document-side text. Returns [] on failure (historic
        contract of this class)."""
        from src.services.embed_service import embed_documents
        vecs = await embed_documents([text])
        return vecs[0] if vecs and vecs[0] else []

    async def close(self):
        """Kept for call-site compatibility — embed_service uses per-call
        clients, so there is nothing to close anymore."""
        return None
