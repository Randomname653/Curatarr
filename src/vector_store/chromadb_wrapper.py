"""
ARR Suite LLM - ChromaDB Vector Store Wrapper.

Handles vector storage and similarity search using ChromaDB.

Singleton access
----------------
``get_chroma_db()`` is the lazy factory — it instantiates the wrapper on
first call and memoizes via ``lru_cache``. Use it instead of importing the
``chroma_db`` module-level instance: the wrapper opens a PersistentClient
in its constructor (filesystem touch, may fail), so doing that at module
import time meant any failure took the whole app down at startup. With
the lazy factory, callers fail loudly when they actually try to use
ChromaDB rather than at import time.
"""

import logging
from functools import lru_cache
from typing import Dict, List, Optional

import chromadb
import numpy as np
from chromadb.config import Settings

from src.config import settings

logger = logging.getLogger(__name__)


class ChromaDBWrapper:
    """
    Wrapper for ChromaDB vector storage.
    
    Provides methods for:
    - Creating/destroying collections
    - Adding documents with embeddings
    - Querying by similarity
    - Managing metadata
    """
    
    def __init__(self, collection_name: str = "media_knowledge"):
        """
        Initialize ChromaDB client and collection.
        
        Args:
            collection_name: Name of the collection to use
        """
        # Pass 97: explicitly disable PostHog telemetry. ChromaDB ≥0.5
        # defaults to anonymized_telemetry=True and ships usage events
        # (``collection_query``, ``collection_add``, OS, version, an
        # anonymised install hash) to eu.posthog.com / app.posthog.com.
        # No content leaves, but it violates the README's "nothing about
        # your library leaves the machine" promise. One-flag fix.
        self.client = chromadb.PersistentClient(
            path=str(settings.CHROMADB_PATH),
            settings=Settings(
                allow_reset=True,
                anonymized_telemetry=False,
            ),
        )
        self.collection_name = collection_name
        self.collection = self._get_or_create_collection()
    
    def _get_or_create_collection(self):
        """Get or create collection."""
        try:
            return self.client.get_collection(self.collection_name)
        except Exception:
            return self.client.create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"}
            )
    
    def reset(self):
        """**Destructive — wipes ALL ChromaDB collections, not just ours.**

        ``chromadb.PersistentClient(settings=Settings(allow_reset=True))``
        means this method is callable at all. It is intentionally NOT wired
        to any HTTP route — call sites must be admin-gated and explicit.
        """
        logger.warning("ChromaDB reset(): wiping all collections at %s", settings.CHROMADB_PATH)
        self.client.reset()
        self.collection = self._get_or_create_collection()
    
    def add_documents(
        self,
        documents: List[str],
        embeddings: List[List[float]],
        metadatas: List[Dict],
        ids: List[str]
    ) -> bool:
        """
        Add documents to vector store.
        
        Args:
            documents: List of text documents
            embeddings: List of vector embeddings (float32)
            metadatas: List of metadata dicts
            ids: List of unique IDs
            
        Returns:
            True if successful
        """
        # Convert embeddings to float32 if needed
        if embeddings:
            embeddings = [np.array(e, dtype=np.float32).tolist() for e in embeddings]
        
        self.collection.add(
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
            ids=ids
        )
        
        return True
    
    def query(
        self,
        query_embeddings: List[List[float]],
        n_results: int = 10,
        where: Optional[Dict] = None
    ) -> Dict:
        """
        Query by similarity.
        
        Args:
            query_embeddings: List of query embeddings
            n_results: Number of results to return
            where: Filter metadata
            
        Returns:
            Results dict with documents, distances, and metadata
        """
        results = self.collection.query(
            query_embeddings=query_embeddings,
            n_results=n_results,
            where=where
        )
        
        return results
    
    def get_by_id(self, doc_id: str) -> Optional[Dict]:
        """Get document by ID — including its embedding.

        Pass 64: ``include=["embeddings", ...]`` is REQUIRED. ChromaDB's
        ``.get()`` omits embeddings by default (only ids/documents/
        metadatas come back), so the previous bare ``.get(ids=[doc_id])``
        always left ``result['embeddings']`` as None — and every caller
        that needed the vector (the deletion-scoring taste-mismatch
        comparison) silently got ``embedding=None`` and fell back to a
        flat 0.5 distance penalty. The doc was found, the embedding
        wasn't returned.
        """
        result = self.collection.get(
            ids=[doc_id],
            include=["embeddings", "documents", "metadatas"],
        )
        if result and result['ids']:
            embeddings = result.get('embeddings')
            documents = result.get('documents')
            metadatas = result.get('metadatas')
            return {
                'id': result['ids'][0],
                'document': documents[0] if documents else None,
                'metadata': metadatas[0] if metadatas else None,
                'embedding': embeddings[0] if embeddings is not None and len(embeddings) else None,
            }
        return None
    
    def delete_by_id(self, doc_id: str) -> bool:
        """Delete document by ID."""
        self.collection.delete(ids=[doc_id])
        return True
    
    def update_metadata(
        self,
        doc_id: str,
        metadata: Dict
    ) -> bool:
        """Update metadata for existing document."""
        self.collection.update(
            ids=[doc_id],
            metadatas=[metadata]
        )
        return True
    
    def get_count(self) -> int:
        """Get number of documents in collection."""
        return self.collection.count()

    def count_by_id_prefix(self, prefix: str) -> int:
        """Pass 65: count documents whose id starts with ``prefix``.

        Used by the ARR status panel to report real per-service embedding
        coverage (doc ids are written as "{service}:{arr_id}" by the
        enrichment pipeline). ``include=[]`` fetches ONLY the id list — no
        embeddings/documents/metadatas — so this stays cheap even on a
        large collection.
        """
        try:
            result = self.collection.get(include=[])
            ids = result.get("ids") or []
            return sum(1 for i in ids if str(i).startswith(prefix))
        except Exception as e:
            logger.debug("count_by_id_prefix(%r) failed: %s", prefix, e)
            return 0
    
    def get_all(self, limit: int = 1000) -> List[Dict]:
        """Get all documents (for batch processing)."""
        result = self.collection.get(limit=limit)
        
        documents = []
        for i in range(len(result['ids'])):
            doc = {
                'id': result['ids'][i],
                'document': result['documents'][i],
                'metadata': result['metadatas'][i],
                'embedding': result['embeddings'][i] if result['embeddings'] else None
            }
            documents.append(doc)
        
        return documents
    
    # Pass 48: removed ``create_index`` — ChromaDB handles HNSW per-
    # collection automatically and there were no callers in src/. The
    # method existed as a no-op compatibility shim with a long-dead
    # callsite.


# ── Lazy singleton accessor ───────────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_chroma_db() -> ChromaDBWrapper:
    """Return the process-wide ChromaDB wrapper, instantiated on first call.

    Memoized via ``functools.lru_cache(maxsize=1)``. Replaces the previous
    module-level ``chroma_db = ChromaDBWrapper()`` which opened a
    PersistentClient at import time and could crash the whole app before
    it ever started.
    """
    return ChromaDBWrapper()


def __getattr__(name):
    """PEP 562 module-level lazy attribute.

    Existing call sites do ``from src.vector_store.chromadb_wrapper import chroma_db``
    — this keeps that pattern working without changing every importer, while
    still deferring construction to first access (i.e., when the import line
    runs in a caller, not when this module is itself imported).
    """
    if name == "chroma_db":
        return get_chroma_db()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
