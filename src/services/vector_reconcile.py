"""
Curatarr — SQLite <-> ChromaDB reconciliation (2026-10 lifecycle audit).

State lives in three stores: curatarr.db (EnrichmentStatus), the metadata
cache (the polished ``enriched:*`` profiles) and ChromaDB (the vectors). The
daily backup covers only the first. Restore curatarr.db over a wiped or older
vector store and nothing noticed: the enrichment pre-filter skips every row
with ``enriched=1 AND error IS NULL``, the audit's zombie walk heals docs that
lost their profile but never profiles that lost their doc, and the library
stats count ``vector_ready`` out of SQLite, so the UI read "fully indexed"
over an empty collection while search and recommendations quietly thinned.

This pass joins the three, keyed the way the audit already joins them: each
polished profile names its own ``plex_rating_key``, and its Chroma doc id is
``plex_rating_key or tmdb_id or anilist_id or title`` (store_profile_vector).
That covers every keying, whether Plex ``ratingKey``, ``radarr:42`` or a
``tmdb``/``anilist`` fallback; ``vector_ready`` alone could not (only the
taste engine and the backfill script ever set it).

Per enriched row with no doc:
  * its profile is cached  -> re-embed from the cache (one embedding call, no
    summariser), capped per run, then ``vector_ready = 1``;
  * no readable profile    -> ``enriched = 0``: the normal enrichment cycle
    rebuilds it (the knowledge is already gone from both stores);
  * profile with no text   -> nothing to embed, left alone (counted).

Never destructive: it adds vectors and re-queues rows, it deletes nothing.
A Chroma read that fails raises (the task fails and backs off) instead of
reading as "every doc is missing".
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

JOB_ID = "vector_reconcile"
_REEMBED_BATCH = 300      # embeds per run; a full wipe drains across ticks
# Re-queues per run. Each one costs the enrichment cycle a full rebuild, and
# a row from an older keying epoch can have its doc under a different id —
# drained gradually, never as one flood.
_REQUEUE_BATCH = 500
_DRIFT_RATIO = 0.5        # docs for < half the enriched rows = desync, not noise
_DRIFT_MIN_ROWS = 50      # a tiny library is not evidence of anything
_ALERT_EVERY = timedelta(days=7)
_ALERT_TRIGGER = "vector_desync"


def doc_id_for(profile: dict) -> str:
    """The Chroma doc id store_profile_vector writes for *profile*."""
    return str(profile.get("plex_rating_key") or profile.get("tmdb_id")
               or profile.get("anilist_id") or profile.get("title") or "")


def _chroma_ids() -> set:
    """Every doc id. Raises on a failed read: ChromaDBWrapper.all_ids()
    answers [] on error, which here would read as a wiped store."""
    from src.vector_store.chromadb_wrapper import get_chroma_db
    got = get_chroma_db().collection.get(include=[]) or {}
    return {str(i) for i in (got.get("ids") or [])}


def _enriched_rows() -> dict:
    """plex_rating_key -> (id, media_category, vector_ready) for every row the
    pre-filter treats as done."""
    from src.database.connection import get_db_session
    from src.database.models import EnrichmentStatus as ES
    with get_db_session() as db:
        return {str(k): (i, cat, bool(vr)) for i, k, cat, vr in db.query(
            ES.id, ES.plex_rating_key, ES.media_category, ES.vector_ready)
            .filter(ES.enriched == True, ES.error.is_(None)).all()}  # noqa: E712


def _profile_index(keys: set) -> dict:
    """plex_rating_key -> (cache_key, doc_id, has_text, category) for the live profiles
    of *keys*. Streams the rows and keeps three fields each: the profiles of a
    large library would not fit comfortably in memory at once."""
    from src.cache.metadata_cache import MetadataCache, _CACHE_VERSION
    out: dict = {}
    cache = MetadataCache()
    try:
        cur = cache.conn.execute(
            "SELECT cache_key, response FROM api_cache WHERE cache_key LIKE ? "
            "AND expires_at > ?",
            # Local time, as MetadataCache._expires_at writes it.
            (f"{_CACHE_VERSION}:enriched:%", datetime.now().isoformat()))
        for row in cur:
            try:
                prof = json.loads(row["response"])
            except (TypeError, ValueError):
                continue
            if not isinstance(prof, dict) or prof.get("_not_found"):
                continue
            prk = str(prof.get("plex_rating_key") or "")
            if prk not in keys:
                continue
            key = row["cache_key"][len(_CACHE_VERSION) + 1:]       # enriched:{cat}:{id}
            cat = key.split(":", 2)[1] if key.count(":") >= 2 else ""
            # Music artists embed a genre/tag line when the summariser wrote
            # no embedding_text (store_profile_vector does the same).
            has_text = bool(prof.get("embedding_text")) or cat == "music"
            out[prk] = (key, doc_id_for(prof), has_text, cat)
    finally:
        cache.close()
    return out


def _read_profile(cache_key: str):
    from src.cache.metadata_cache import MetadataCache
    cache = MetadataCache()
    try:
        hit = cache.get_cache(cache_key)
        return (hit or {}).get("response")
    finally:
        cache.close()


def _set_flags(ids: list, **values) -> None:
    from src.database.connection import get_db_session
    from src.database.models import EnrichmentStatus as ES
    with get_db_session() as db:
        for i in range(0, len(ids), 500):
            (db.query(ES).filter(ES.id.in_(ids[i:i + 500]))
               .update(values, synchronize_session=False))
        db.commit()


def _alert_admin(enriched: int, docs: int) -> bool:
    """One admin bubble per week while the stores disagree."""
    from src.database.connection import get_db_session
    from src.database.models import ProactiveMessage, User
    with get_db_session() as db:
        admin = db.query(User).filter_by(is_admin=True).first()
        if not admin:
            return False
        recent = (db.query(ProactiveMessage.id)
                  .filter(ProactiveMessage.user_id == admin.id,
                          ProactiveMessage.trigger_type == _ALERT_TRIGGER,
                          ProactiveMessage.created_at > datetime.utcnow() - _ALERT_EVERY)
                  .first())
        if recent:
            return False
        db.add(ProactiveMessage(
            user_id=admin.id, trigger_type=_ALERT_TRIGGER,
            trigger_data=json.dumps({"enriched": enriched, "docs": docs}),
            message=(f"The vector store holds documents for only {docs:,} of "
                     f"{enriched:,} enriched titles — it looks restored from a "
                     "different point in time than the database. Curatarr is "
                     "re-embedding the missing ones in the background; search "
                     "and recommendations fill back in over the next days.")))
        db.commit()
        return True


def is_drift(enriched: int, missing: int) -> bool:
    return enriched >= _DRIFT_MIN_ROWS and (enriched - missing) < _DRIFT_RATIO * enriched


async def reconcile_vectors(task=None) -> bool:
    """One pass; True when nothing actionable is left, False while re-embeds
    remain (the custodian keeps the task due and continues next tick)."""
    import asyncio
    from src.database.runner import run_read, run_write
    from src.services.task_monitor import task_monitor
    from src.vector_store.chromadb_wrapper import run_chroma

    def _say(msg: str) -> None:
        if task is not None:
            task_monitor.update(task, message=msg)

    present = await run_chroma(_chroma_ids)
    rows = await run_read(_enriched_rows)
    if not rows:
        _say("No enriched rows")
        return True
    profiles = await asyncio.to_thread(_profile_index, set(rows))

    reembed, requeue, no_text, stale_flag, true_flag = [], [], 0, [], []
    for prk, (row_id, _cat, ready) in rows.items():
        prof = profiles.get(prk)
        # An expired profile with its doc intact is the audit's business.
        if (prof is not None and prof[1] in present) or (prof is None and prk in present):
            if not ready:
                true_flag.append(row_id)
            continue
        if prof is not None and not prof[2]:
            no_text += 1
            continue
        if prof is not None:
            reembed.append((row_id, prof[0], prof[3]))
        else:
            requeue.append(row_id)
        if ready:
            stale_flag.append(row_id)
    missing = len(reembed) + len(requeue)
    requeue_now = requeue[:_REQUEUE_BATCH]

    # The library stats read vector_ready: make it say what is really there.
    if stale_flag:
        await run_write(_set_flags, stale_flag, vector_ready=False)
    if true_flag:
        await run_write(_set_flags, true_flag, vector_ready=True)
    if requeue_now:
        await run_write(_set_flags, requeue_now, enriched=False, vector_ready=False)
    if is_drift(len(rows), missing):
        logger.critical(
            "[vectors] ChromaDB holds docs for %d of %d enriched rows — restored "
            "database without its vector store? Re-embedding %d from the cache, "
            "re-queueing %d for enrichment", len(rows) - missing, len(rows),
            len(reembed), len(requeue_now))
        await run_write(_alert_admin, len(rows), len(rows) - missing)

    from src.services.media_enricher import store_profile_vector
    from src.cache.metadata_cache import acache_set
    done, failed, landed = 0, 0, []
    for row_id, cache_key, cat in reembed[:_REEMBED_BATCH]:
        prof = await asyncio.to_thread(_read_profile, cache_key)
        if not prof:
            continue
        if await store_profile_vector(prof, media_type=prof.get("media_type") or cat or "movie",
                                      fallback_title=prof.get("title") or ""):
            landed.append(row_id)
            if prof.pop("vector_pending", None):
                await acache_set(cache_key, prof, days=30)
            done += 1
        else:
            failed += 1
        if task is not None and (done + failed) % 25 == 0:
            task_monitor.update(task, processed=done + failed,
                                total=min(len(reembed), _REEMBED_BATCH))
    if landed:
        await run_write(_set_flags, landed, vector_ready=True)

    left = len(reembed) - done + len(requeue) - len(requeue_now)
    summary = (f"{len(rows):,} enriched, {len(present):,} docs · re-embedded {done}"
               f"{f', {failed} failed' if failed else ''} · re-queued {len(requeue_now)}"
               f"{f' · {no_text} with nothing to embed' if no_text else ''}"
               f"{f' · {left} left' if left > 0 else ''}")
    _say(summary)
    logger.info("[vectors] reconcile: %s", summary)
    if failed and not done:
        from src.services.data_custodian import TaskFailed
        raise TaskFailed(f"{failed} re-embeds failed (embedder or Chroma down?)")
    return left <= 0


async def boot_drift_check() -> None:
    """Cheap startup look (two counts): when the stores disagree badly, clear
    the task's stamp so the custodian's first tick reconciles. A restored
    database carries its own, possibly recent, stamp."""
    from src.database.runner import run_read, run_write
    from src.services.app_state import set_state
    from src.vector_store.chromadb_wrapper import get_chroma_db, run_chroma

    def _enriched_count() -> int:
        from src.database.connection import get_db_session
        from src.database.models import EnrichmentStatus as ES
        with get_db_session() as db:
            return (db.query(ES.id).filter(ES.enriched == True,  # noqa: E712
                                           ES.error.is_(None)).count())
    try:
        enriched = await run_read(_enriched_count)
        docs = await run_chroma(lambda: get_chroma_db().get_count())
    except Exception as e:  # noqa: BLE001 - reported; the daily pass still runs
        logger.warning("[vectors] boot drift check skipped: %s", e)
        return
    if is_drift(enriched, max(0, enriched - docs)):
        logger.warning("[vectors] %d enriched rows, %d docs — reconciling on the "
                       "first maintenance tick", enriched, docs)
        await run_write(set_state, f"job_last_run:{JOB_ID}", "")
