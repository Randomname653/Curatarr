"""
Curatarr — curation statistics: the aggregation layer CuratorResolutionLog
was built for ("the recap is a single GROUP BY away", models.py) but never
got. Everything here is cheap live SQL over small indexed tables; only the
LLM narrative (stats router) is cached.

v1 covers the CHANGELOG recap spec's Tier-1 core: monthly resolution +
GB-freed buckets, the Stubbornness Index (override-kept titles untouched
for 90+ days), the redundancy audit, watch-hours, and taste evolution.
Deliberate follow-ups (not v1): Wasted Lifetime, Trash-Tax, Graveyard,
MonthlyRecap persistence.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_STUBBORN_DAYS = 90


def _month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def _month_range(months: int, now: datetime = None) -> list:
    """Chronological list of the last *months* month keys, oldest first."""
    now = now or datetime.utcnow()
    keys = []
    y, m = now.year, now.month
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(keys))


def aggregate_resolutions(rows: list, months: int = 12,
                          now: datetime = None) -> list:
    """Pure: [(outcome, resolution_type, created_at)] → chronological month
    buckets [{month, deleted, kept, overrides, consensus}]. Months without
    activity stay present (zeroed) so the chart keeps its time axis."""
    buckets = {k: {"month": k, "deleted": 0, "kept": 0,
                   "overrides": 0, "consensus": 0}
               for k in _month_range(months, now)}
    for outcome, rtype, created_at in rows:
        if not created_at:
            continue
        b = buckets.get(_month_key(created_at))
        if b is None:
            continue
        if outcome == "deleted":
            b["deleted"] += 1
        elif outcome == "kept":
            b["kept"] += 1
        if rtype == "override":
            b["overrides"] += 1
        elif rtype == "consensus":
            b["consensus"] += 1
    return list(buckets.values())


def build_curation_stats(user_id: int, months: int = 12) -> dict:
    from sqlalchemy import func

    from src.database.connection import get_db_session
    from src.database.models import (CuratorResolutionLog, DeletionProposal,
                                     EncryptedTasteVector, WatchHistoryEntry)
    from src.services.size_norms import duplicate_report

    now = datetime.utcnow()
    cutoff = now - timedelta(days=31 * months)

    with get_db_session() as db:
        res_rows = (db.query(CuratorResolutionLog.outcome,
                             CuratorResolutionLog.resolution_type,
                             CuratorResolutionLog.created_at)
                    .filter(CuratorResolutionLog.user_id == user_id,
                            CuratorResolutionLog.created_at >= cutoff).all())
        months_out = aggregate_resolutions(res_rows, months, now)

        # GB freed per month from executed deletions
        freed = (db.query(func.strftime("%Y-%m", DeletionProposal.resolved_at),
                          func.sum(DeletionProposal.storage_mb))
                 .filter(DeletionProposal.user_id == user_id,
                         DeletionProposal.status == "deleted",
                         DeletionProposal.resolved_at >= cutoff)
                 .group_by(func.strftime("%Y-%m", DeletionProposal.resolved_at))
                 .all())
        freed_by_month = {k: round((v or 0) / 1024, 1) for k, v in freed}
        for b in months_out:
            b["gb_freed"] = freed_by_month.get(b["month"], 0.0)

        # Stubbornness Index: kept OVER the curator's objection, then never
        # touched again. (Exact-title match against watch_history — the kept
        # titles are few, so two cheap max() lookups per title.)
        stubborn = []
        overrides = (db.query(CuratorResolutionLog)
                     .filter(CuratorResolutionLog.user_id == user_id,
                             CuratorResolutionLog.outcome == "kept",
                             CuratorResolutionLog.resolution_type == "override")
                     .order_by(CuratorResolutionLog.created_at.desc())
                     .limit(50).all())
        seen_titles = set()
        for r in overrides:
            if r.title in seen_titles:
                continue
            seen_titles.add(r.title)
            last_play = (db.query(func.max(WatchHistoryEntry.viewed_at))
                         .filter(WatchHistoryEntry.user_id == user_id,
                                 (WatchHistoryEntry.title == r.title)
                                 | (WatchHistoryEntry.series_title == r.title))
                         .scalar())
            days = (now - last_play).days if last_play else None
            if days is None or days >= _STUBBORN_DAYS:
                stubborn.append({
                    "title": r.title, "category": r.category,
                    "kept_at": r.created_at.isoformat() if r.created_at else None,
                    "days_since_play": days,
                    "curator_stance": (r.curator_stance or "")[:160],
                })

        # Watch hours per category per month
        watch = (db.query(func.strftime("%Y-%m", WatchHistoryEntry.viewed_at),
                          WatchHistoryEntry.media_type,
                          func.sum(WatchHistoryEntry.duration_ms))
                 .filter(WatchHistoryEntry.user_id == user_id,
                         WatchHistoryEntry.viewed_at >= cutoff)
                 .group_by(func.strftime("%Y-%m", WatchHistoryEntry.viewed_at),
                           WatchHistoryEntry.media_type).all())
        watch_by_month: dict = {}
        for mk, mtype, ms in watch:
            watch_by_month.setdefault(mk, {})[mtype] = round((ms or 0) / 3_600_000, 1)

        # Taste evolution: the latest explicit feedback across this user's blobs
        import json as _json
        feedback = []
        for etv in (db.query(EncryptedTasteVector)
                    .filter(EncryptedTasteVector.user_id == user_id).all()):
            try:
                blob = _json.loads(etv.encrypted_blob or "{}")
                if blob.get("version") == 1:
                    continue
                for fb in blob.get("explicit_feedback") or []:
                    feedback.append({
                        "title": fb.get("title"),
                        "sentiment": fb.get("sentiment"),
                        "reason": (fb.get("reason") or "")[:120],
                        "date": fb.get("date"),
                        "weight": float(fb.get("weight") or 1.0),
                        "category": etv.media_category,
                    })
            except Exception:
                continue
        feedback.sort(key=lambda f: f.get("date") or "", reverse=True)

    totals = {
        "deleted": sum(b["deleted"] for b in months_out),
        "kept": sum(b["kept"] for b in months_out),
        "overrides": sum(b["overrides"] for b in months_out),
        "consensus": sum(b["consensus"] for b in months_out),
        "gb_freed": round(sum(b["gb_freed"] for b in months_out), 1),
    }
    return {
        "months": months_out,
        "totals": totals,
        "stubbornness": stubborn[:15],
        "duplicates": duplicate_report(),
        "watch_hours": watch_by_month,
        "taste_evolution": feedback[:10],
        "generated_at": now.isoformat(),
    }
