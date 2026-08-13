"""
Curatarr — UPGRADE curation: the inverse of the downscale flag.

The deletion pipeline has always known how to say "this is bloated, shrink
it" — nothing ever said "this favourite deserves BETTER". Detection is
deliberately LLM-free and conservative (a miss is fine, a false positive
wastes the owner's attention):

  strong love signal        AND   weak file quality
  ─ rewatched movie (≥2 plays)    ─ resolution below 1080p, or
  ─ deep-watched series (≥10 eps) ─ size_outlier() says "lean"
  ─ explicit positive feedback         (≤0.55× the class median)
    with weight ≥ 2 (a Curatarr-
    recommendation verdict)

No arr writes, no auto-regrab — this is a read-only panel; acting on it
stays the owner's click inside the arr itself.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_MIN_EPISODE_PLAYS = 10        # series "deep watch" threshold
_MIN_MOVIE_PLAYS = 2           # movie rewatch threshold


def _judge(profile: dict, love: Optional[dict],
           norms_verdict: Optional[dict]) -> Optional[dict]:
    """Pure decision core: weak file + strong love → candidate row, else None."""
    if not love:
        return None
    weakness = None
    res = (profile.get("resolution") or "").lower()
    if res in ("sd", "720"):
        weakness = f"below 1080p ({res})"
    elif norms_verdict and norms_verdict.get("verdict") == "lean":
        weakness = (f"lean bitrate ({norms_verdict.get('mb_per_min')} vs "
                    f"median {norms_verdict.get('median')} MB/min)")
    if not weakness:
        return None
    return {
        "title": profile.get("title"),
        "category": profile.get("category"),
        "resolution": profile.get("resolution"),
        "size_gb": round(float(profile.get("size_mb") or 0) / 1024, 1),
        "weakness": weakness,
        "love_reason": love.get("reason") or "",
    }


def _collect_love_signals() -> dict:
    """Household-wide love: {"tmdb": {id: reason},
    "title": {(category, norm): reason}}.

    Title keys are CATEGORY-scoped — watch_history's series_title is also
    set on MUSIC rows (the artist), so an unscoped title match credited a
    film named like a band with "109 episodes" of listening (the classic
    The-Devil-Wears-Prada cross-domain collision). Feedback with weight ≥ 2
    comes from the version-0 taste blobs of ALL users — an upgrade benefits
    whoever loves the title."""
    import json as _json

    from sqlalchemy import func

    from src.database.connection import get_db_session
    from src.database.models import EncryptedTasteVector, WatchHistoryEntry
    from src.services.library_memory import normalize_title

    by_tmdb: dict = {}
    by_title: dict = {}
    with get_db_session() as db:
        rows = (db.query(WatchHistoryEntry.tmdb_id,
                         func.count(WatchHistoryEntry.id))
                .filter(WatchHistoryEntry.media_type == "movie",
                        WatchHistoryEntry.tmdb_id.isnot(None))
                .group_by(WatchHistoryEntry.tmdb_id).all())
        for tmdb_id, n in rows:
            if n >= _MIN_MOVIE_PLAYS:
                by_tmdb[tmdb_id] = f"rewatched ({n} plays)"

        rows = (db.query(WatchHistoryEntry.media_type,
                         WatchHistoryEntry.series_title,
                         func.count(WatchHistoryEntry.id))
                .filter(WatchHistoryEntry.series_title.isnot(None),
                        WatchHistoryEntry.media_type.in_(["show", "anime"]))
                .group_by(WatchHistoryEntry.media_type,
                          WatchHistoryEntry.series_title).all())
        for mtype, series, n in rows:
            if n >= _MIN_EPISODE_PLAYS and series:
                by_title[(mtype, normalize_title(series))] = \
                    f"deep watch ({n} episodes)"

        for etv in db.query(EncryptedTasteVector).all():
            try:
                blob = _json.loads(etv.encrypted_blob or "{}")
                if blob.get("version") == 1:
                    continue   # encrypted — no plaintext feedback available
                cat = etv.media_category
                for fb in blob.get("explicit_feedback") or []:
                    if (fb.get("sentiment") == "positive"
                            and float(fb.get("weight") or 1.0) >= 2.0
                            and fb.get("title") and cat):
                        by_title[(cat, normalize_title(fb["title"]))] = (
                            "loved a Curatarr recommendation: "
                            f"{(fb.get('reason') or '')[:80]}")
            except Exception:
                continue
    return {"tmdb": by_tmdb, "title": by_title}


def upgrade_candidates() -> list:
    """Scan the video tech profiles for loved-but-lean titles."""
    from src.database.connection import get_db_session
    from src.database.models import MediaTechProfile
    from src.services.library_memory import normalize_title
    from src.services.size_norms import size_outlier

    love = _collect_love_signals()
    out = []
    with get_db_session() as db:
        rows = db.query(MediaTechProfile).filter(
            MediaTechProfile.media_type.in_(["movie", "show", "anime"])).all()
        rows = [{"title": r.title, "category": r.media_type,
                 "tmdb_id": r.tmdb_id, "resolution": r.resolution,
                 "codec": r.codec, "mb_per_min": r.mb_per_min,
                 "size_mb": r.size_mb, "is_remux": bool(r.is_remux)}
                for r in rows]
    for p in rows:
        hit = (love["tmdb"].get(p["tmdb_id"])
               or love["title"].get((p["category"],
                                     normalize_title(p["title"] or ""))))
        if not hit:
            continue
        verdict = size_outlier(p["category"], p["resolution"], p["codec"],
                               p["mb_per_min"], is_remux=p["is_remux"])
        row = _judge(p, {"reason": hit}, verdict)
        if row:
            out.append(row)
    out.sort(key=lambda r: (r["resolution"] or "") != "sd")
    logger.info("[upgrade] %d candidates from %d loved titles",
                len(out), len(love["tmdb"]) + len(love["title"]))
    return out
