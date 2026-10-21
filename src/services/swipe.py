"""Swipe: rate library titles near your taste, one card at a time (2026-10-21).

The taste vector only knows what Plex saw. A household member who watches
on Netflix, a title seen at a friend's place, a film from years ago - none of
it reached the profile. The Swipe view walks the titles closest to the
user's taste vector that they have neither played in Plex nor answered yet:
"not seen", or "seen" with stars out of 10.

A seen-and-rated title joins the next taste rebuild as a weighted entry
(``load_swipe_entries``): 10 stars pull the vector toward it, 1 star pushes
it away like an abandoned series, "seen" without stars counts mildly in
favour. "Not seen" carries no taste signal; it keeps the title out of the
next stack. Every REBUILD_EVERY answers in a category rebuild that
category's vector in the background, without LLM summaries.
"""
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

SWIPE_CATEGORIES = ("movie", "show", "anime")
# Weight of a 10-star (or, negated, a 1-star) answer in the taste average. A
# watched series weighs recency x (1 + log10(episodes)): roughly 0.3-3.
SWIPE_MAX_WEIGHT = 1.5
SWIPE_SEEN_UNRATED_WEIGHT = 0.5
REBUILD_EVERY = 10
# How many neighbours of the taste vector to look through for one stack.
_POOL = 200


def star_weight(seen: bool, stars: Optional[int]) -> float:
    """The taste weight of one answer: 0 for "not seen"."""
    if not seen:
        return 0.0
    if stars is None:
        return SWIPE_SEEN_UNRATED_WEIGHT
    s = max(1, min(10, int(stars)))
    return SWIPE_MAX_WEIGHT * (s - 5.5) / 4.5


def _taste_queries(user_id: int, category: str) -> list:
    """The user's taste vector(s) for this category: its cluster centroids,
    or the single average; [] without a profile."""
    from src.database.connection import get_db_session
    from src.database.models import EncryptedTasteVector
    with get_db_session() as db:
        row = (db.query(EncryptedTasteVector.encrypted_blob)
               .filter(EncryptedTasteVector.user_id == user_id,
                       EncryptedTasteVector.media_category == category).first())
    if not row or not row[0]:
        return []
    try:
        blob = json.loads(row[0])
    except (TypeError, ValueError):
        return []
    if not isinstance(blob, dict) or blob.get("version") == 1:
        return []
    clusters = [c.get("embedding") for c in (blob.get("cluster_centroids") or [])
                if isinstance(c, dict) and c.get("embedding")]
    return clusters or ([blob["embedding"]] if blob.get("embedding") else [])


def _exclusions(user_id: int, category: str) -> tuple[set, set]:
    """(doc ids, lower-case titles) the stack leaves out: answered already,
    or played in Plex."""
    from sqlalchemy import func
    from src.database.connection import get_db_session
    from src.database.models import SwipeRating, WatchHistoryEntry as W
    ids, titles = set(), set()
    with get_db_session() as db:
        for doc_id, title in (db.query(SwipeRating.doc_id, SwipeRating.title)
                              .filter(SwipeRating.user_id == user_id,
                                      SwipeRating.category == category)):
            ids.add(doc_id)
            if title:
                titles.add(title.lower())
        for key, name in (db.query(W.plex_item_id, func.lower(func.coalesce(W.series_title, W.title)))
                          .filter(W.user_id == user_id, W.media_type == category).distinct()):
            if key:
                ids.add(str(key))
            if name:
                titles.add(name)
    return ids, titles


def _profile_bits(category: str, doc_ids: list) -> dict:
    """{doc_id: (synopsis, tmdb_id)} from the enriched-profile cache."""
    from src.cache.metadata_cache import MetadataCache
    keys = {d: f"enriched:{category}:{d}" for d in doc_ids}
    with MetadataCache() as cache:
        found = cache.get_cache_many(list(keys.values()))
    out = {}
    for d, k in keys.items():
        prof = (found.get(k) or {}).get("response") or {}
        if isinstance(prof, dict):
            out[d] = ((prof.get("plot_summary") or prof.get("plot") or "")[:600],
                      prof.get("tmdb_id"))
    return out


