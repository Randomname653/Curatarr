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
    """Candidate class keys for an item, finest → coarsest.

    Crucially the fallback drops TYPE before RESOLUTION: bitrate is driven first
    by resolution (4K ≈ 5× 1080p) and only secondarily by type (a movie ≈ 1.5× a
    show at the same res). So a sparse class like a 4K *show* falls back to
    ``*|4k|*`` (all 4K content) — NOT to ``show|*|*`` (1080p-dominated), which
    would falsely flag it as 5× bloated."""
    res = resolution or "*"
    cod = codec or "*"
    keys = [f"{media_type}|{res}|{cod}", f"{media_type}|{res}|*"]
    if res != "*":
        keys.append(f"*|{res}|*")          # resolution across all types
    # NB: deliberately NO `type|*|*` fallback. Mixing resolutions makes a bloat
    # verdict meaningless (a 4K item vs a 1080p-dominated norm reads as 5× bloat).
    # An item whose resolution has no norm gets None → no verdict (safe).
    return keys


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
    _CROSS_DUP["data"] = None


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
    # No resolution → no reliable class (bloat is resolution-relative); don't judge.
    if not mb_per_min or mb_per_min <= 0 or not media_type or not resolution:
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


def duplicate_report() -> dict:
    """Audit of redundant storage. INTRA-item = one Plex item kept in several
    qualities (versions>1). CROSS-item = the same tmdb/tvdb id across multiple
    separate Plex items. Returns totals + worst offenders for a curator summary
    or a 'what's redundant?' answer."""
    from collections import defaultdict
    intra = []
    by_tmdb, by_tvdb = defaultdict(list), defaultdict(list)
    with get_db_session() as db:
        for r in db.query(MediaTechProfile).all():
            if (r.versions or 1) > 1 and (r.redundant_mb or 0) >= 500:
                intra.append((r.title, r.versions, round((r.redundant_mb or 0) / 1024, 1)))
            if r.tmdb_id:
                by_tmdb[r.tmdb_id].append((r.title, r.size_mb or 0))
            if r.tvdb_id:
                by_tvdb[r.tvdb_id].append((r.title, r.size_mb or 0))
    cross, seen = [], set()
    for grp in (by_tmdb, by_tvdb):
        for _id, rows in grp.items():
            if len(rows) > 1 and rows[0][0] not in seen:
                seen.add(rows[0][0])
                sizes = sorted((s for _, s in rows), reverse=True)
                cross.append((rows[0][0], len(rows), round(sum(sizes[1:]) / 1024, 1)))
    intra.sort(key=lambda x: x[2], reverse=True)
    cross.sort(key=lambda x: x[2], reverse=True)
    intra_gb = round(sum(x[2] for x in intra), 1)
    cross_gb = round(sum(x[2] for x in cross), 1)
    return {"intra_item": intra[:20], "cross_item": cross[:20],
            "intra_redundant_gb": intra_gb, "cross_redundant_gb": cross_gb,
            "total_redundant_gb": round(intra_gb + cross_gb, 1)}


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
            "versions": row.versions or 1, "redundant_mb": row.redundant_mb or 0,
        }


def load_tech_index() -> dict:
    """Build an in-memory {('tmdb', id): profile, ('tvdb', id): profile} map for
    fast batch lookup in the deletion-scoring loop — avoids a DB query per
    candidate. Profiles carry only what the outlier check + size_pts need."""
    idx = {}
    with get_db_session() as db:
        rows = db.query(
            MediaTechProfile.tmdb_id, MediaTechProfile.tvdb_id,
            MediaTechProfile.media_type, MediaTechProfile.resolution,
            MediaTechProfile.codec, MediaTechProfile.mb_per_min,
            MediaTechProfile.size_mb, MediaTechProfile.item_count,
        ).filter(MediaTechProfile.mb_per_min.isnot(None)).all()
    for tmdb_id, tvdb_id, mt, res, cod, mpm, smb, cnt in rows:
        prof = {"media_type": mt, "resolution": res, "codec": cod,
                "mb_per_min": mpm, "size_mb": smb, "item_count": cnt}
        # Index by both int and str so the ARR item dict matches regardless of
        # how its id is typed upstream.
        if tmdb_id:
            idx[("tmdb", tmdb_id)] = prof
            idx[("tmdb", str(tmdb_id))] = prof
        if tvdb_id:
            idx[("tvdb", tvdb_id)] = prof
            idx[("tvdb", str(tvdb_id))] = prof
    return idx


def _tech_profile_by_title(title: str) -> dict:
    if not title:
        return None
    with get_db_session() as db:
        row = db.query(MediaTechProfile).filter(
            MediaTechProfile.title == title).first()
        if not row:
            return None
        return {"media_type": row.media_type, "resolution": row.resolution,
                "codec": row.codec, "mb_per_min": row.mb_per_min,
                "size_mb": row.size_mb, "item_count": row.item_count,
                "versions": row.versions or 1, "redundant_mb": row.redundant_mb or 0}


