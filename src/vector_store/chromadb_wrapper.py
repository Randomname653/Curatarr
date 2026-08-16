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

# ── Ops hardening (eval package 4) ────────────────────────────────────────────

_lock_handle = None            # exclusive per-process lock, held for lifetime
_quick_checked = False
_fallback_counts = {"query": 0, "get": 0}
_FALLBACK_WARN_EVERY = 25      # empty-result fallbacks can mask corruption


def _startup_quick_check() -> None:
    """PRAGMA quick_check on Chroma's backing SQLite, once per process.
    Corruption here used to degrade silently (every query fell into the
    empty-result fallback) — fail LOUDLY with the restore path instead."""
    global _quick_checked
    if _quick_checked:
        return
    _quick_checked = True
    try:
        import sqlite3
        from pathlib import Path
        db_file = Path(str(settings.CHROMADB_PATH)) / "chroma.sqlite3"
        if not db_file.exists():
            return   # fresh install — nothing to check
        con = sqlite3.connect(str(db_file), timeout=30)
        try:
            ok = (con.execute("PRAGMA quick_check(1)").fetchone() or ["?"])[0]
        finally:
            con.close()
        if ok != "ok":
            logger.critical(
                "ChromaDB backing store FAILED quick_check: %r. Restore "
                "path: stop the app, move data/chromadb aside, restart, "
                "then rebuild via POST /api/enrichment/embedding-migration "
                "(the corpus is reproducible from the enrichment caches).",
                ok)
            raise RuntimeError(f"ChromaDB store corrupt (quick_check: {ok})")
        logger.info("[chroma] quick_check ok (%s)", db_file.name)
    except RuntimeError:
        raise
    except Exception as e:
        logger.warning("[chroma] quick_check skipped: %s", e)


def _acquire_process_lock() -> None:
    """Exclusive lock on data/chromadb — TWO PersistentClients (running app
    + a standalone script) are exactly the pattern behind the existing
    data/_chromadb_corrupt_* backups. The OS releases the lock with the
    process, so a crash never leaves it stuck."""
    global _lock_handle
    if _lock_handle is not None:
        return
    try:
        from pathlib import Path
        lock_path = Path(str(settings.CHROMADB_PATH)) / ".curatarr.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+")
        try:
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except ImportError:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_handle = fh
    except OSError:
        logger.critical(
            "data/chromadb is already locked by ANOTHER process — refusing "
            "a second PersistentClient (this exact pattern corrupted the "
            "store before). Stop the other process first.")
        raise RuntimeError("ChromaDB is in use by another process")
    except Exception as e:
        logger.warning("[chroma] process lock unavailable: %s", e)


def _count_fallback(kind: str) -> None:
    """Empty-result fallbacks are the corruption mask — count and surface."""
    _fallback_counts[kind] = _fallback_counts.get(kind, 0) + 1
    n = _fallback_counts[kind]
    if n % _FALLBACK_WARN_EVERY == 0:
        logger.warning(
            "[chroma] %d %s calls have hit the empty-result fallback this "
            "process — if this keeps climbing, suspect store corruption "
            "(run PRAGMA quick_check / see the restore path).", n, kind)


