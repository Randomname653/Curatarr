"""Because you watched A, B and C: picks that sit near all three.

The library lane ranks owned, unwatched titles by their fit to a member's
whole taste. This lane takes three titles the member watched recently and
keeps only what is close to ALL of them (the lowest of the three
similarities decides), so a pick says something about this run of
viewing, not about the taste average. The curator then writes one line
per pick on what links it to the three, from their themes and plots only.

Vectors: the item embeddings the library lane uses (Chroma, by the arr
doc key), unit-normalised and centred on the category's corpus mean - raw
text embeddings share one cone, so uncentred cosines read ~0.9 between
unrelated films (services/taste_engine._domain_corpus_mean).

The lane is per member and category ("because" in CachedRecommendation,
the anchors in score_factors), and feeds one more playlist per member
(services/playlist_names: "because").
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

ANCHOR_DAYS = 60          # what "recently" means
ANCHORS = 3
PICKS = 5
MIN_FIT = 0.05            # centred cosine: below this a pick is no neighbour of the three


def recent_titles(user_id: int, category: str, now: Optional[datetime] = None,
                  days: int = ANCHOR_DAYS) -> list[str]:
    """The titles (a series counts once) this member finished most in the
    last ``days``, the most recent first among equals. Blocking."""
    from sqlalchemy import func
    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry as W
    now = now or datetime.utcnow()
    key = func.coalesce(W.series_title, W.title) if category in ("show", "anime") else W.title
    with get_db_session() as db:
        rows = (db.query(key, func.count(W.id), func.max(W.viewed_at))
                .filter(W.user_id == user_id, W.media_type == category, W.completed.is_(True),
                        W.viewed_at >= now - timedelta(days=days), key.isnot(None))
                .group_by(key).all())
    rows.sort(key=lambda r: (min(r[1], 5), r[2]), reverse=True)   # a binge counts, but caps at 5
    return [r[0] for r in rows]


def _unit(v) -> Optional[np.ndarray]:
    if v is None:
        return None
    a = np.asarray(v, dtype=float)
    n = float(np.linalg.norm(a))
    return a / n if n > 0 else None


def _centred(v: Optional[np.ndarray], mean: Optional[np.ndarray]) -> Optional[np.ndarray]:
    if v is None:
        return None
    return _unit(v - mean) if mean is not None and mean.shape == v.shape else v


def pick(anchor_vecs: list, candidates: list, *, mean=None, n: int = PICKS,
         min_fit: float = MIN_FIT) -> list[tuple]:
    """[(fit, candidate)] for the ``n`` candidates closest to ALL anchors:
    fit = the lowest centred cosine to any anchor. ``candidates``:
    [(vector, item)]. Pure."""
    anchors = [a for a in (_centred(_unit(v), mean) for v in anchor_vecs) if a is not None]
    if len(anchors) < 2:
        return []
    scored = []
    for vec, item in candidates:
        c = _unit(vec)
        if c is None or c.shape != anchors[0].shape:
            continue
        c = _centred(c, mean)
        fit = min(float(np.dot(a, c)) for a in anchors)
        if fit >= min_fit:
            scored.append((fit, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:n]


def because_factors(anchors: list, fit: float) -> dict:
    """The card's "Why?" in the shape every rec uses
    (recommendations_engine.recommendation_score_factors), plus the anchors
    the page names in the section heading."""
    return {"kind": "recommendation", "lane": "because", "anchors": list(anchors), "factors": [
        {"key": "anchors", "label": "Close to all three of " + ", ".join(anchors), "cosine": round(fit, 4)},
        {"key": "lane", "label": "In your library and still unwatched"},
    ]}


def _doc_id(item: dict) -> str:
    """The Chroma id the library lane reads an item's embedding under."""
    return str(item.get("plex_rating_key") or item.get("tmdb_id") or item.get("title") or "")


def _narrate_prompt(anchors: list, picks: list, lang_directive: str) -> str:
    def line(i):
        bits = [f"{i['title']} ({i.get('year') or '?'})"]
        if i.get("_themes"):
            bits.append("themes: " + ", ".join(i["_themes"][:4]))
        if i.get("_plot"):
            bits.append(str(i["_plot"])[:160])
        return " | ".join(bits)
    return f"""[MODE: BECAUSE YOU WATCHED]
{lang_directive}
The member recently watched these three:
{chr(10).join("- " + line(a) for a in anchors)}

These titles from their own library sit close to all three:
{chr(10).join("- " + line(p) for p in picks)}

For EACH of the second list write one or two sentences on what links it to the three: a shared theme, mood or way of telling, taken from the themes and plots given. Name the link, not a verdict on the member. No facts that are not given above.
JSON ONLY: [{{"title": "Exact Title", "reason": "..."}}]"""


