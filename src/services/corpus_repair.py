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

import asyncio
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Same guard class as media_enricher's merge: an adult genre in a bare
# prefetch genre list (arr/TVDB-fed) is exactly how the poisoning started.
_ADULT_GENRES = {"hentai", "erotica"}

# Metadata key stamped on every doc this module rebuilt (see the upsert).
REBUILT_MARK = "rebuilt_from_prefetch"


async def rebuild_doc_from_prefetch(doc_id: str, fallback_domain: str = "") -> bool:
    """Rebuild ONE parent doc from its raw_prefetch cache entry. Returns True
    when the doc (and its facets) were rewritten; False when no trustworthy
    prefetch data exists (the doc is left untouched — knowledge preserved)."""
    if not doc_id:
        return False
    try:
        from src.cache.metadata_cache import MetadataCache, _CACHE_VERSION

        def _read_prefetch():
            mc = MetadataCache()
            try:
                hit = mc.get_cache(f"raw_prefetch:{doc_id}")
                raw = (hit or {}).get("response")
                if not isinstance(raw, dict):
                    # STALE fallback: gone-from-arr media never refresh their
                    # prefetch, so an EXPIRED row is the final — and only —
                    # trustworthy record. get_cache filters expiry; read past it.
                    try:
                        import json as _json
                        for k in (f"{_CACHE_VERSION}:raw_prefetch:{doc_id}",
                                  f"raw_prefetch:{doc_id}"):
                            row = mc.conn.execute(
                                "SELECT response FROM api_cache WHERE cache_key=?",
                                (k,)).fetchone()
                            if row:
                                cand = _json.loads(row[0])
                                if isinstance(cand, dict):
                                    raw = cand
                                    break
                    except (ValueError, TypeError):
                        pass    # an unparseable row is no trustworthy record
                    except Exception as e:
                        logger.warning("[corpus-repair] stale prefetch read failed "
                                       "for %s: %s", doc_id, e)
            finally:
                mc.close()
            return raw

        # The cache file has its own lock: read it on a worker thread.
        raw = await asyncio.to_thread(_read_prefetch)
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

        from src.vector_store.chromadb_wrapper import get_chroma_db, run_chroma
        chroma = get_chroma_db()
        meta = {"title": f"{title} ({year})" if year else title,
                "media_type": domain, "domain": domain,
                "genres": ", ".join(genres),
                "themes": ", ".join(keywords[:6]),
                "mood": "", "year": year,
                # The repair marker: a gone-from-arr doc has no enriched:* row
                # to make it look alive, so without it the weekly zombie walk
                # re-classified and re-embedded the same docs forever (and the
                # same first 150 took the cap every week). A real re-enrichment
                # replaces the metadata wholesale, which drops the marker.
                REBUILT_MARK: datetime.utcnow().isoformat(timespec="seconds")}
        # ONE upsert, not delete-then-add: when add raised after the delete,
        # the doc was gone yet reported "left untouched", and a gone-from-arr
        # item never re-enriches — the knowledge this module exists to keep
        # was lost. An upsert either replaces the doc or leaves the old one.
        try:
            await run_chroma(chroma.upsert_documents, documents=[doc_text],
                             embeddings=[vecs[0]], metadatas=[meta], ids=[str(doc_id)])
        except Exception as e:
            logger.warning("[repair] write failed for %s (old doc kept): %s",
                           doc_id, e, exc_info=True)
            return False
        from src.services.facet_index import write_facets
        await write_facets(str(doc_id), meta["title"], domain,
                           meta["genres"], keywords[:6])
        logger.info("[repair] rebuilt %s from prefetch (%s, %d keywords)",
                    doc_id, domain, len(keywords))
        return True
    except Exception as e:
        logger.warning("[repair] rebuild failed for %s: %s", doc_id, e)
        return False


# ── merging a re-added title's old doc into its live twin ─────────────────────
# Deleting a title in an arr and adding it back gives it a new arr id, so a
# second doc appears (``radarr:7`` gone, ``radarr:912`` live). The old one is
# not rebuilt — that only re-embedded a duplicate — and not deleted either
# (keep the knowledge): it is marked ``merged_into`` the live doc, which hides
# it from similarity search. Its facets go: they are derived points, and the
# live doc writes its own.

