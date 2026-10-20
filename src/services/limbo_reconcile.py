"""
Curatarr — daily reconcile of deletions with an unknown outcome.

``limbo`` means "the delete was sent (or due) and nobody knows how it ended":
the arr went silent mid-DELETE, was unreachable during a bulk run, Lidarr's
index drifted, or the process died with the row claimed. Until 2026-10 a row
left limbo only when someone clicked Retry, so a delete that had in fact gone
through sat there for good: no resolution-log row, no freed-storage credit,
and the item counted as removed while nobody had confirmed it.

Once a day, per open limbo row:
  * the arr / Plex answers 404 for the item -> the same path as a Retry
    (``_delete_one_and_log`` treats 404 as "the earlier delete went through"),
    so the outcome, the resolution log and the SoulSync ban are written
    exactly as a click would;
  * the item is still there -> left in limbo (the owner approved the delete,
    not an unattended re-send) and, at most weekly, the owner is told;
  * the service does not answer -> untouched, tried again tomorrow.

It also adopts rows hidden in ``deleting`` that no delete of this process
holds (a status write that failed three times): they become limbo — visible,
retryable — instead of waiting months for the next boot to do the same. Only
a row that was already ``deleting`` on the previous pass: a delete claims
its row on the writer thread before it can note the claim in
_DELETE_CLAIMS, and no real delete lasts a day.
Nothing here sends a DELETE for an item that still exists.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_CLAIM_STALE_S = 3600.0          # the longest delete (Lidarr guard + 300 s + polls) is minutes
_NOTIFY_EVERY = timedelta(days=7)
_NOTIFY_TRIGGER = "limbo_deletions"
# Ids seen in "deleting" by the previous pass of this process.
_SEEN_DELETING: set = set()


def _adopt_orphaned_claims(held: set, seen: set) -> tuple:
    """'deleting' rows seen last pass that no delete of this process holds
    -> 'limbo'. Returns (adopted, ids still 'deleting' after this pass)."""
    from src.database.connection import get_db_session
    from src.database.models import DeletionProposal as DP
    with get_db_session() as db:
        now_deleting = {i for (i,) in db.query(DP.id).filter(DP.status == "deleting")}
        orphaned = sorted((now_deleting & seen) - held)
        n = 0
        if orphaned:
            n = (db.query(DP).filter(DP.id.in_(orphaned), DP.status == "deleting")
                 .update({"status": "limbo"}, synchronize_session=False))
            db.commit()
    return n, now_deleting - set(orphaned)


def _limbo_rows() -> list:
    from src.database.connection import get_db_session
    from src.database.models import DeletionProposal as DP
    from src.routers.recommendations import _Proposal
    with get_db_session() as db:
        return [(r.user_id, _Proposal(r)) for r in
                db.query(DP).filter(DP.status == "limbo").order_by(DP.id.asc()).all()]


def _item_url(service: str, media_id) -> tuple:
    """(url, headers) of the item a proposal deletes, or (None, None)."""
    from src.config import settings
    from src.routers.recommendations import _require_media_id
    mid = _require_media_id(media_id)
    if mid is None:
        return None, None
    if service == "plex":
        base, token = settings.effective_plex_url, settings.effective_plex_token
        if not base or not token:
            return None, None
        return (f"{str(base).rstrip('/')}/library/metadata/{mid}",
                {"X-Plex-Token": token, "Accept": "application/json"})
    arrs = {"radarr": (settings.effective_radarr_url, settings.RADARR_API_KEY, "api/v3/movie"),
            "sonarr": (settings.effective_sonarr_url, settings.SONARR_API_KEY, "api/v3/series"),
            "lidarr": (settings.effective_lidarr_url, settings.LIDARR_API_KEY, "api/v1/artist")}
    if service not in arrs:
        return None, None
    base, key, path = arrs[service]
    if not base or not key:
        return None, None
    return f"{str(base).rstrip('/')}/{path}/{mid}", {"X-Api-Key": key}


async def item_gone(client, service: str, media_id):
    """True = the service answers 404/410 for it, False = still there,
    None = no answer worth acting on."""
    url, headers = _item_url(service, media_id)
    if url is None:
        return None
    try:
        r = await client.get(url, headers=headers)
    except Exception as e:  # noqa: BLE001 - no proof either way; tomorrow again
        logger.debug("[limbo] %s lookup failed: %s", service, e)
        return None
    if r.status_code in (404, 410):
        return True
    if r.status_code == 200:
        return False
    return None


def _notify(per_user: dict) -> int:
    """One bubble per user and week naming the deletions still waiting."""
    from src.database.connection import get_db_session
    from src.database.models import ProactiveMessage
    sent = 0
    with get_db_session() as db:
        for uid, titles in per_user.items():
            recent = (db.query(ProactiveMessage.id)
                      .filter(ProactiveMessage.user_id == uid,
                              ProactiveMessage.trigger_type == _NOTIFY_TRIGGER,
                              ProactiveMessage.created_at > datetime.utcnow() - _NOTIFY_EVERY)
                      .first())
            if recent:
                continue
            shown = ", ".join(f'"{t}"' for t in titles[:3])
            more = f" and {len(titles) - 3} more" if len(titles) > 3 else ""
            db.add(ProactiveMessage(
                user_id=uid, trigger_type=_NOTIFY_TRIGGER,
                trigger_data=json.dumps({"titles": titles[:20]}),
                message=(f"{len(titles)} approved deletion(s) never went through — "
                         f"{shown}{more} {'are' if len(titles) > 1 else 'is'} still in "
                         "the library. Open Deletions to retry or keep them.")))
            sent += 1
        db.commit()
    return sent


async def reconcile_limbo(task=None) -> dict:
    import httpx
    from src.database.runner import run_read, run_write
    from src.routers import recommendations as recs
    from src.services.http_client import tls_context
    from src.services.task_monitor import task_monitor

    now = time.monotonic()
    held = {pid for pid, at in recs._DELETE_CLAIMS.items() if now - at < _CLAIM_STALE_S}
    adopted, still = await run_write(_adopt_orphaned_claims, held, set(_SEEN_DELETING))
    _SEEN_DELETING.clear()
    _SEEN_DELETING.update(still)
    stats = {"adopted": adopted, "resolved": 0, "still_present": 0, "unanswered": 0}
    rows = await run_read(_limbo_rows)
    if task is not None:
        task_monitor.update(task, total=len(rows))
    reachable = {svc: await recs._probe_arr(svc) for svc in {p.service for _, p in rows}}
    waiting: dict = {}
    async with httpx.AsyncClient(verify=tls_context(), timeout=15) as client:
        for i, (user_id, p) in enumerate(rows, 1):
            gone = await item_gone(client, p.service, p.media_id) if reachable.get(p.service) else None
            if gone is True:
                ok = await recs._delete_one_and_log(user_id, p)
                await recs._commit_outcome(p)
                if ok:
                    stats["resolved"] += 1
                    logger.info("[limbo] %r was already gone from %s — recorded as deleted",
                                p.title, p.service)
            elif gone is False:
                stats["still_present"] += 1
                waiting.setdefault(user_id, []).append(p.title)
            else:
                stats["unanswered"] += 1
            if task is not None:
                task_monitor.update(task, processed=i)
    if waiting:
        await run_write(_notify, waiting)
    if task is not None:
        task_monitor.update(task, message=", ".join(f"{k}: {v}" for k, v in stats.items()))
    if any(stats.values()):
        logger.info("[limbo] reconcile: %s", stats)
    return stats
