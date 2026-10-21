"""Swipe: rate library titles near your taste, one card at a time (2026-10-21).

The taste vector only knows what Plex saw. A household member who watches
on Netflix, a title seen at a friend's place, a film from years ago - none of
it reached the profile. The Swipe view walks the titles closest to the
user's taste vector that they have neither played in Plex nor answered yet:
"not seen", or "seen" with stars out of 10.

Each card says why it is there: the taste cluster it sits nearest (its
strongest titles), the genres and themes the user's profile favours, and the
enriched profile's facts (mood, director, cast, runtime, scores). A title the
user loves seeds a short chain of its own nearest neighbours
(``chain_cards``), so the stack follows a fresh enthusiasm.

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


def _taste_profile(user_id: int, category: str) -> dict:
    """The parts of the user's taste blob the stack reads: ``queries`` (its
    cluster centroids, or the single average; [] without a profile),
    ``because`` (each query's strongest titles, [] for the single average)
    and the genre / theme affinities."""
    from src.database.connection import get_db_session
    from src.database.models import EncryptedTasteVector
    empty = {"queries": [], "because": [], "genres": {}, "themes": {}}
    with get_db_session() as db:
        row = (db.query(EncryptedTasteVector.encrypted_blob)
               .filter(EncryptedTasteVector.user_id == user_id,
                       EncryptedTasteVector.media_category == category).first())
    if not row or not row[0]:
        return empty
    try:
        blob = json.loads(row[0])
    except (TypeError, ValueError):
        return empty
    if not isinstance(blob, dict) or blob.get("version") == 1:
        return empty
    clusters = [c for c in (blob.get("cluster_centroids") or [])
                if isinstance(c, dict) and c.get("embedding")]
    if clusters:
        queries = [c["embedding"] for c in clusters]
        because = [[str(t) for t in (c.get("top_titles") or []) if t][:3] for c in clusters]
    elif blob.get("embedding"):
        queries, because = [blob["embedding"]], [[]]
    else:
        return empty

    def _aff(key):
        d = blob.get(key)
        return {str(k).strip().lower(): float(v) for k, v in d.items()} if isinstance(d, dict) else {}
    return {"queries": queries, "because": because,
            "genres": _aff("genre_affinity"), "themes": _aff("theme_affinity")}


def _taste_queries(user_id: int, category: str) -> list:
    """The user's taste vector(s) for this category: its cluster centroids,
    or the single average; [] without a profile."""
    return _taste_profile(user_id, category)["queries"]


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


def _names(v, cap: int = 3) -> list:
    """A profile field that is a list, a comma string or one name → names."""
    if isinstance(v, str):
        v = v.split(",")
    if not isinstance(v, (list, tuple)):
        return []
    out = []
    for x in v:
        x = (x.get("name") if isinstance(x, dict) else x)
        x = str(x or "").strip()
        if x and x.lower() not in ("unknown", "n/a", "none") and x not in out:
            out.append(x)
    return out[:cap]


def _num(v) -> Optional[float]:
    try:
        f = float(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _profile_bits(category: str, doc_ids: list) -> dict:
    """{doc_id: {synopsis, tmdb_id, mood, director, cast, runtime_min,
    seasons, episodes, score, rt_score, content_rating}} from the
    enriched-profile cache; a field the profile lacks is empty."""
    from src.cache.metadata_cache import MetadataCache
    keys = {d: f"enriched:{category}:{d}" for d in doc_ids}
    with MetadataCache() as cache:
        found = cache.get_cache_many(list(keys.values()))
    out = {}
    for d, k in keys.items():
        prof = (found.get(k) or {}).get("response") or {}
        if not isinstance(prof, dict):
            continue
        score = _num(prof.get("rating"))
        out[d] = {
            "synopsis": (prof.get("plot_summary") or prof.get("plot") or "")[:600],
            "tmdb_id": prof.get("tmdb_id"),
            "mood": _names(prof.get("mood"), 3),
            "director": _names(prof.get("director"), 2),
            "cast": _names(prof.get("cast"), 3),
            "runtime_min": int(_num(prof.get("runtime_min")) or 0) or None,
            "seasons": int(_num(prof.get("seasons")) or 0) or None,
            "episodes": int(_num(prof.get("episodes_total")) or 0) or None,
            # a 0-10 score; a stray 0-100 one (AniList) is folded down
            "score": round(score / 10 if score and score > 10 else score, 1) if score else None,
            "rt_score": int(_num(prof.get("rt_score")) or 0) or None,
            "content_rating": str(prof.get("content_rating") or "").strip()[:24] or None,
        }
    return out


def _card(doc_id: str, meta: dict, taste: dict) -> dict:
    """One card from a vector-store hit; ``yours`` marks the tags the user's
    profile favours (affinity >= 0.3 of their strongest)."""
    genres = [g.strip() for g in (meta.get("genres") or "").split(",") if g.strip()][:4]
    themes = [t.strip() for t in (meta.get("themes") or "").split(",") if t.strip()][:3]
    yours = [g for g in genres if taste.get("genres", {}).get(g.lower(), 0) >= 0.3]
    yours += [t for t in themes if taste.get("themes", {}).get(t.lower(), 0) >= 0.3]
    return {"doc_id": doc_id, "title": meta["title"].strip(),
            "year": meta.get("year") or None,
            "genres": genres, "themes": themes, "yours": yours}


async def _attach_bits(category: str, cards: list) -> None:
    from src.database.runner import run_read
    if not cards:
        return
    bits = await run_read(_profile_bits, category, [c["doc_id"] for c in cards])
    for c in cards:
        b = bits.get(c["doc_id"]) or {}
        c["synopsis"] = b.get("synopsis") or ""
        c["tmdb_id"] = b.get("tmdb_id")
        c["details"] = {k: v for k, v in b.items() if k not in ("synopsis", "tmdb_id") and v}


def _walk(res: dict, ex_ids: set, ex_titles: set) -> list:
    """Vector-store hits → [(dist, doc_id, meta, query index)], nearest
    first, one per title, excluded ids / titles left out."""
    rows = [(qi, doc_id, dist, meta)
            for qi, (ids, dists, metas) in enumerate(zip(res.get("ids") or [], res.get("distances") or [],
                                                         res.get("metadatas") or []))
            for doc_id, dist, meta in zip(ids, dists, metas)]
    # an excluded doc's title excludes its twins (one title, Plex + arr docs)
    ex_titles = ex_titles | {((m or {}).get("title") or "").strip().lower()
                             for _, d, _, m in rows if d in ex_ids} - {""}
    best: dict = {}
    for qi, doc_id, dist, meta in rows:
        title = ((meta or {}).get("title") or "").strip()
        if not title or doc_id in ex_ids or title.lower() in ex_titles:
            continue
        if doc_id not in best or dist < best[doc_id][0]:
            best[doc_id] = (dist, meta, qi)
    out, titles_seen = [], set()
    for doc_id, (dist, meta, qi) in sorted(best.items(), key=lambda kv: kv[1][0]):
        tl = meta["title"].strip().lower()
        if tl in titles_seen:
            continue                     # one title, several docs (Plex + arr)
        titles_seen.add(tl)
        out.append((dist, doc_id, meta, qi))
    return out


async def next_cards(user_id: int, category: str, n: int = 8,
                     skip: Optional[set] = None) -> list[dict]:
    """The next ``n`` unanswered, unplayed library titles closest to the
    user's taste, nearest first. [] when the user has no taste vector for
    the category yet. ``skip``: doc ids already waiting in the client's
    stack."""
    from src.database.runner import run_read
    from src.vector_store.chromadb_wrapper import domain_where, get_chroma_db, run_chroma
    taste = await run_read(_taste_profile, user_id, category)
    if not taste["queries"]:
        return []
    ex_ids, ex_titles = await run_read(_exclusions, user_id, category)
    ex_ids |= set(skip or ())
    res = await run_chroma(get_chroma_db().query, query_embeddings=taste["queries"],
                           n_results=_POOL, where=domain_where(category))
    cards = []
    for dist, doc_id, meta, qi in _walk(res, ex_ids, ex_titles)[:n]:
        c = _card(doc_id, meta, taste)
        c["fit"] = round(max(0.0, 1.0 - float(dist)), 3)
        # the cluster this card sits nearest; one average has nothing to name
        c["because"] = taste["because"][qi] if qi < len(taste["because"]) else []
        cards.append(c)
    await _attach_bits(category, cards)
    return cards


async def chain_cards(user_id: int, category: str, seed_doc_id: str, n: int = 3,
                      skip: Optional[set] = None) -> list[dict]:
    """Up to ``n`` unanswered, unplayed titles nearest one the user just
    loved - the stack follows a fresh enthusiasm. [] when the seed has no
    vector."""
    from src.database.runner import run_read
    from src.vector_store.chromadb_wrapper import domain_where, get_chroma_db, run_chroma
    db = get_chroma_db()
    seed = await run_chroma(db.get_by_id, seed_doc_id)
    emb = (seed or {}).get("embedding")
    if emb is None or not len(emb):
        return []
    seed_title = (((seed or {}).get("metadata") or {}).get("title") or "").strip()
    taste = await run_read(_taste_profile, user_id, category)
    ex_ids, ex_titles = await run_read(_exclusions, user_id, category)
    ex_ids |= set(skip or ()) | {seed_doc_id}
    if seed_title:
        ex_titles.add(seed_title.lower())
    res = await run_chroma(db.query, query_embeddings=[[float(x) for x in emb]],
                           n_results=40, where=domain_where(category))
    cards = []
    for dist, doc_id, meta, _ in _walk(res, ex_ids, ex_titles)[:n]:
        c = _card(doc_id, meta, taste)
        c["fit"] = None
        c["because"] = []
        c["chain_from"] = seed_title or None
        c["likeness"] = round(max(0.0, 1.0 - float(dist)), 3)
        cards.append(c)
    await _attach_bits(category, cards)
    return cards


def since_rebuild(user_id: int) -> dict:
    """{category: answers since that category's last swipe rebuild}."""
    from src.services.app_state import get_state
    return {c: int(get_state(f"swipe_since_rebuild:{user_id}:{c}") or 0) for c in SWIPE_CATEGORIES}


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
