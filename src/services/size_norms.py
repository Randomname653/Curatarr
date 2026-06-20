"""Size-outlier intelligence.

Learn the MB-per-minute (≈ bitrate) distribution per media class
(``media_type × resolution × codec``) from ``MediaTechProfile``, so the curator
can flag GENUINE bloat instead of blanket file size — a 4K film or a series with
many specials is large in absolute GB but *normal* per minute.

``class_key`` encodes the grouping granularity with ``*`` wildcards, so the
outlier detector tries the finest class that has enough samples, then falls back::

    movie|4k|hevc   →   movie|4k|*   →   movie|*|*
"""
import logging
from datetime import datetime

import numpy as np

from src.database.connection import get_db_session
from src.database.models import MediaTechProfile, MediaSizeNorm

logger = logging.getLogger("curatarr")

# Below this a class is too sparse to trust → the item falls back to a coarser
# class (drop codec, then drop resolution). Logged at compute time.
_MIN_SAMPLES = 15


def _class_keys(media_type: str, resolution, codec) -> list:
    """Candidate class keys for an item, finest → coarsest."""
    res = resolution or "*"
    cod = codec or "*"
    return [
        f"{media_type}|{res}|{cod}",
        f"{media_type}|{res}|*",
        f"{media_type}|*|*",
    ]


def compute_size_norms() -> dict:
    """Recompute MB-per-minute norms from MediaTechProfile and persist one
    MediaSizeNorm row per class_key with >= _MIN_SAMPLES samples, at three
    granularities (×codec, ×res, ×type) so the lookup can fall back gracefully."""
    from collections import defaultdict
    buckets = defaultdict(list)   # class_key -> [mb_per_min, ...]
    with get_db_session() as db:
        rows = db.query(
            MediaTechProfile.media_type, MediaTechProfile.resolution,
            MediaTechProfile.codec, MediaTechProfile.mb_per_min,
        ).filter(MediaTechProfile.mb_per_min.isnot(None)).all()
    for mt, res, cod, mpm in rows:
        if not mt or not mpm or mpm <= 0:
            continue
        for ck in _class_keys(mt, res, cod):
            buckets[ck].append(mpm)

    norms, collapsed = [], 0
    for ck, vals in buckets.items():
        if len(vals) < _MIN_SAMPLES:
            collapsed += 1
            continue
        arr = np.asarray(vals, dtype=float)
        norms.append({
            "class_key": ck, "media_type": ck.split("|")[0],
            "median": float(np.median(arr)),
            "p25": float(np.percentile(arr, 25)),
            "p75": float(np.percentile(arr, 75)),
            "p90": float(np.percentile(arr, 90)),
            "std": float(np.std(arr)),
            "sample_count": len(vals),
        })

    with get_db_session() as db:
        db.query(MediaSizeNorm).delete()
        for n in norms:
            db.add(MediaSizeNorm(computed_at=datetime.utcnow(), **n))
        db.commit()
    invalidate_norms_cache()
    logger.info("[size-norms] computed %d class norms (%d sparse classes dropped, "
                "min %d samples)", len(norms), collapsed, _MIN_SAMPLES)
    return {"classes": len(norms), "dropped_sparse": collapsed}


# Norms are a few dozen rows — cache them in-memory, refresh on recompute.
_NORMS_CACHE = {"data": None}


def invalidate_norms_cache() -> None:
    _NORMS_CACHE["data"] = None


def _load_norms() -> dict:
    if _NORMS_CACHE["data"] is None:
        with get_db_session() as db:
            _NORMS_CACHE["data"] = {
                n.class_key: {"median": n.median, "p75": n.p75, "p90": n.p90,
                              "std": n.std, "count": n.sample_count}
                for n in db.query(MediaSizeNorm).all()
            }
    return _NORMS_CACHE["data"]


