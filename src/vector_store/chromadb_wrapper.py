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

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache, partial
from typing import Any, Callable, Dict, List, Optional, TypeVar

import chromadb
import numpy as np
from chromadb.config import Settings

from src.config import settings
from src.database.loop_guard import sqlite_connect

logger = logging.getLogger(__name__)


def domain_where(domain):
    """Chroma where-clause for a domain filter — THE one place that knows
    about the legacy 'tv' epoch. The old Plex sync wrote series docs with
    domain='tv' (995 docs, including everything the household actually
    watched); the arr era writes 'show' (289). Filtering on 'show' alone
    silently dropped the watched half from taste calibration, search and
    facet probes (external eval catch). None stays None (no filter)."""
    if not domain:
        return None
    if domain == "show":
        return {"domain": {"$in": ["show", "tv"]}}
    return {"domain": domain}


# ── Ops hardening (eval package 4) ────────────────────────────────────────────

_lock_handle = None            # exclusive per-process lock, held for lifetime
_quick_checked = False
_fallback_counts = {"query": 0, "get": 0}
_FALLBACK_WARN_EVERY = 25      # empty-result fallbacks can mask corruption

# embeddings_for_domain memo: {(collection, domain, limit): (write_gen, at,
# float32 matrix)}. The taste rebuild pulls the same corpus up to twice per
# (user, category) — cluster gate + calibration — and the corpus is the same
# for every household member: 2 x users x 4 categories pulls of up to 30k
# vectors, each seconds of Chroma decode. Every write through this wrapper
# bumps _domain_write_gen, so a memo never outlives a change; the TTL bounds
# how long the matrix (~90 MB at 30k x 768 float32) stays resident.
_domain_emb_memo: dict = {}
_domain_emb_lock = threading.Lock()
_domain_write_gen = [0]
_DOMAIN_EMB_TTL = 600.0


