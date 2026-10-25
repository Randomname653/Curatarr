"""The judge's record: its verdicts and the protections it granted, kept.

Verdicts lived only in the 30-day ``verdict:`` cache (services/verdict_cache),
so the Wrap-Up in December could no longer say what the judge thought of a
title in spring. And a protection was hard-deleted when the owner lifted it
and overwritten when the judge re-judged it, so "you deleted what the judge
kept" had nothing to stand on.

* ``record_verdict`` writes a ``judge_verdicts`` row when a title's verdict
  first comes or changes. The same verdict at the next scan only moves
  ``last_at`` and counts ``scans``: a nightly scan that reuses a cached
  verdict adds no rows.
* ``archive_protection`` copies a ``ProtectedMedia`` row's state into
  ``protection_history`` before it is lifted or changed. The live table
  stays as every reader expects it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from src.database.connection import get_db_session

logger = logging.getLogger(__name__)

_PILLARS = ("pillar_3_household", "pillar_2_custodian", "pillar_1_resonance",
            "pillar_0_ego", "bitrate_note")


def item_key(item: dict) -> str:
    """The identifier protections use: the TMDB id, else the title."""
    tmdb_id = item.get("tmdb_id")
    return str(tmdb_id) if tmdb_id is not None else (item.get("title") or "")


def _pillars_json(verdict: dict) -> Optional[str]:
    found = {k: verdict[k] for k in _PILLARS if verdict.get(k)}
    if verdict.get("_guard_detail"):
        found["guard_detail"] = str(verdict["_guard_detail"])
    return json.dumps(found, ensure_ascii=False) if found else None


def record_verdict(user_id: int, item: dict, category: str, verdict: dict, *,
                   model_verdict: Optional[str] = None, score: Optional[float] = None,
                   model: str = "", verdict_key: str = "",
                   now: Optional[datetime] = None) -> Optional[str]:
    """Keep one verdict. Returns "new", "same" or None (nothing to keep:
    a failed verdict, or no identifier). Blocking — call via ``run_write``."""
    from src.database.models import JudgeVerdict as J
    v = (verdict or {}).get("verdict")
    key = item_key(item)
    if not v or verdict.get("_error") or not key:
        return None
    now = now or datetime.utcnow()
    guard = verdict.get("_guard") or None
    with get_db_session() as db:
        last = (db.query(J).filter(J.user_id == user_id, J.category == category,
                                   J.item_key == key)
                .order_by(J.last_at.desc(), J.id.desc()).first())
        if (last is not None and last.verdict == v and last.guard == guard
                and (last.verdict_key or "") == (verdict_key or "")):
            last.last_at, last.scans = now, (last.scans or 0) + 1
            if item.get("title"):
                last.title = item["title"]
            return "same"
        db.add(J(user_id=user_id, category=category, item_key=key,
                 title=item.get("title"), verdict=v,
                 model_verdict=model_verdict or v, guard=guard,
                 protecting_pillar=verdict.get("protecting_pillar") or None,
                 score=score, size_mb=item.get("size_mb"),
                 pillars_json=_pillars_json(verdict), model=model or None,
                 verdict_key=verdict_key or None, first_at=now, last_at=now, scans=1))
        return "new"


async def keep_verdict(*args, **kwargs) -> None:
    """``record_verdict`` on a worker; a failure is logged, never raised —
    the record must not cost a scan its proposals."""
    from src.database.runner import run_write
    try:
        await run_write(record_verdict, *args, **kwargs)
    except Exception as e:  # noqa: BLE001
        logger.warning("[judge record] verdict for %r not kept: %s",
                       (args[1] if len(args) > 1 else {}).get("title"), e)


def archive_protection(db, row, kind: str, *, by_user_id: Optional[int] = None,
                       new_verdict: Optional[str] = None) -> None:
    """Copy ``row`` (a ProtectedMedia) into protection_history before it is
    lifted or changed. Adds to ``db``; the caller commits."""
    from src.database.models import ProtectionHistory
    db.add(ProtectionHistory(
        protection_id=row.id, user_id=row.user_id, identifier=row.identifier,
        category=row.category, title=row.title, source=row.source,
        verdict=row.verdict, new_verdict=new_verdict, reason=row.reason,
        granted_at=row.created_at, kind=kind, by_user_id=by_user_id))