_ID_FIELDS = (("tmdb_id", "tmdb"), ("tvdb_id", "tvdb"), ("anilist_id", "anilist"),
              ("mbid", "mbid"), ("musicbrainz_id", "mbid"))


def identity_keys(service: str, rec: dict) -> set:
    """``{(service, kind, id)}`` an item can be recognised by — the same
    service only: a film and a series never share an identity."""
    out = set()
    for field, kind in _ID_FIELDS:
        v = (rec or {}).get(field)
        if v not in (None, "", 0):
            out.add((service, kind, str(v).strip().lower()))
    return out


def _gone_ids_from_db(doc_ids: list) -> dict:
    """Blocking (run_read): the ids ArrEnrichmentStatus still holds for gone
    arr items — the status row outlives the item."""
    from src.database.connection import get_db_session
    from src.database.models import ArrEnrichmentStatus
    out: dict = {}
    by_svc: dict = {}
    for d in doc_ids:
        svc, _, aid = d.partition(":")
        if aid.isdigit():
            by_svc.setdefault(svc, {})[int(aid)] = d
    with get_db_session() as db:
        for svc, ids in by_svc.items():
            for r in (db.query(ArrEnrichmentStatus.arr_id, ArrEnrichmentStatus.tmdb_id,
                               ArrEnrichmentStatus.tvdb_id)
                      .filter(ArrEnrichmentStatus.service == svc,
                              ArrEnrichmentStatus.arr_id.in_(list(ids))).all()):
                out[ids[r.arr_id]] = {"tmdb_id": r.tmdb_id, "tvdb_id": r.tvdb_id}
    return out


def _gone_ids_from_prefetch(doc_ids: list) -> dict:
    """Blocking (worker thread): the ids in each gone doc's prefetch row."""
    from src.cache.metadata_cache import MetadataCache
    out: dict = {}
    mc = MetadataCache()
    try:
        for d in doc_ids:
            hit = mc.get_cache(f"raw_prefetch:{d}")
            raw = (hit or {}).get("response")
            if isinstance(raw, dict):
                out[d] = raw
    finally:
        mc.close()
    return out


async def find_live_twins(gone_ids: list, live_index: dict) -> dict:
    """``{gone_doc_id: live_doc_id}`` for gone docs whose title is live under
    another id of the same service. ``live_index`` maps identity_keys of the
    arr's live items to their doc id. Unreadable ids find no twin (the doc is
    then handled as before)."""
    if not gone_ids or not live_index:
        return {}
    from src.database.runner import run_read
    ids: dict = {}
    try:
        for d, rec in (await run_read(_gone_ids_from_db, gone_ids)).items():
            ids.setdefault(d, set()).update(identity_keys(d.split(":", 1)[0], rec))
    except Exception as e:
        logger.warning("[corpus-repair] twin lookup (status rows) failed: %s", e)
    try:
        for d, rec in (await asyncio.to_thread(_gone_ids_from_prefetch, gone_ids)).items():
            ids.setdefault(d, set()).update(identity_keys(d.split(":", 1)[0], rec))
    except Exception as e:
        logger.warning("[corpus-repair] twin lookup (prefetch) failed: %s", e)
    out = {}
    for d, keys in ids.items():
        twins = {live_index[k] for k in keys if k in live_index} - {d}
        if len(twins) == 1:           # two candidates is a guess, not a twin
            out[d] = twins.pop()
    return out


async def merge_into_live(doc_id: str, live_id: str) -> bool:
    """Mark ``doc_id`` as merged into ``live_id`` (kept, hidden from search)
    and drop its facet points. False when the doc could not be updated."""
    from src.vector_store.chromadb_wrapper import MERGED_INTO, get_chroma_db, run_chroma
    try:
        chroma = get_chroma_db()
        doc = await run_chroma(chroma.get_by_id, doc_id)
        if not doc:
            return False
        meta = dict(doc.get("metadata") or {})
        meta[MERGED_INTO] = live_id
        meta["merged_at"] = datetime.utcnow().isoformat(timespec="seconds")
        await run_chroma(chroma.update_metadata, doc_id, meta)
        await run_chroma(chroma.facets_delete_by_parent, doc_id)
        logger.info("[repair] merged %s into its live twin %s", doc_id, live_id)
        return True
    except Exception as e:
        logger.warning("[repair] merge of %s into %s failed: %s", doc_id, live_id, e)
        return False