def _bump_domain_gen() -> None:
    with _domain_emb_lock:
        _domain_write_gen[0] += 1
        _domain_emb_memo.clear()


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
        con = sqlite_connect(str(db_file), timeout=30)
        try:
            ok = (con.execute("PRAGMA quick_check(1)").fetchone() or ["?"])[0]
        finally:
            con.close()
        if ok != "ok":
            logger.critical(
                "ChromaDB backing store FAILED quick_check: %r. Restore "
                "path: stop the app, move data/chromadb aside, restart: "
                "the 'vector_reconcile' maintenance task re-embeds from the "
                "enrichment caches on its own (scripts/revectorize_backfill.py "
                "drains it in one go, app stopped). POST /api/enrichment/embedding-migration "
                "is NOT a restore path — it copies from the v1 collection, "
                "which no longer exists.",
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


# Caller errors — a query vector of the wrong dimension (old-model vectors
# after a profile flip), a malformed `where` — are bugs, not corruption. They
# used to log at DEBUG and read as "no neighbours". Still answered empty (a
# caller must not 500), but WARNED: on the first hit per call site, then every
# _FALLBACK_WARN_EVERY-th, so a per-item loop cannot flood the log.
try:
    from chromadb.errors import InvalidArgumentError as _ChromaInvalidArg
    from chromadb.errors import InvalidDimensionException as _ChromaInvalidDim
    _CALLER_ERRORS: tuple = (ValueError, TypeError, _ChromaInvalidArg, _ChromaInvalidDim)
except ImportError:   # pragma: no cover — older chromadb without these names
    _CALLER_ERRORS = (ValueError, TypeError)
_caller_error_counts: Dict[str, int] = {}


def _log_read_failure(site: str, e: Exception, detail: str = "") -> None:
    """DEBUG for backend hiccups (counted by _count_fallback); WARNING
    (rate-limited per site) for invalid-argument / dimension-mismatch."""
    if isinstance(e, _CALLER_ERRORS):
        n = _caller_error_counts[site] = _caller_error_counts.get(site, 0) + 1
        if n == 1 or n % _FALLBACK_WARN_EVERY == 0:
            logger.warning(
                "[chroma] %s(%s) rejected its arguments (%d this process): %s: %s"
                " — answered empty; check the embedding dimension / where filter",
                site, detail, n, type(e).__name__, e)
            return
    logger.debug("[chroma] %s(%s) failed: %s", site, detail, e)


def _count_fallback(kind: str) -> None:
    """Empty-result fallbacks are the corruption mask — count and surface."""
    _fallback_counts[kind] = _fallback_counts.get(kind, 0) + 1
    n = _fallback_counts[kind]
    if n % _FALLBACK_WARN_EVERY == 0:
        logger.warning(
            "[chroma] %d %s calls have hit the empty-result fallback this "
            "process — if this keeps climbing, suspect store corruption "
            "(run PRAGMA quick_check / see the restore path).", n, kind)



# Metadata key on a doc that was MERGED into its live twin: the same title
# (same tmdb/tvdb/MusicBrainz id) re-added to an arr under a new id gets a
# second doc. The old one is kept — the knowledge is never deleted — but it
# is hidden from similarity search so the title is not returned twice.
MERGED_INTO = "merged_into"
# Extra neighbours fetched per query so hiding merged docs never shortens
# the page a caller asked for.
_MERGED_HEADROOM = 8


def drop_merged(results: Dict, n_results: int) -> Dict:
    """``results`` (Chroma's nested query shape) without merged docs, each
    row cut back to ``n_results``."""
    if not isinstance(results, dict):
        return results
    ids = results.get("ids") or []
    metas = results.get("metadatas")
    if not metas:
        return results
    def _rows(key, val) -> bool:
        # Per-query lists (ids, distances, metadatas, embeddings — the latter
        # may be numpy rows); "included" and other scalars are left alone.
        if key == "ids" or val is None or isinstance(val, (str, bytes, dict)):
            return key == "ids"
        try:
            return len(val) == len(ids)
        except TypeError:
            return False

    out = dict(results)
    row_keys = [key for key, val in results.items() if _rows(key, val)]
    for key in row_keys:
        out[key] = list(results[key])
    for i, row_ids in enumerate(ids):
        row_meta = metas[i] if i < len(metas) and metas[i] is not None else []
        keep = [j for j in range(len(row_ids))
                if not ((row_meta[j] if j < len(row_meta) else None) or {}).get(MERGED_INTO)]
        keep = keep[:n_results]
        for key in row_keys:
            row = results[key][i]
            try:
                if row is None or len(row) != len(row_ids):
                    continue
            except TypeError:
                continue
            out[key][i] = [row[j] for j in keep]
    return out

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
                active embedding profile's collection. Fallback is the V2
                collection: v1 was dropped 2026-08-18 (ballast cleanup),
                so a v1 fallback would silently CREATE an empty legacy
                collection and serve zero results.
        """
        if collection_name is None:
            try:
                from src.services.embed_service import get_profile
                collection_name = get_profile().get("collection") or "media_knowledge_v2"
            except Exception as e:
                logger.warning("[chroma] embedding profile unreadable - serving "
                               "media_knowledge_v2: %s", e)
                collection_name = "media_knowledge_v2"

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
        self.client = self._open_client()
        self.collection_name = collection_name
        self.collection = self._get_or_create_collection()

    @staticmethod
    def _open_client():
        return chromadb.PersistentClient(
            path=str(settings.CHROMADB_PATH),
            settings=Settings(
                allow_reset=True,
                anonymized_telemetry=False,
            ),
        )

    def release_indexes(self) -> bool:
        """Free the HNSW indexes Chroma holds in RAM by opening a fresh
        client. The Rust core keeps every index it has loaded for the life
        of the client (its cache counts indexes, not bytes), and a deleted
        collection's index stays resident too. Measured: dropping the client
        returned 820 MB at once; the indexes reload on the next query."""
        try:
            import gc
            from chromadb.api.client import SharedSystemClient
            old = self.client
            SharedSystemClient.clear_system_cache()   # else the same System comes back
            self.client = self._open_client()
            self.collection = self._get_or_create_collection()
            self._phrases = None
            del old
            gc.collect()
            return True
        except Exception as e:
            logger.warning("[chroma] release_indexes failed: %s", e)
            return False

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
        self._phrases = None
        with self._fmap_lock:                 # the phrase map goes with the store
            con = self._facet_map()
            for t in ("facet_map", "facet_meta", "facet_migrated"):
                con.execute(f"DELETE FROM {t}")  # nosec B608 - the three table names above
            con.commit()

    # ── FACET INDEX: one point per phrase (multi-vector items, stage 1) ──────
    # A title's ~6 theme phrases are its facets. They live OUTSIDE the main
    # collection so every consumer of the one-point-per-title assumption
    # there (n_results slots, anchor resolution, the 30k calibration cap,
    # coverage by id prefix) stays byte-identical. Same PersistentClient -
    # the process lock forbids a second one.
    #
    # One point per distinct PHRASE, not per (title, phrase): themes repeat
    # (199k facet points held 80k phrases on the owner's store, 2026-10-25),
    # and the copies crowded every query - the 20 nearest points were often
    # the same three phrases, so a probe found few titles. Measured on a copy
    # against an exact search: title recall@20 0.52 with a point per title
    # and phrase, 0.81 with a point per phrase, and 262 MB of index instead
    # of 651 MB. Which titles carry a phrase is a small SQLite table beside
    # the store (facet_map.sqlite3); the methods below keep the shapes the
    # per-title collection had, so the probe, the backfill and the
    # nanogenres read it as before. A phrase point is never deleted while a
    # title carries it and an upsert keeps its slot, so the index does not
    # grow dead slots the way the per-title one did.

    PHRASES_COLLECTION = "media_phrases_v1"
    LEGACY_FACETS = "media_facets_v1"
    FACET_MAP_FILE = "facet_map.sqlite3"
    _phrases = None
    _fmap = None
    _fmap_lock = threading.Lock()

    @staticmethod
    def phrase_id(phrase: str) -> str:
        import hashlib
        return "ph:" + hashlib.sha1((phrase or "").strip().encode("utf-8"),
                                    usedforsecurity=False).hexdigest()[:24]

    def _phrases_collection(self):
        if self._phrases is None:
            try:
                coll = self.client.get_collection(self.PHRASES_COLLECTION)
                if (coll.metadata or {}).get("hnsw:space") != "cosine":
                    raise RuntimeError(
                        f"phrase collection '{self.PHRASES_COLLECTION}' is not in cosine space")
            except RuntimeError:
                raise
            except Exception:
                coll = self.client.create_collection(
                    name=self.PHRASES_COLLECTION, metadata={"hnsw:space": "cosine"})
            self._phrases = coll
        return self._phrases

    def _facet_map(self):
        """The (title -> phrase) table, opened once; every call takes the lock."""
        if self._fmap is None:
            from pathlib import Path
            base = Path(str(settings.CHROMADB_PATH))
            base.mkdir(parents=True, exist_ok=True)
            con = sqlite_connect(str(base / self.FACET_MAP_FILE), timeout=30,
                                 check_same_thread=False)
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS facet_map (
                    parent TEXT NOT NULL, idx INTEGER NOT NULL, phrase_id TEXT NOT NULL,
                    phrase TEXT NOT NULL, title TEXT, domain TEXT, genres TEXT,
                    facet TEXT, emb_model TEXT, PRIMARY KEY (parent, idx));
                CREATE INDEX IF NOT EXISTS ix_facet_map_phrase ON facet_map(phrase_id);
                CREATE TABLE IF NOT EXISTS facet_meta (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS facet_migrated (parent TEXT PRIMARY KEY);
            """)
            con.commit()
            self._fmap = con
        return self._fmap

    @staticmethod
    def _parse_point_id(pid: str):
        parent, sep, idx = str(pid).rpartition("::f")
        return (parent, int(idx)) if sep and idx.isdigit() else (str(pid), 0)

    def _map_rows(self, sql: str, args=()) -> list:
        with self._fmap_lock:
            return self._facet_map().execute(sql, args).fetchall()

    def _put_phrases(self, documents, embeddings, metadatas) -> None:
        """Upsert the phrase points that are new, or whose vector came from
        another embedding model. Raises on a vector Chroma refuses."""
        emb = np.array(embeddings, dtype=np.float32)
        coll = self._phrases_collection()
        pids = [self.phrase_id(d) for d in documents]
        have = {}
        uniq = list(dict.fromkeys(pids))
        for i in range(0, len(uniq), 1000):
            got = coll.get(ids=uniq[i:i + 1000], include=["metadatas"])
            have.update({h: (m or {}).get("emb_model") for h, m in
                         zip(got.get("ids") or [], got.get("metadatas") or [])})
        put = {}
        for k, (pid, md) in enumerate(zip(pids, metadatas)):
            model = (md or {}).get("emb_model", "")
            if pid not in put and (pid not in have or (model and have[pid] != model)):
                put[pid] = k
        if put:
            ks = list(put.values())
            coll.upsert(ids=list(put), embeddings=emb[ks],
                        documents=[documents[k].strip() for k in ks],
                        metadatas=[{"emb_model": (metadatas[k] or {}).get("emb_model", ""),
                                    "facet": (metadatas[k] or {}).get("facet", "theme")}
                                   for k in ks])

    def _map_insert(self, con, documents, metadatas, ids) -> None:
        con.executemany(
            "INSERT OR REPLACE INTO facet_map (parent, idx, phrase_id, phrase, title, domain,"
            " genres, facet, emb_model) VALUES (?,?,?,?,?,?,?,?,?)",
            [(*self._parse_point_id(i), self.phrase_id(d), d.strip(), (m or {}).get("title"),
              (m or {}).get("domain"), (m or {}).get("genres"), (m or {}).get("facet"),
              (m or {}).get("emb_model")) for d, m, i in zip(documents, metadatas, ids)])

    def facets_replace(self, parent_id: str, documents, embeddings, metadatas,
                       ids) -> bool:
        """Replace one title's facets: its phrases (only the new ones are
        stored) and its rows in the map. A failed write leaves the title's
        old rows in place."""
        try:
            self._put_phrases(documents, embeddings, metadatas)
            with self._fmap_lock:
                con = self._facet_map()
                con.execute("DELETE FROM facet_map WHERE parent = ?", (str(parent_id),))
                con.execute("DELETE FROM facet_migrated WHERE parent = ?", (str(parent_id),))
                self._map_insert(con, documents, metadatas, ids)
                con.commit()
            return True
        except Exception as e:
            logger.warning("[chroma] facets_replace(%s) failed: %s", parent_id, e)
            return False

    def facets_add(self, documents, embeddings, metadatas, ids) -> bool:
        """Add facets beside a title's existing ones (facets_replace is the
        writer the app uses)."""
        try:
            self._put_phrases(documents, embeddings, metadatas)
            with self._fmap_lock:
                con = self._facet_map()
                self._map_insert(con, documents, metadatas, ids)
                con.commit()
            return True
        except Exception as e:
            logger.warning("[chroma] facets_add failed: %s", e)
            return False

    def facets_delete_by_parent(self, parent_id: str) -> bool:
        """Drop every facet of one title. Its phrase points stay for the other
        titles that carry them (facets_sweep removes the orphans)."""
        try:
            with self._fmap_lock:
                con = self._facet_map()
                con.execute("DELETE FROM facet_map WHERE parent = ?", (str(parent_id),))
                con.execute("DELETE FROM facet_migrated WHERE parent = ?", (str(parent_id),))
                con.commit()
            return True
        except Exception as e:
            logger.warning("[chroma] facets_delete_by_parent(%s) failed: %s", parent_id, e)
            return False

    def facets_count(self) -> int:
        """Facet rows (title x phrase), as the per-title index counted points."""
        try:
            return int(self._map_rows("SELECT count(*) FROM facet_map")[0][0])
        except Exception as e:
            logger.warning("[chroma] facets_count failed: %s", e)
            return 0

    @staticmethod
    def _domains(where):
        """The domains a domain_where clause allows, or None for any."""
        d = (where or {}).get("domain") if isinstance(where, dict) else None
        if d is None:
            return None
        if isinstance(d, dict):
            return list(d.get("$in") or [])
        return [d]

    @staticmethod
    def _row_meta(r) -> dict:
        return {"parent": r[0], "title": r[4] or "", "domain": r[5] or "",
                "genres": r[6] or "", "facet": r[7] or "theme", "emb_model": r[8] or ""}

    _MAP_COLS = "parent, idx, phrase_id, phrase, title, domain, genres, facet, emb_model"

    def facets_query(self, query_embeddings, n_results: int = 20,
                     where=None) -> Dict:
        """The nearest phrases, each standing for every title that carries it
        (in the domain asked for), in the per-title shape: one entry per
        (title, phrase), nearest first, until ``n_results`` titles. A domain
        filter can leave the nearest phrases short of titles; the search
        then widens (x4, up to 4,000 phrases) until enough are found."""
        empty = {"ids": [[]], "distances": [[]], "documents": [[]], "metadatas": [[]]}
        try:
            coll = self._phrases_collection()
            total = coll.count()
            if not total:
                return {k: [[] for _ in query_embeddings] for k in empty}
            domains = self._domains(where)
            out = {k: [] for k in empty}
            for q in query_embeddings:
                width = max(60, n_results * 3)
                while True:
                    res = coll.query(query_embeddings=[q], n_results=min(total, width),
                                     include=["distances"])
                    got = self._expand(res["ids"][0], res["distances"][0], domains, n_results)
                    if len(set(m["parent"] for m in got[2])) >= n_results                             or width >= min(total, 4000):
                        break
                    width *= 4
                for k, v in zip(("ids", "documents", "metadatas", "distances"), got):
                    out[k].append(v)
            return out
        except Exception as e:
            _log_read_failure("facets_query", e)
            _count_fallback("query")
            return empty

    def _expand(self, pids, dists, domains, n_results: int) -> tuple:
        """(ids, documents, metadatas, distances) of the titles behind these
        phrases, nearest phrase first, until ``n_results`` titles."""
        rows = {}
        for i in range(0, len(pids), 500):
            chunk = pids[i:i + 500]
            sql = (f"SELECT {self._MAP_COLS} FROM facet_map WHERE phrase_id IN "
                   f"({','.join('?' * len(chunk))})")
            args = list(chunk)
            if domains:
                sql += f" AND domain IN ({','.join('?' * len(domains))})"
                args += domains
            for r in self._map_rows(sql + " ORDER BY parent, idx", args):  # nosec B608 - placeholders only
                rows.setdefault(r[2], []).append(r)
        ids_, docs, metas, ds, seen = [], [], [], [], set()
        for pid, dist in zip(pids, dists):
            for r in rows.get(pid, []):
                if len(seen) >= n_results and r[0] not in seen:
                    continue
                seen.add(r[0])
                ids_.append(f"{r[0]}::f{r[1]}")
                docs.append(r[3])
                metas.append(self._row_meta(r))
                ds.append(dist)
            if len(seen) >= n_results:
                break
        return ids_, docs, metas, ds

    def _with_vectors(self, rows) -> Dict:
        vec = {}
        pids = list(dict.fromkeys(r[2] for r in rows))
        coll = self._phrases_collection()
        for i in range(0, len(pids), 1000):
            got = coll.get(ids=pids[i:i + 1000], include=["embeddings"])
            for h, e in zip(got.get("ids") or [], got.get("embeddings")
                            if got.get("embeddings") is not None else []):
                vec[h] = e
        keep = [r for r in rows if r[2] in vec]
        return {"ids": [f"{r[0]}::f{r[1]}" for r in keep],
                "documents": [r[3] for r in keep],
                "metadatas": [self._row_meta(r) for r in keep],
                "embeddings": [vec[r[2]] for r in keep]}

    def facets_page(self, offset: int, limit: int, embeddings: bool = True) -> Dict:
        """One page of the facets (title x phrase), ordered by title, for a
        whole-index walk (services/nanogenres)."""
        rows = self._map_rows(f"SELECT {self._MAP_COLS} FROM facet_map ORDER BY parent, idx "  # nosec B608 - column list is a constant
                              "LIMIT ? OFFSET ?", (int(limit), int(offset)))
        if embeddings:
            return self._with_vectors(rows)
        return {"ids": [f"{r[0]}::f{r[1]}" for r in rows], "documents": [r[3] for r in rows],
                "metadatas": [self._row_meta(r) for r in rows]}

    def facets_by_ids(self, ids: List[str]) -> Dict:
        """These facets (``parent::fN``) with their vectors."""
        want = {self._parse_point_id(i) for i in ids}
        parents = sorted({p for p, _ in want})
        rows = []
        for i in range(0, len(parents), 500):
            chunk = parents[i:i + 500]
            rows += [r for r in self._map_rows(
                f"SELECT {self._MAP_COLS} FROM facet_map WHERE parent IN "  # nosec B608 - placeholders only
                f"({','.join('?' * len(chunk))}) ORDER BY parent, idx", chunk) if (r[0], r[1]) in want]
        return self._with_vectors(rows)

    def facets_phrase_vectors(self, phrases, emb_model: str = "") -> Dict:
        """{phrase: vector} for the phrases already stored (in ``emb_model``
        when given): a rewrite embeds only the phrases that are new."""
        uniq = list(dict.fromkeys((p or "").strip() for p in phrases if (p or "").strip()))
        if not uniq:
            return {}
        by_id = {self.phrase_id(p): p for p in uniq}
        out = {}
        try:
            coll = self._phrases_collection()
            ids = list(by_id)
            for i in range(0, len(ids), 1000):
                got = coll.get(ids=ids[i:i + 1000], include=["embeddings", "metadatas"])
                embs = got.get("embeddings") if got.get("embeddings") is not None else []
                for h, e, m in zip(got.get("ids") or [], embs, got.get("metadatas") or []):
                    if not emb_model or (m or {}).get("emb_model") == emb_model:
                        out[by_id[h]] = list(np.asarray(e, dtype=np.float32))
        except Exception as e:  # noqa: BLE001 - a miss only means the phrases are embedded again
            logger.debug("[chroma] facets_phrase_vectors failed: %s", e)
        return out

    def facets_sweep(self) -> int:
        """Drop the phrase points no title carries any more. Returns how many."""
        coll = self._phrases_collection()
        have = set(coll.get(include=[]).get("ids") or [])
        used = {r[0] for r in self._map_rows("SELECT DISTINCT phrase_id FROM facet_map")}
        orphans = sorted(have - used)
        for i in range(0, len(orphans), 1000):
            coll.delete(ids=orphans[i:i + 1000])
        return len(orphans)

    def drop_orphan_segment_dirs(self) -> int:
        """Remove the index folders no collection uses any more; bytes freed.
        Chroma leaves a deleted collection's HNSW folder on disk (the old
        facet index: 1.1 GB on the owner's store). Only folders named like a
        segment id and absent from the segments table go, and only after a
        fresh client (release_indexes) has let go of their files; a folder
        still held is left for the next run."""
        import re
        import shutil
        from pathlib import Path
        base = Path(str(settings.CHROMADB_PATH))
        con = sqlite_connect(f"file:{(base / 'chroma.sqlite3').as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            used = {r[0] for r in con.execute("SELECT id FROM segments")}
        finally:
            con.close()
        uuid = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        freed = 0
        for d in base.iterdir():
            if d.is_dir() and uuid.match(d.name) and d.name not in used:
                size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                try:
                    shutil.rmtree(d)
                    freed += size
                except OSError as e:
                    logger.info("[chroma] unused index folder %s still held: %s", d.name, e)
        return freed

    # ── the per-title collection, moved over once ──────────────────────────
    def facets_legacy_count(self):
        """Points left in the old per-title collection, None when it is gone."""
        try:
            return self.client.get_collection(self.LEGACY_FACETS).count()
        except Exception:  # noqa: BLE001 - "does not exist" is the normal answer
            return None

    def facets_migrate_step(self, page: int = 2000) -> Dict:
        """Move the next page of the old per-title collection over, reusing
        its vectors (nothing is embedded again). A title written since the
        move began keeps its new rows; a title split across pages is
        completed. {"done", "moved", "offset"}; at the end the old
        collection is dropped."""
        legacy = self.client.get_collection(self.LEGACY_FACETS)
        with self._fmap_lock:
            row = self._facet_map().execute(
                "SELECT value FROM facet_meta WHERE key = 'migrate_offset'").fetchone()
        offset = int(row[0]) if row else 0
        res = legacy.get(limit=page, offset=offset, include=["embeddings", "documents", "metadatas"])
        ids = list(res.get("ids") or [])
        moved = 0
        if ids:
            docs, metas = res.get("documents") or [], res.get("metadatas") or []
            embs = res.get("embeddings")
            parents = sorted({(m or {}).get("parent") or self._parse_point_id(i)[0]
                              for i, m in zip(ids, metas)})
            with self._fmap_lock:
                con = self._facet_map()
                written, migrated = set(), set()
                for i in range(0, len(parents), 500):
                    chunk = parents[i:i + 500]
                    q = ','.join('?' * len(chunk))
                    written |= {r[0] for r in con.execute(  # nosec B608 - placeholders only
                        f"SELECT DISTINCT parent FROM facet_map WHERE parent IN ({q})", chunk)}
                    migrated |= {r[0] for r in con.execute(  # nosec B608 - placeholders only
                        f"SELECT parent FROM facet_migrated WHERE parent IN ({q})", chunk)}
            keep = [k for k, (i, m) in enumerate(zip(ids, metas))
                    if ((m or {}).get("parent") or self._parse_point_id(i)[0]) not in written - migrated]
            if keep:
                kd = [docs[k] for k in keep]
                km = [dict(metas[k] or {}) for k in keep]
                for k, m in zip(keep, km):
                    m.setdefault("parent", self._parse_point_id(ids[k])[0])
                self._put_phrases(kd, np.asarray(embs, dtype=np.float32)[keep], km)
                with self._fmap_lock:
                    con = self._facet_map()
                    self._map_insert(con, kd, km, [ids[k] for k in keep])
                    con.executemany("INSERT OR IGNORE INTO facet_migrated (parent) VALUES (?)",
                                    [(m["parent"],) for m in km])
                    con.commit()
                moved = len(keep)
        offset += len(ids)
        done = len(ids) < page
        with self._fmap_lock:
            con = self._facet_map()
            if done:
                con.execute("DELETE FROM facet_meta WHERE key = 'migrate_offset'")
                con.execute("DELETE FROM facet_migrated")
            else:
                con.execute("INSERT OR REPLACE INTO facet_meta (key, value) VALUES ('migrate_offset', ?)",
                            (str(offset),))
            con.commit()
        if done:
            self.client.delete_collection(self.LEGACY_FACETS)
        return {"done": done, "moved": moved, "offset": offset}

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
        
        _bump_domain_gen()
        self.collection.add(
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
            ids=ids
        )
        
        return True

    def upsert_documents(
        self,
        documents: List[str],
        embeddings: List[List[float]],
        metadatas: List[Dict],
        ids: List[str]
    ) -> bool:
        """Insert-or-replace — the write for RE-enrichment. Chroma's ``add``
        with an id that already exists does not raise (verified on 1.5.9):
        it logs a warning and keeps the OLD embedding, document and
        metadata. The enricher's old add → (except) delete+re-add fallback
        therefore never fired, and a re-enriched item kept its stale vector
        forever. ``upsert`` replaces all three."""
        if embeddings:
            embeddings = [np.array(e, dtype=np.float32).tolist() for e in embeddings]
        _bump_domain_gen()
        self.collection.upsert(
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
            ids=ids
        )
        return True

    def use_collection(self, collection_name: str) -> None:
        """Re-point THIS wrapper at another collection, in place. Modules
        bind the wrapper at import (``from … import chroma_db``); swapping
        the object on a profile flip left them querying the old collection,
        and query()'s empty-result fallback hid it as "no neighbours"."""
        if collection_name == self.collection_name:
            return
        old = self.collection_name
        self.collection_name = collection_name
        try:
            self.collection = self._get_or_create_collection()
        except Exception:
            self.collection_name = old
            raise

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
                n_results=n_results + _MERGED_HEADROOM,
                where=where
            )
        except Exception as e:
            # Same resilience as get_by_id: a corrupt/inconsistent index segment
            # must not 500 callers (chat RAG, scoring). Return an empty result
            # in ChromaDB's nested shape so callers just see "no neighbours" —
            # but COUNT it: this fallback is exactly what masked corruption.
            _log_read_failure("query", e)
            _count_fallback("query")
            return {"ids": [[]], "distances": [[]], "documents": [[]],
                    "metadatas": [[]], "embeddings": [[]]}

        return drop_merged(results, n_results)
    
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
            _log_read_failure("get_by_id", e, detail=doc_id)
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

    def get_by_title(self, title: str, media_type: str, limit: int = 6) -> List[Dict]:
        """The docs whose metadata carries exactly this title and media type,
        with their embeddings. A series is indexed under the show's Plex
        key, which a watch-history row (an episode's key) does not know, and
        Plex hands out new keys when a library is re-added."""
        try:
            result = self.collection.get(
                where={"$and": [{"title": title}, {"media_type": media_type}]},
                include=["embeddings", "metadatas"], limit=limit,
            )
        except Exception as e:
            _log_read_failure("get_by_title", e, detail=title)
            return []
        result = result or {}
        embeddings = result.get("embeddings")
        metadatas = result.get("metadatas") or []
        return [{"id": r_id, "metadata": metadatas[j] if j < len(metadatas) else None,
                 "embedding": embeddings[j] if embeddings is not None and len(embeddings) > j else None}
                for j, r_id in enumerate(result.get("ids") or [])]

    def get_by_ids(self, doc_ids: List[str], chunk_size: int = 1000) -> Dict[str, Dict]:
        """Bulk fetch embeddings for many ids — one Chroma call per chunk.

        Returns ``{doc_id: {'id': ..., 'embedding': ...}}``; ids Chroma
        doesn't know are simply absent. Replacement for per-item
        ``get_by_id`` inside thousands-wide loops (deletion scoring /
        arr ranking), where one round-trip per item is the N+1 cost.

        A failed chunk retries its ids through ``get_by_id`` so a single
        corrupt HNSW segment costs only the genuinely unreadable ids —
        and the per-item fallback counter (the corruption tell, see
        ``get_by_id``) keeps counting per id, not per 1000.
        """
        results_map: Dict[str, Dict] = {}
        unique_ids = list(dict.fromkeys(doc_ids))
        for i in range(0, len(unique_ids), chunk_size):
            chunk = unique_ids[i:i + chunk_size]
            try:
                result = self.collection.get(ids=chunk, include=["embeddings"])
            except Exception as e:
                logger.debug("[chroma] get_by_ids chunk of %d failed (%s) — per-id fallback",
                             len(chunk), e)
                for doc_id in chunk:
                    one = self.get_by_id(doc_id)
                    if one is not None:
                        results_map[one["id"]] = one
                continue
            if result and result.get("ids"):
                embeddings = result.get("embeddings")
                for j, r_id in enumerate(result["ids"]):
                    results_map[r_id] = {
                        "id": r_id,
                        "embedding": embeddings[j]
                        if embeddings is not None and len(embeddings) > j else None,
                    }
        return results_map

    def delete_by_id(self, doc_id: str) -> bool:
        """Delete document by ID."""
        _bump_domain_gen()
        self.collection.delete(ids=[doc_id])
        return True
    
    def update_metadata(
        self,
        doc_id: str,
        metadata: Dict
    ) -> bool:
        """Update metadata for existing document."""
        _bump_domain_gen()
        self.collection.update(
            ids=[doc_id],
            metadatas=[metadata]
        )
        return True
    
    def embeddings_for_domain(self, domain: str, limit: int = 30000):
        """All stored embeddings of one domain (movie/show/anime/music) —
        used by the taste rebuild to compute GLOBAL calibration anchors
        (centroid-vs-corpus cosine p10/p90) instead of per-batch anchors.
        Returns the raw vectors as one float32 matrix (rows x dim; Chroma
        stores float32, so nothing is lost) or [] — caller normalizes.
        Memoized until the next write through this wrapper, at most
        _DOMAIN_EMB_TTL s; callers must not modify the matrix in place. 'show' unions the
        legacy 'tv' epoch via domain_where — the external eval measured show
        taste calibrated against the 289-doc 'show' slice while all watched
        series live under 'tv' (995 docs from the old Plex sync)."""
        import time as _time
        key = (getattr(self, "collection_name", None), domain, limit)
        with _domain_emb_lock:
            gen = _domain_write_gen[0]
            hit = _domain_emb_memo.get(key)
            if hit and hit[0] == gen and _time.monotonic() - hit[1] < _DOMAIN_EMB_TTL:
                return hit[2]
        try:
            res = self.collection.get(where=domain_where(domain),
                                      include=["embeddings", "metadatas"], limit=limit)
            emb = res.get("embeddings")
            if emb is None:
                return []
            metas = res.get("metadatas") or [None] * len(emb)
            # A doc merged into its live twin would count the title twice.
            rows = [e for e, m in zip(emb, metas) if not (m or {}).get(MERGED_INTO)]
            try:
                out = np.asarray(rows, dtype=np.float32) if rows else []
            except (TypeError, ValueError):   # ragged: mixed widths
                return rows
            if not isinstance(out, list) and out.ndim == 2:
                out.setflags(write=False)
                with _domain_emb_lock:
                    if _domain_write_gen[0] == gen:
                        # expired entries go too: only live corpora stay resident
                        now = _time.monotonic()
                        for k in [k for k, v in _domain_emb_memo.items()
                                  if now - v[1] >= _DOMAIN_EMB_TTL]:
                            del _domain_emb_memo[k]
                        _domain_emb_memo[key] = (gen, now, out)
            return out
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
            logger.warning("count_by_id_prefix(%r) failed: %s", prefix, e)
            return 0
    
    def all_ids(self) -> List[str]:
        """Every document id, cheaply (include=[] skips embeddings/documents).
        Used by the KB overview to count vector coverage per ITEM identity —
        raw id counts over-report because one item may carry ids from several
        keying epochs."""
        try:
            return list((self.collection.get(include=[]) or {}).get("ids") or [])
        except Exception as e:
            logger.warning("all_ids() failed: %s", e)
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
    wrapper switches collection so queries and writes move to the new
    collection together with the new model — never partially.

    The switch is IN PLACE (``use_collection``), not a new wrapper: every
    module that did ``from … import chroma_db`` holds this exact object,
    and a replacement would leave them on the old collection.
    """
    inst = _cached_wrapper()
    try:
        inst.use_collection(_wanted_collection())
    except Exception as e:
        # The wrapper stays on the OLD collection: after a profile flip every
        # query and write goes to the wrong model's vectors. Not DEBUG.
        logger.warning("[chroma] collection switch to the active profile failed "
                       "(still serving %r): %s", inst.collection_name, e)
    return inst


def _wanted_collection() -> str:
    from src.services.embed_service import get_profile
    return get_profile().get("collection") or "media_knowledge_v2"


def refresh_singleton() -> None:
    """Re-point the live wrapper at the active profile's collection (called
    by embed_service.set_profile). No wrapper built yet → nothing to do; the
    first get_chroma_db() opens the right collection anyway."""
    if _cached_wrapper.cache_info().currsize:
        _cached_wrapper().use_collection(_wanted_collection())


# ── Off-loop bulk reads ───────────────────────────────────────────────────────
#
# get_by_ids over a whole arr library and embeddings_for_domain (up to 30,000
# vectors) read and decode embeddings from disk for seconds. Called from a
# coroutine they froze the event loop — every request and SSE stream with it.
# They run on ONE dedicated thread: the loop stays free, and two bulk reads
# (a deletion scan and a taste rebuild) queue behind each other instead of
# competing for the same disk and the same SQLite file under Chroma.
_T = TypeVar("_T")
_chroma_pool: Optional[ThreadPoolExecutor] = None
_chroma_pool_lock = threading.Lock()


def _get_chroma_pool() -> ThreadPoolExecutor:
    global _chroma_pool
    with _chroma_pool_lock:
        if _chroma_pool is None:
            _chroma_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chroma")
        return _chroma_pool


async def run_chroma(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Run a blocking Chroma call (or a function built around one, NumPy
    included) on the Chroma thread and await its result."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_get_chroma_pool(), partial(fn, *args, **kwargs))


def shutdown_chroma_pool() -> None:
    """Let a queued bulk read finish, then drop the thread (shutdown)."""
    global _chroma_pool
    with _chroma_pool_lock:
        pool, _chroma_pool = _chroma_pool, None
    if pool is not None:
        pool.shutdown(wait=True)


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