class ChromaDBWrapper:
    """
    Wrapper for ChromaDB vector storage.
    
    Provides methods for:
    - Creating/destroying collections
    - Adding documents with embeddings
    - Querying by similarity
    - Managing metadata
    """
    
    def __init__(self, collection_name: str = None):
        """
        Initialize ChromaDB client and collection.

        Args:
            collection_name: Name of the collection to use. None → the
                active embedding profile's collection (media_knowledge
                until the migration flips it).
        """
        if collection_name is None:
            try:
                from src.services.embed_service import get_profile
                collection_name = get_profile().get("collection") or "media_knowledge"
            except Exception:
                collection_name = "media_knowledge"

        # Ops hardening (eval package 4): the known Chroma corruption
        # pattern here was a second process / a kill during an HNSW
        # checkpoint. (a) quick_check the backing SQLite at startup so a
        # corrupt store fails LOUDLY with a restore hint instead of
        # serving empty results for weeks; (b) hold an exclusive
        # process lock for data/chromadb — a second PersistentClient
        # (e.g. a standalone script beside the running app) is refused.
        _startup_quick_check()
        _acquire_process_lock()
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
        """Get or create collection.

        Space assertion (eval 1.12): every cosine assumption in the app
        (taste mismatch, calibration anchors, RAG ranking) silently breaks
        if a collection is ever recreated without ``hnsw:space=cosine`` —
        Chroma's default is L2, and only THIS create path sets cosine. A
        collection restored/recreated any other way must fail LOUDLY at
        startup instead of ranking by the wrong metric for weeks."""
        try:
            coll = self.client.get_collection(self.collection_name)
        except Exception:
            return self.client.create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"}
            )
        space = (coll.metadata or {}).get("hnsw:space")
        if space != "cosine":
            logger.critical(
                "ChromaDB collection %r has hnsw:space=%r (expected 'cosine') "
                "— it was recreated outside the wrapper (corruption restore?). "
                "Refusing to serve wrong-metric rankings.",
                self.collection_name, space,
            )
            raise RuntimeError(
                f"ChromaDB collection '{self.collection_name}' is not in "
                f"cosine space (hnsw:space={space!r})")
        return coll
    
    def reset(self):
        """**Destructive — wipes ALL ChromaDB collections, not just ours.**

        ``chromadb.PersistentClient(settings=Settings(allow_reset=True))``
        means this method is callable at all. It is intentionally NOT wired
        to any HTTP route — call sites must be admin-gated and explicit.
        """
        logger.warning("ChromaDB reset(): wiping all collections at %s", settings.CHROMADB_PATH)
        self.client.reset()
        self.collection = self._get_or_create_collection()
        self._facets = None

    # ── FACET COLLECTION (multi-vector items, stage 1) ───────────────────────
    # Facet points live in their OWN collection (media_facets_v1) so every
    # consumer of the one-point-per-title assumption in the main collection
    # (n_results slots, anchor resolution, embeddings_for_domain calibration
    # with its 30k cap, count_by_id_prefix coverage) stays byte-identical.
    # Same PersistentClient — the process lock forbids a second one.

    FACETS_COLLECTION = "media_facets_v1"
    _facets = None

    def _facets_collection(self):
        if self._facets is None:
            try:
                coll = self.client.get_collection(self.FACETS_COLLECTION)
                if (coll.metadata or {}).get("hnsw:space") != "cosine":
                    raise RuntimeError(
                        f"facet collection '{self.FACETS_COLLECTION}' is not "
                        "in cosine space")
            except RuntimeError:
                raise
            except Exception:
                coll = self.client.create_collection(
                    name=self.FACETS_COLLECTION,
                    metadata={"hnsw:space": "cosine"})
            self._facets = coll
        return self._facets

    def facets_add(self, documents, embeddings, metadatas, ids) -> bool:
        try:
            embeddings = np.array(embeddings, dtype=np.float32)
            self._facets_collection().add(
                documents=documents, embeddings=embeddings,
                metadatas=metadatas, ids=ids)
            return True
        except Exception as e:
            logger.debug("[chroma] facets_add failed: %s", e)
            return False

    def facets_query(self, query_embeddings, n_results: int = 20,
                     where=None) -> Dict:
        try:
            return self._facets_collection().query(
                query_embeddings=query_embeddings, n_results=n_results,
                where=where)
        except Exception as e:
            logger.debug("[chroma] facets_query failed: %s", e)
            _count_fallback("query")
            return {"ids": [[]], "distances": [[]], "documents": [[]],
                    "metadatas": [[]]}

    def facets_delete_by_parent(self, parent_id: str) -> bool:
        """Upsert half: drop every facet of one title before re-adding —
        never the metadata-only staleness the main writer's fallback has."""
        try:
            self._facets_collection().delete(where={"parent": str(parent_id)})
            return True
        except Exception as e:
            logger.debug("[chroma] facets_delete_by_parent(%s) failed: %s",
                         parent_id, e)
            return False

    def facets_count(self) -> int:
        try:
            return self._facets_collection().count()
        except Exception:
            return 0
    
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
        try:
            results = self.collection.query(
                query_embeddings=query_embeddings,
                n_results=n_results,
                where=where
            )
        except Exception as e:
            # Same resilience as get_by_id: a corrupt/inconsistent index segment
            # must not 500 callers (chat RAG, scoring). Return an empty result
            # in ChromaDB's nested shape so callers just see "no neighbours" —
            # but COUNT it: this fallback is exactly what masked corruption.
            logger.debug("[chroma] query failed: %s", e)
            _count_fallback("query")
            return {"ids": [[]], "distances": [[]], "documents": [[]],
                    "metadatas": [[]], "embeddings": [[]]}

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
        try:
            result = self.collection.get(
                ids=[doc_id],
                include=["embeddings", "documents", "metadatas"],
            )
        except Exception as e:
            # A corrupt/inconsistent HNSW index segment (or any backend
            # InternalError) must NOT crash the caller: deletion scoring loops
            # over thousands of ids, and one failed lookup shouldn't 500 the
            # whole analysis. Treat it as a missing embedding — callers already
            # fall back to a neutral taste-mismatch score for that item.
            # Counted: a climbing fallback rate is the corruption tell.
            logger.debug("[chroma] get_by_id(%s) failed: %s", doc_id, e)
            _count_fallback("get")
            return None
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
    
    def embeddings_for_domain(self, domain: str, limit: int = 30000) -> List:
        """All stored embeddings of one domain (movie/show/anime/music) —
        used by the taste rebuild to compute GLOBAL calibration anchors
        (centroid-vs-corpus cosine p10/p90) instead of per-batch anchors.
        Returns a list of raw vectors; caller normalizes."""
        try:
            res = self.collection.get(where={"domain": domain},
                                      include=["embeddings"], limit=limit)
            emb = res.get("embeddings")
            return list(emb) if emb is not None else []
        except Exception as e:
            logger.warning("embeddings_for_domain(%s) failed: %s", domain, e)
            return []

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
    
    def all_ids(self) -> List[str]:
        """Every document id, cheaply (include=[] skips embeddings/documents).
        Used by the KB overview to count vector coverage per ITEM identity —
        raw id counts over-report because one item may carry ids from several
        keying epochs."""
        try:
            return list((self.collection.get(include=[]) or {}).get("ids") or [])
        except Exception as e:
            logger.debug("all_ids() failed: %s", e)
            return []

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
def _cached_wrapper() -> ChromaDBWrapper:
    return ChromaDBWrapper()


def get_chroma_db() -> ChromaDBWrapper:
    """Return the process-wide ChromaDB wrapper, instantiated on first call.

    Profile-aware (eval package 2): the active embedding profile names the
    collection to serve; when the migration runner flips the profile, the
    cached wrapper is rebuilt so queries and writes move to the new
    collection together with the new model — never partially.
    """
    inst = _cached_wrapper()
    try:
        from src.services.embed_service import get_profile
        wanted = get_profile().get("collection") or "media_knowledge"
        if inst.collection_name != wanted:
            refresh_singleton()
            inst = _cached_wrapper()
    except Exception:
        pass
    return inst


def refresh_singleton() -> None:
    """Drop the cached wrapper (called by embed_service.set_profile)."""
    _cached_wrapper.cache_clear()


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