def size_outlier(media_type: str, resolution, codec, mb_per_min) -> dict:
    """Compare an item's mb_per_min against its class norm. Returns a verdict dict
    or None when there's no profile/norm to compare against (caller falls back to
    today's blanket behaviour)."""
    if not mb_per_min or mb_per_min <= 0 or not media_type:
        return None
    norms = _load_norms()
    chosen = None
    for ck in _class_keys(media_type, resolution, codec):
        if ck in norms:
            chosen = (ck, norms[ck])
            break
    if not chosen:
        return None
    ck, st = chosen
    median = st["median"] or 0
    if median <= 0:
        return None
    ratio = mb_per_min / median
    p90 = st.get("p90") or (median * 1.8)
    if ratio >= 1.5 and mb_per_min >= p90:
        verdict = "bloated"
    elif ratio <= 0.55:
        verdict = "lean"
    else:
        verdict = "normal"
    return {
        "ratio": round(ratio, 2), "verdict": verdict, "class_key": ck,
        "median": round(median, 1), "mb_per_min": round(float(mb_per_min), 1),
        "sample_count": st["count"],
    }


def _coerce_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def tech_profile_for(*, tmdb_id=None, tvdb_id=None, plex_rating_key=None) -> dict:
    """Look up a MediaTechProfile by any available id (plex key → tvdb → tmdb).
    Returns its fields as a detached-safe plain dict, or None."""
    tmdb_id = _coerce_int(tmdb_id)
    tvdb_id = _coerce_int(tvdb_id)
    if not (tmdb_id or tvdb_id or plex_rating_key):
        return None
    with get_db_session() as db:
        row = None
        if plex_rating_key:
            row = db.query(MediaTechProfile).filter(
                MediaTechProfile.plex_rating_key == str(plex_rating_key)).first()
        if not row and tvdb_id:
            row = db.query(MediaTechProfile).filter(
                MediaTechProfile.tvdb_id == tvdb_id).first()
        if not row and tmdb_id:
            row = db.query(MediaTechProfile).filter(
                MediaTechProfile.tmdb_id == tmdb_id).first()
        if not row:
            return None
        return {
            "media_type": row.media_type, "resolution": row.resolution,
            "codec": row.codec, "mb_per_min": row.mb_per_min,
            "size_mb": row.size_mb, "duration_min": row.duration_min,
            "hdr": row.hdr, "item_count": row.item_count, "title": row.title,
        }


def size_context_for(*, tmdb_id=None, tvdb_id=None, plex_rating_key=None) -> str:
    """One-line SIZE CONTEXT string for the curator (pitch / discussion), or "".

    Translates the outlier verdict into plain language the curator weighs:
    NORMAL → don't treat size as a flaw; BLOATED → size is a legitimate argument;
    LEAN → unusually small (possible low-quality rip)."""
    prof = tech_profile_for(tmdb_id=tmdb_id, tvdb_id=tvdb_id,
                            plex_rating_key=plex_rating_key)
    if not prof or not prof.get("mb_per_min"):
        return ""
    o = size_outlier(prof["media_type"], prof["resolution"], prof["codec"],
                     prof["mb_per_min"])
    if not o:
        return ""
    gb = (prof["size_mb"] or 0) / 1024
    res = (prof["resolution"] or "?").upper()
    codec = (prof["codec"] or "?")
    eps = (f", {prof['item_count']} episodes" if prof.get("item_count", 1) > 1 else "")
    base = (f"SIZE CONTEXT: {gb:.0f} GB — {res} {codec}{eps}, "
            f"{o['mb_per_min']:.0f} MB/min vs class median {o['median']:.0f} "
            f"({o['ratio']:.1f}×).")
    if o["verdict"] == "normal":
        return base + " This is NORMAL for its class — do NOT treat the size as a flaw."
    if o["verdict"] == "bloated":
        return base + (" This is GENUINELY oversized for its class — size IS a "
                       "legitimate deletion argument here.")
    return base + " This is unusually SMALL for its class (possible low-quality rip)."
