"""Curatarr — taste snapshots: what each month of watching looked like.

``TasteVectorEntry`` is one row per user, overwritten on every rebuild, so
the profile itself had no history: no "what you were in March", no monthly
drift, no river of how a year's taste moved. The wrap-up's drift was
recomputed from scratch for one period against everything before it.

A snapshot is the played-time-weighted mean of a month's item vectors —
the same stored index vectors the taste engine uses
(``taste_engine._chroma_item_vector``) — per user, category and calendar
month (UTC). The vectors are static per item, so past months can be built
from the watch history today: the first run fills every month there is.

* A closed month is written once; the running month is rewritten on every
  run until it closes.
* The mean is stored UNCENTRED and readers centre it on the current corpus
  mean (``similarity``): raw text embeddings share one cone, uncentred two
  different months still read ~0.94, and a centre fixed at write time would
  drift as the corpus grows.
* A change of embedding model rebuilds every snapshot (old vectors are a
  different space).
* Under ``_MIN_VECTORS`` item vectors a month keeps its counts and top
  titles but no mean.
"""
import json
import logging
from datetime import datetime
from typing import Optional

from src.database import orphaned
from src.database.connection import get_db_session

logger = logging.getLogger(__name__)

CATEGORIES = ("movie", "show", "anime", "music")
_MIN_VECTORS = 3
_TOP = 5
_BUDGET = 240          # (user, category, month) builds per run — the backfill spreads out