async def next_cards(user_id: int, category: str, n: int = 8) -> list[dict]:
    """The next ``n`` unanswered, unplayed library titles closest to the
    user's taste, nearest first. [] when the user has no taste vector for
    the category yet."""
    from src.database.runner import run_read
    from src.vector_store.chromadb_wrapper import domain_where, get_chroma_db, run_chroma
    queries = await run_read(_taste_queries, user_id, category)
    if not queries:
        return []
    ex_ids, ex_titles = await run_read(_exclusions, user_id, category)
    res = await run_chroma(get_chroma_db().query, query_embeddings=queries,
                           n_results=_POOL, where=domain_where(category))
    best: dict = {}
    for ids, dists, metas in zip(res.get("ids") or [], res.get("distances") or [],
                                 res.get("metadatas") or []):
        for doc_id, dist, meta in zip(ids, dists, metas):
            title = ((meta or {}).get("title") or "").strip()
            if not title or doc_id in ex_ids or title.lower() in ex_titles:
                continue
            if doc_id not in best or dist < best[doc_id][0]:
                best[doc_id] = (dist, meta)
    cards, titles_seen = [], set()
    for doc_id, (dist, meta) in sorted(best.items(), key=lambda kv: kv[1][0]):
        tl = meta["title"].strip().lower()
        if tl in titles_seen:
            continue                     # one title, several docs (Plex + arr)
        titles_seen.add(tl)
        cards.append({
            "doc_id": doc_id,
            "title": meta["title"].strip(),
            "year": meta.get("year") or None,
            "genres": [g.strip() for g in (meta.get("genres") or "").split(",") if g.strip()][:4],
            "themes": [t.strip() for t in (meta.get("themes") or "").split(",") if t.strip()][:2],
            "fit": round(max(0.0, 1.0 - float(dist)), 3),
        })
        if len(cards) >= n:
            break
    if cards:
        bits = await run_read(_profile_bits, category, [c["doc_id"] for c in cards])
        for c in cards:
            c["synopsis"], c["tmdb_id"] = bits.get(c["doc_id"], ("", None))
    return cards


def save_answer(user_id: int, category: str, doc_id: str, title: str,
                seen: bool, stars: Optional[int]) -> int:
    """Store (or replace) one answer; returns the user's answers in this
    category since the last taste rebuild it triggered."""
    from src.database.connection import get_db_session
    from src.database.models import SwipeRating
    from src.services.app_state import get_state, set_state
    with get_db_session() as db:
        # A new answer replaces the old one as a NEW row: the id then orders
        # the answers for undo. Timestamps cannot - Windows' clock ticks
        # every ~15 ms, and two quick answers shared one.
        (db.query(SwipeRating)
           .filter(SwipeRating.user_id == user_id, SwipeRating.category == category,
                   SwipeRating.doc_id == doc_id).delete(synchronize_session=False))
        db.add(SwipeRating(user_id=user_id, category=category, doc_id=doc_id,
                           title=(title or "")[:512], seen=bool(seen),
                           stars=int(stars) if (seen and stars is not None) else None))
        db.commit()
    key = f"swipe_since_rebuild:{user_id}:{category}"
    count = int(get_state(key) or 0) + 1
    set_state(key, str(count))
    return count


def reset_rebuild_counter(user_id: int, category: str) -> None:
    from src.services.app_state import set_state
    set_state(f"swipe_since_rebuild:{user_id}:{category}", "0")


def undo_last(user_id: int, category: str) -> Optional[dict]:
    """Remove the user's latest answer in this category; returns it."""
    from src.database.connection import get_db_session
    from src.database.models import SwipeRating
    with get_db_session() as db:
        row = (db.query(SwipeRating)
               .filter(SwipeRating.user_id == user_id, SwipeRating.category == category)
               .order_by(SwipeRating.id.desc()).first())
        if row is None:
            return None
        out = {"doc_id": row.doc_id, "title": row.title, "seen": row.seen, "stars": row.stars}
        db.delete(row)
        db.commit()
    return out


def stats(user_id: int) -> dict:
    """{category: {"seen": n, "not_seen": n, "rated": n}}."""
    from sqlalchemy import func
    from src.database.connection import get_db_session
    from src.database.models import SwipeRating as S
    out = {c: {"seen": 0, "not_seen": 0, "rated": 0} for c in SWIPE_CATEGORIES}
    with get_db_session() as db:
        for cat, seen, rated, n in (db.query(S.category, S.seen, S.stars.isnot(None), func.count(S.id))
                                    .filter(S.user_id == user_id)
                                    .group_by(S.category, S.seen, S.stars.isnot(None))):
            if cat not in out:
                continue
            out[cat]["seen" if seen else "not_seen"] += n
            if seen and rated:
                out[cat]["rated"] += n
    return out


def load_swipe_entries(user_id: int, category: Optional[str]) -> list:
    """[(weight, entry)] for the taste rebuild: every seen answer, shaped like
    a watch entry whose enriched profile is cached under the doc id."""
    from src.database.connection import get_db_session
    from src.database.models import SwipeRating as S
    with get_db_session() as db:
        q = db.query(S.category, S.doc_id, S.title, S.seen, S.stars).filter(
            S.user_id == user_id, S.seen == True)  # noqa: E712
        if category:
            q = q.filter(S.category == category)
        rows = q.all()
    out = []
    for cat, doc_id, title, seen, stars in rows:
        w = star_weight(seen, stars)
        if w:
            out.append((w, {"plex_item_id": doc_id, "media_type": cat,
                            "title": title or "", "series_title": None}))
    return out
