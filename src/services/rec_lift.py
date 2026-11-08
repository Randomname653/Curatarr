"""Curatarr — a "Loved" moves its nearest picks up, visibly and at once.

A Loved answer in Swipe, or "Loved it" on the landing's follow-up card,
fed the taste rebuild - and the weekly recommendation cache, the TV's
"Curatarr Recommended" rows and the Recommendations page showed nothing of
it for days (UX audit 2026-11-08, S5 / L3). Now the member's stored picks
nearest the loved title (at most LIFT_MAX, by centred cosine like
because_recs) are lifted: they sort first in their lane and in the next
playlist push, the Recommendations card says why, and the answer reports
how many moved, so "it moved up" is only ever said when it did.

A re-rank of rows that are already there, not a regeneration: no model
call, nothing invented, and the next weekly regeneration rebuilds the rows
on the refreshed taste vector. Lanes: library and because (owned titles the
TV rows are made of).
"""
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

LIFT_MAX = 3
LIFT_MIN = 0.2          # centred cosine: below this a pick is no neighbour of the loved title
LANES = ("library", "because")


def _picks(user_id: int, category: str) -> list[tuple]:
    """[(row id, title)] of the member's stored picks in the lifting lanes."""
    from src.database.connection import get_db_session
    from src.database.models import CachedRecommendation as R
    with get_db_session() as db:
        return [(i, t) for i, t in db.query(R.id, R.title).filter(
            R.user_id == user_id, R.category == category, R.lane.in_(LANES))]


def nearest(seed_vec, picks: list, mean, n: int = LIFT_MAX, min_fit: float = LIFT_MIN) -> list:
    """[(fit, item)] for the ``n`` picks closest to ``seed_vec``, nearest
    first. ``picks``: [(vector, item)]. Pure."""
    from src.services.because_recs import _centred, _unit
    s = _centred(_unit(seed_vec), mean)
    if s is None:
        return []
    scored = []
    for vec, item in picks:
        v = _centred(_unit(vec), mean)
        if v is None or v.shape != s.shape:
            continue
        fit = float(s @ v)
        if fit >= min_fit:
            scored.append((fit, item))
    scored.sort(key=lambda x: -x[0])
    return scored[:n]


def _mark(ids: list, loved_title: str, now: datetime) -> None:
    from src.database.connection import get_db_session
    from src.database.models import CachedRecommendation as R
    with get_db_session() as db:
        db.query(R).filter(R.id.in_(ids)).update(
            {"lifted_at": now, "lifted_by": loved_title[:500]}, synchronize_session=False)
        db.commit()


def unlift(user_id: int, category: str, loved_title: str) -> int:
    """Take back the lift a Loved of ``loved_title`` gave (an undone Swipe
    answer): its picks fall back to their match order. Returns how many.
    Blocking."""
    from src.database.connection import get_db_session
    from src.database.models import CachedRecommendation as R
    with get_db_session() as db:
        n = (db.query(R).filter(R.user_id == user_id, R.category == category,
                                R.lifted_by == loved_title[:500])
             .update({"lifted_at": None, "lifted_by": None}, synchronize_session=False))
        db.commit()
    return n


async def lift_near(user_id: int, category: str, loved_title: str,
                    seed_doc_id: Optional[str] = None, now: Optional[datetime] = None) -> list[str]:
    """Lift the member's stored picks nearest ``loved_title`` (its vector by
    ``seed_doc_id``, else by its enrichment key). Returns the lifted titles,
    nearest first; [] when nothing is close, or anything is missing. Never
    raises: the Loved itself is stored either way."""
    from src.database.runner import run_read, run_write
    from src.services.swipe import _anchor_keys, corpus_mean
    from src.vector_store.chromadb_wrapper import get_chroma_db, run_chroma
    try:
        picks = [(i, t) for i, t in await run_read(_picks, user_id, category)
                 if t.lower() != (loved_title or "").lower()]
        if not picks:
            return []
        keys = await run_read(_anchor_keys, category, [t for _, t in picks] + [loved_title])
        seed = seed_doc_id or keys.get((loved_title or "").lower())
        if not seed:
            return []
        mean = await corpus_mean(category)
        if mean is None:
            return []
        doc = {i: keys.get(t.lower()) for i, t in picks}
        embs = await run_chroma(get_chroma_db().get_by_ids,
                                [seed] + [d for d in doc.values() if d])
        seed_vec = (embs.get(seed) or {}).get("embedding")
        if seed_vec is None:
            return []
        cands = [((embs.get(doc[i]) or {}).get("embedding"), (i, t))
                 for i, t in picks if doc[i]]
        chosen = nearest(seed_vec, [c for c in cands if c[0] is not None], mean)
        if not chosen:
            return []
        await run_write(_mark, [i for _, (i, _t) in chosen], loved_title, now or datetime.utcnow())
        titles = [t for _, (_i, t) in chosen]
        logger.info("[rec-lift] user %d: %s lifted %s", user_id, loved_title, titles)
        return titles
    except Exception as e:  # noqa: BLE001 - the lift is a courtesy on top of a stored answer
        logger.warning("[rec-lift] user %d, %r: no lift: %s", user_id, loved_title, e)
        return []