def _month(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def _embed_model() -> str:
    from src.config import settings
    return settings.EMBEDDING_MODEL or ""


def due(now: Optional[datetime] = None) -> list:
    """[(user_id, category, month)] still to build: months with plays that
    have no snapshot, one built in another embedding model, and the running
    month. Blocking — call via ``run_read``."""
    from sqlalchemy import func
    from src.database.models import TasteSnapshot as S, WatchHistoryEntry as W
    now = now or datetime.utcnow()
    current, model = _month(now), _embed_model()
    with get_db_session() as db:
        orphaned.include(db)        # plays of media gone from Plex count
        month = func.strftime("%Y-%m", W.viewed_at)
        played = {(u, c, m) for u, c, m in db.query(W.user_id, W.media_type, month)
                  .filter(W.media_type.in_(CATEGORIES), W.viewed_at < now).distinct()}
        have = {(u, c, m) for u, c, m, e in db.query(S.user_id, S.category, S.month, S.embed_model)
                if e == model and m != current}
    return sorted(played - have, key=lambda k: (k[2], k[0], k[1]))


def _month_rows(user_id: int, category: str, month: str) -> list:
    """The month's titles with their played time, heaviest first."""
    from sqlalchemy import func
    from src.database.models import WatchHistoryEntry as W
    from src.services.wrapup_facts import a_play, played_ms_sql
    ms = played_ms_sql(W)
    key = func.coalesce(W.series_title, W.title)
    with get_db_session() as db:
        orphaned.include(db)        # plays of media gone from Plex count
        q = (db.query(key, func.max(W.series_title), func.max(W.plex_item_id), func.sum(ms), func.count())
             .filter(W.user_id == user_id, W.media_type == category, a_play(W),
                     func.strftime("%Y-%m", W.viewed_at) == month)
             .group_by(key).order_by(func.sum(ms).desc()))
        return [{"title": k, "series_title": s, "plex_item_id": pk, "media_type": category,
                 "w": float(v or 0), "plays": int(n)} for k, s, pk, v, n in q if k]


def _mean_sync(rows: list) -> tuple:
    """(weighted mean of the unit item vectors or None, how many had one).
    Chroma reads — on the Chroma thread (``run_chroma``)."""
    import numpy as np
    from src.services.taste_engine import _chroma_item_vector
    vs, ws = [], []
    for r in rows:
        v = _chroma_item_vector(r, None)
        if not v:
            continue
        a = np.asarray(v, dtype=float)
        n = np.linalg.norm(a)
        if n > 0 and (not vs or len(a) == len(vs[0])):
            vs.append(a / n)
            ws.append(max(r["w"], 1.0))
    if len(vs) < _MIN_VECTORS:
        return None, len(vs)
    w = np.asarray(ws)
    mean = (np.stack(vs) * w[:, None]).sum(axis=0) / w.sum()
    return [round(float(x), 6) for x in mean], len(vs)


def _save(user_id: int, category: str, month: str, rows: list, mean, nvec: int) -> None:
    from src.database.models import TasteSnapshot as S
    with get_db_session() as db:
        row = db.query(S).filter_by(user_id=user_id, category=category, month=month).first()
        if row is None:
            row = S(user_id=user_id, category=category, month=month)
            db.add(row)
        row.plays = sum(r["plays"] for r in rows)
        row.hours = round(sum(r["w"] for r in rows) / 3_600_000, 2)
        row.titles = len(rows)
        row.vectors = nvec
        row.mean_json = json.dumps(mean) if mean is not None else None
        row.top_titles = json.dumps([r["title"] for r in rows[:_TOP]])
        row.embed_model = _embed_model()
        row.computed_at = datetime.utcnow()
        db.commit()


async def run_taste_snapshots(task=None, now: Optional[datetime] = None) -> bool:
    """The custodian job: build what is due, up to the run's budget. True
    when nothing is left over."""
    from src.database.runner import run_read, run_write
    from src.services.task_monitor import task_monitor
    from src.vector_store.chromadb_wrapper import run_chroma
    todo = await run_read(due, now)
    batch = todo[:_BUDGET]
    for i, (uid, cat, month) in enumerate(batch, 1):
        rows = await run_read(_month_rows, uid, cat, month)
        if not rows:
            # nothing nameable that month: stored empty, so it is not due again
            await run_write(_save, uid, cat, month, [], None, 0)
            continue
        try:
            mean, nvec = await run_chroma(_mean_sync, rows)
        except Exception as e:  # noqa: BLE001 - one month left for the next run
            logger.warning("[taste-snapshots] %s/%s/%s not built: %s", uid, cat, month, e)
            continue
        await run_write(_save, uid, cat, month, rows, mean, nvec)
        if task is not None:
            task_monitor.update(task, processed=i, total=len(batch), message=f"{cat} {month}")
    if batch:
        logger.info("[taste-snapshots] built %d month(s), %d still due", len(batch),
                    len(todo) - len(batch))
    return len(todo) <= _BUDGET


# ── reading them ────────────────────────────────────────────────────────────

def similarity(a: list, b: list, centre=None) -> Optional[float]:
    """Cosine of two stored means after centring both on ``centre`` (the
    corpus mean; None = uncentred)."""
    import numpy as np
    if not a or not b or len(a) != len(b):
        return None
    x, y = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if centre is not None and len(centre) == len(a):
        c = np.asarray(centre, dtype=float)
        x, y = x - c, y - c
    nx, ny = np.linalg.norm(x), np.linalg.norm(y)
    if nx == 0 or ny == 0:
        return None
    return round(float(x @ y / (nx * ny)), 3)


def series(user_id: int, category: str) -> list:
    """The user's months in one category, oldest first: counts, top titles
    and the stored mean (or None). Blocking — call via ``run_read``."""
    from src.database.models import TasteSnapshot as S
    model = _embed_model()
    with get_db_session() as db:
        return [{"month": r.month, "plays": r.plays, "hours": r.hours, "titles": r.titles,
                 "top_titles": json.loads(r.top_titles or "[]"),
                 "mean": json.loads(r.mean_json) if r.mean_json and r.embed_model == model else None}
                for r in db.query(S).filter_by(user_id=user_id, category=category).order_by(S.month)]


def month_over_month(months: list, centre=None) -> list:
    """[(month, similarity to the previous month with a mean)] — the drift
    river's backbone; None where a month has no mean."""
    out, prev = [], None
    for m in months:
        sim = similarity(prev, m["mean"], centre) if prev is not None and m["mean"] else None
        out.append((m["month"], sim))
        if m["mean"]:
            prev = m["mean"]
    return out