def _dup_note(prof: dict) -> str:
    """INTRA-item redundant-version clause: one Plex item kept in multiple
    qualities (4K Remux + 1080p Bluray = real hoarding the bitrate check misses)."""
    v = prof.get("versions", 1) or 1
    red = prof.get("redundant_mb", 0) or 0
    if v > 1 and red >= 500:   # >= 0.5 GB of redundant copies is worth mentioning
        return (f" DUPLICATE: keeps {v} versions of this title — ~{red / 1024:.1f} GB "
                f"is redundant; the lower-quality copy can be removed without losing it.")
    return ""


_CROSS_DUP = {"data": None}


def _load_cross_dup() -> dict:
    """{('tmdb', id)/('tvdb', id): (copies, redundant_mb)} for titles that exist as
    MULTIPLE separate library items (same external id, different rating keys)."""
    if _CROSS_DUP["data"] is None:
        from collections import defaultdict
        by = defaultdict(list)
        with get_db_session() as db:
            rows = db.query(MediaTechProfile.tmdb_id, MediaTechProfile.tvdb_id,
                            MediaTechProfile.size_mb).all()
        for tmdb, tvdb, smb in rows:
            if tmdb:
                by[("tmdb", tmdb)].append(smb or 0)
            if tvdb:
                by[("tvdb", tvdb)].append(smb or 0)
        idx = {}
        for k, sizes in by.items():
            if len(sizes) > 1:
                sizes.sort(reverse=True)
                idx[k] = (len(sizes), round(sum(sizes[1:]), 1))   # copies, redundant_mb
        _CROSS_DUP["data"] = idx
    return _CROSS_DUP["data"]


def _cross_dup_note(tmdb_id, tvdb_id) -> str:
    """CROSS-item clause: the same title as several SEPARATE library items."""
    idx = _load_cross_dup()
    hit = (idx.get(("tmdb", _coerce_int(tmdb_id)))
           or idx.get(("tvdb", _coerce_int(tvdb_id))))
    if hit and hit[1] >= 500:
        n, red = hit
        return (f" DUPLICATE: this title exists as {n} separate library copies — "
                f"~{red / 1024:.1f} GB redundant; keep one.")
    return ""


def short_size_tag(*, tmdb_id=None, tvdb_id=None, plex_rating_key=None,
                   title=None) -> str:
    """Compact inline size tag for the chat RAG list, e.g. '[size: 4K, normal]',
    '[size: 1080P, bloated 2.4×]', '[size: 4K, normal ·dup×2]'. '' when no profile."""
    prof = tech_profile_for(tmdb_id=tmdb_id, tvdb_id=tvdb_id,
                            plex_rating_key=plex_rating_key)
    if not prof and title:
        prof = _tech_profile_by_title(title)
    if not prof or not prof.get("mb_per_min"):
        return ""
    o = size_outlier(prof["media_type"], prof["resolution"], prof["codec"],
                     prof["mb_per_min"])
    res = (prof.get("resolution") or "?").upper()
    dup = f" ·dup×{prof.get('versions')}" if (prof.get("versions", 1) or 1) > 1 else ""
    if not o:
        return f"[size: {res}{dup}]"
    if o["verdict"] == "normal":
        return f"[size: {res}, normal{dup}]"
    return f"[size: {res}, {o['verdict']} {o['ratio']}×{dup}]"


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
    dup = _dup_note(prof) + _cross_dup_note(tmdb_id, tvdb_id)
    gb = (prof["size_mb"] or 0) / 1024
    if not o:
        # No class norm yet, but a redundant duplicate is still worth flagging.
        return f"SIZE CONTEXT: {gb:.0f} GB.{dup}" if dup else ""
    # Surface only when actionable: an outlier, a duplicate, or a NORMAL item big
    # enough to pre-empt a blanket size complaint. Small normals stay silent.
    if o["verdict"] == "normal" and gb < 15 and not dup:
        return ""
    res = (prof["resolution"] or "?").upper()
    codec = (prof["codec"] or "?")
    eps = (f", {prof['item_count']} episodes" if prof.get("item_count", 1) > 1 else "")
    base = (f"SIZE CONTEXT: {gb:.0f} GB — {res} {codec}{eps}, "
            f"{o['mb_per_min']:.0f} MB/min vs class median {o['median']:.0f} "
            f"({o['ratio']:.1f}×).")
    if o["verdict"] == "normal":
        base += " This is NORMAL for its class — do NOT treat the size as a flaw."
    elif o["verdict"] == "bloated":
        base += (" This is GENUINELY oversized for its class — size IS a "
                 "legitimate deletion argument here.")
    else:
        base += " This is unusually SMALL for its class (possible low-quality rip)."
    return base + dup