async def because_recs(user_id: int, category: str, unwatched: list, owned: list) -> list:
    """The lane's recs for one member and category, [] when there is no
    run of three recent titles with neighbours in the library. ``unwatched``:
    the library lane's candidate pool; ``owned``: every owned item (the
    anchors are found among them by title)."""
    from src.database.runner import run_read
    from src.services.library_memory import normalize_title
    from src.services.llm_utils import language_directive, parse_llm_json
    from src.services.media_enricher import ensure_verified_data
    from src.services.recommendations_engine import _call_llm, _chroma_get_by_ids
    from src.services.taste_engine import _domain_corpus_mean
    from src.vector_store.chromadb_wrapper import run_chroma

    titles = await run_read(recent_titles, user_id, category)
    if len(titles) < ANCHORS or not unwatched:
        return []
    by_norm = {normalize_title(i.get("title") or ""): i for i in owned}
    anchor_items = []
    for t in titles:
        it = by_norm.get(normalize_title(t))
        if it and it not in anchor_items:
            anchor_items.append(it)
        if len(anchor_items) == ANCHORS * 2:
            break
    ids = [_doc_id(i) for i in anchor_items + unwatched if _doc_id(i)]
    if not ids:
        return []
    embs = await run_chroma(_chroma_get_by_ids, ids)

    def vec(i):
        r = embs.get(_doc_id(i))
        return r.get("embedding") if r else None
    with_vec = [i for i in anchor_items if vec(i) is not None]
    mean = await run_chroma(_domain_corpus_mean, category)
    cands = [(vec(i), i) for i in unwatched if vec(i) is not None]
    chosen, picks = [], []
    # The three most recent with neighbours: a run that shares nothing
    # (a horror film, a sitcom, a documentary) is skipped for the next.
    for start in range(0, max(1, len(with_vec) - ANCHORS + 1)):
        trio = with_vec[start:start + ANCHORS]
        if len(trio) < ANCHORS:
            break
        picks = pick([vec(a) for a in trio], cands, mean=mean)
        if len(picks) >= 2:
            chosen = trio
            break
    if not chosen:
        logger.info("[recs] because/%s: no three recent titles with neighbours in the library", category)
        return []

    async def grounded(i):
        try:
            vd = await ensure_verified_data(i["title"], category, tmdb_id=i.get("tmdb_id"),
                                            tvdb_id=i.get("tvdb_id"), anilist_id=i.get("anilist_id"),
                                            plex_rating_key=i.get("plex_rating_key"), year=i.get("year"),
                                            allow_summarizer=False)
        except Exception as e:  # noqa: BLE001 - a line without themes is still a line; logged
            from src.services.enrichment_state import log_topup_failure
            log_topup_failure(logger, "[recs] because verified-data", i.get("title"), e)
            vd = None
        return {**i, "_themes": [str(t) for t in (vd or {}).get("themes") or []],
                "_plot": (vd or {}).get("plot")}
    import asyncio
    anchors = list(await asyncio.gather(*[grounded(a) for a in chosen]))
    picked = list(await asyncio.gather(*[grounded(p) for _, p in picks]))

    from src.services.user_language import user_language
    raw = await _call_llm(_narrate_prompt(anchors, picked, language_directive(await run_read(user_language, user_id))),
                          max_tokens=900)
    try:
        lines = parse_llm_json(raw or "")
    except Exception as e:  # noqa: BLE001 - no reasons means no lane this run; logged
        logger.warning("[recs] because/%s: the curator's lines were unreadable: %s", category, e)
        return []
    reason_of = {normalize_title(str(x.get("title") or "")): str(x.get("reason") or "").strip()
                 for x in lines if isinstance(x, dict)}
    anchor_names = [a["title"] for a in anchors]
    out = []
    for (fit, item), g in zip(picks, picked):
        reason = reason_of.get(normalize_title(item.get("title") or ""))
        if not reason:
            continue          # no invented line: a pick the curator did not explain is left out
        out.append({"title": item["title"], "reason": reason, "genres": item.get("genres") or "",
                    "year": item.get("year"), "category": category, "lane": "because",
                    "confidence": round(min(1.0, 0.5 + fit), 3),
                    "score_factors": because_factors(anchor_names, fit)})
    logger.info("[recs] because/%s: %d picks near %s", category, len(out), json.dumps(anchor_names))
    return out
