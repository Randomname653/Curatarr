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
# Remux classes are a smaller population — accept a thinner norm rather than
# leaving every remux unjudged (or worse, judged against encode medians).
_MIN_SAMPLES_REMUX = 8


def _class_keys(media_type: str, resolution, codec, is_remux: bool = False) -> list:
    """Candidate class keys for an item, finest → coarsest.

    Crucially the fallback drops TYPE before RESOLUTION: bitrate is driven first
    by resolution (4K ≈ 5× 1080p) and only secondarily by type (a movie ≈ 1.5× a
    show at the same res). So a sparse class like a 4K *show* falls back to
    ``*|4k|*`` (all 4K content) — NOT to ``show|*|*`` (1080p-dominated), which
    would falsely flag it as 5× bloated.

    REMUX items live in their own class branch (``…|remux``) and NEVER fall
    back into encode classes: an untouched disc stream is legitimately several
    times an encode's bitrate — against the encode median every remux read as
    "3× bloated" and the curator complained about each one. No remux norm →
    None → no verdict (safe)."""
    res = resolution or "*"
    cod = codec or "*"
    if is_remux:
        keys = [f"{media_type}|{res}|{cod}|remux", f"{media_type}|{res}|*|remux"]
        if res != "*":
            keys.append(f"*|{res}|*|remux")
        return keys
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
            MediaTechProfile.is_remux,
        ).filter(MediaTechProfile.mb_per_min.isnot(None)).all()
    for mt, res, cod, mpm, remux in rows:
        if not mt or not mpm or mpm <= 0:
            continue
        # remuxes feed ONLY the remux branch — keeping them out of the encode
        # buckets also stops them skewing the encode medians upward
        for ck in _class_keys(mt, res, cod, is_remux=bool(remux)):
            buckets[ck].append(mpm)

    norms, collapsed = [], 0
    for ck, vals in buckets.items():
        min_n = _MIN_SAMPLES_REMUX if ck.endswith("|remux") else _MIN_SAMPLES
        if len(vals) < min_n:
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


def size_outlier(media_type: str, resolution, codec, mb_per_min,
                 is_remux: bool = False) -> dict:
    """Compare an item's mb_per_min against its class norm. Returns a verdict dict
    or None when there's no profile/norm to compare against (caller falls back to
    today's blanket behaviour). Remux items compare ONLY against remux norms."""
    # No resolution → no reliable class (bloat is resolution-relative); don't judge.
    if not mb_per_min or mb_per_min <= 0 or not media_type or not resolution:
        return None
    norms = _load_norms()
    chosen = None
    for ck in _class_keys(media_type, resolution, codec, is_remux=is_remux):
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
    separate Plex items. Returns totals + worst offenders, each entry carrying
    its provenance (ratingKey, library category, resolution/codec, and for
    cross-item the full copy list) so the UI can name WHAT is duplicated and
    deep-link to WHERE it lives."""
    from collections import defaultdict

    def _row(r) -> dict:
        return {"title": r.title, "plex_rating_key": r.plex_rating_key,
                "media_type": r.media_type, "resolution": r.resolution,
                "codec": r.codec, "is_remux": bool(r.is_remux),
                "size_gb": round((r.size_mb or 0) / 1024, 1)}

    intra = []
    by_tmdb, by_tvdb = defaultdict(list), defaultdict(list)
    with get_db_session() as db:
        for r in db.query(MediaTechProfile).all():
            if (r.versions or 1) > 1 and (r.redundant_mb or 0) >= 500:
                intra.append({**_row(r), "versions": r.versions,
                              "redundant_gb": round((r.redundant_mb or 0) / 1024, 1)})
            if r.tmdb_id:
                by_tmdb[r.tmdb_id].append(_row(r))
            if r.tvdb_id:
                by_tvdb[r.tvdb_id].append(_row(r))
    cross, seen = [], set()
    for grp in (by_tmdb, by_tvdb):
        for _id, rows in grp.items():
            if len(rows) > 1 and rows[0]["title"] not in seen:
                seen.add(rows[0]["title"])
                copies = sorted(rows, key=lambda c: c["size_gb"], reverse=True)
                cross.append({"title": copies[0]["title"], "count": len(copies),
                              "redundant_gb": round(sum(c["size_gb"] for c in copies[1:]), 1),
                              "copies": copies})
    intra.sort(key=lambda x: x["redundant_gb"], reverse=True)
    cross.sort(key=lambda x: x["redundant_gb"], reverse=True)
    intra_gb = round(sum(x["redundant_gb"] for x in intra), 1)
    cross_gb = round(sum(x["redundant_gb"] for x in cross), 1)
    return {"intra_item": intra[:20], "cross_item": cross[:20],
            "intra_redundant_gb": intra_gb, "cross_redundant_gb": cross_gb,
            "total_redundant_gb": round(intra_gb + cross_gb, 1)}


def tech_profile_for(*, tmdb_id=None, tvdb_id=None, plex_rating_key=None,
                     media_type=None) -> dict:
    """Look up a MediaTechProfile by any available id (plex key → tvdb → tmdb).
    Returns its fields as a detached-safe plain dict, or None.

    ``media_type`` scopes the TMDB lookup to its namespace; without it a
    colliding film/series id can return the wrong work's profile.
    """
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
            q = db.query(MediaTechProfile).filter(
                MediaTechProfile.tmdb_id == tmdb_id)
            if media_type:
                types = (["movie"] if media_type == "movie" else ["show", "anime"])
                q = q.filter(MediaTechProfile.media_type.in_(types))
            row = q.first()
        if not row:
            return None
        return {
            "media_type": row.media_type, "resolution": row.resolution,
            "codec": row.codec, "mb_per_min": row.mb_per_min,
            "size_mb": row.size_mb, "duration_min": row.duration_min,
            "hdr": row.hdr, "item_count": row.item_count, "title": row.title,
            "versions": row.versions or 1, "redundant_mb": row.redundant_mb or 0,
            "is_remux": bool(row.is_remux),
            # The REAL Plex ratingKey. Deletion candidates carry an ARR doc id
            # ("radarr:1234") in their own ``plex_rating_key`` field — the
            # enrichment-pipeline key, not a Plex key — so anything that needs
            # to call the Plex API for an item has to resolve it from HERE,
            # by tmdb/tvdb. Additive: existing callers ignore it.
            "plex_rating_key": row.plex_rating_key,
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
            MediaTechProfile.is_remux,
        ).filter(MediaTechProfile.mb_per_min.isnot(None)).all()
    for tmdb_id, tvdb_id, mt, res, cod, mpm, smb, cnt, remux in rows:
        prof = {"media_type": mt, "resolution": res, "codec": cod,
                "mb_per_min": mpm, "size_mb": smb, "item_count": cnt,
                "is_remux": bool(remux)}
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
                "versions": row.versions or 1, "redundant_mb": row.redundant_mb or 0,
                "is_remux": bool(row.is_remux)}


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


def _tmdb_namespace(media_type: str) -> str:
    """TMDB numbers films and series in SEPARATE sequences, so an id alone is
    not an identity — movie 90 is Beverly Hills Cop, series 90 is Air Crash
    Investigation. Anime and shows share the TV sequence."""
    return "movie" if media_type == "movie" else "tv"


def _load_cross_dup() -> dict:
    """{(source, namespace, id): (copies, redundant_mb)} for titles that exist as
    MULTIPLE separate library items (same external id, different rating keys).

    Keyed by TMDB NAMESPACE, not by the bare id: grouping on the number alone
    paired every film with the unrelated series holding the same number and
    reported it as a redundant copy. That reached the owner as "you have two
    separate copies of this series — ~8.7 GB redundant" for a title with
    exactly one copy, where the 8.7 GB was the size of a completely different
    film. 88 such phantom pairs existed in a 10k-item library; scoping by
    namespace removes all of them and keeps all 195 genuine duplicates.
    """
    if _CROSS_DUP["data"] is None:
        from collections import defaultdict
        by = defaultdict(list)
        with get_db_session() as db:
            rows = db.query(MediaTechProfile.tmdb_id, MediaTechProfile.tvdb_id,
                            MediaTechProfile.size_mb,
                            MediaTechProfile.media_type).all()
        for tmdb, tvdb, smb, mtype in rows:
            ns = _tmdb_namespace(mtype)
            if tmdb:
                by[("tmdb", ns, tmdb)].append(smb or 0)
            if tvdb:
                # TVDB is TV-only, but keep the shape uniform.
                by[("tvdb", "tv", tvdb)].append(smb or 0)
        idx = {}
        for k, sizes in by.items():
            if len(sizes) > 1:
                sizes.sort(reverse=True)
                idx[k] = (len(sizes), round(sum(sizes[1:]), 1))   # copies, redundant_mb
        _CROSS_DUP["data"] = idx
    return _CROSS_DUP["data"]


def _cross_dup_note(tmdb_id, tvdb_id, media_type: str = None) -> str:
    """CROSS-item clause: the same title as several SEPARATE library items.

    Without ``media_type`` a TMDB id cannot be resolved to one namespace, so
    the film/series ambiguity is refused rather than guessed — a wrong
    duplicate claim has talked an owner into a deletion before.
    """
    idx = _load_cross_dup()
    hit = None
    if media_type:
        hit = idx.get(("tmdb", _tmdb_namespace(media_type), _coerce_int(tmdb_id)))
    hit = hit or idx.get(("tvdb", "tv", _coerce_int(tvdb_id)))
    if hit and hit[1] >= 500:
        n, red = hit
        return (f" DUPLICATE: this title exists as {n} separate library copies — "
                f"~{red / 1024:.1f} GB redundant; keep one.")
    return ""


def is_bloated_by_title(title: str, media_type=None) -> bool:
    """True when the title's tech profile marks a genuine bitrate outlier.

    Deterministic check for the discussion→action hook: a keep reached in a
    deletion discussion gets its DOWNSCALE flag from THIS, never from the
    curator's prose — the flag the curator announces must be the flag the
    work list actually shows. Missing profile / no class norm → False
    (no flag on unknown data)."""
    # The discussion hook only has the title — the id-keyed lookup
    # (tech_profile_for) has nothing to key on here.
    prof = _tech_profile_by_title(title)
    if not prof or not prof.get("mb_per_min"):
        return False
    o = size_outlier(prof["media_type"], prof["resolution"], prof["codec"],
                     prof["mb_per_min"], is_remux=prof.get("is_remux", False))
    return bool(o and o.get("verdict") == "bloated")


def short_size_tag(*, tmdb_id=None, tvdb_id=None, plex_rating_key=None,
                   title=None, media_type=None) -> str:
    """Compact inline size tag for the chat RAG list, e.g. '[size: 4K, normal]',
    '[size: 1080P, bloated 2.4×]', '[size: 4K, normal ·dup×2]'. '' when no profile."""
    prof = tech_profile_for(tmdb_id=tmdb_id, tvdb_id=tvdb_id,
                            plex_rating_key=plex_rating_key,
                            media_type=media_type)
    if not prof and title:
        prof = _tech_profile_by_title(title)
    if not prof or not prof.get("mb_per_min"):
        return ""
    o = size_outlier(prof["media_type"], prof["resolution"], prof["codec"],
                     prof["mb_per_min"], is_remux=prof.get("is_remux", False))
    res = (prof.get("resolution") or "?").upper()
    if prof.get("is_remux"):
        res += " remux"
    dup = f" ·dup×{prof.get('versions')}" if (prof.get("versions", 1) or 1) > 1 else ""
    if not o:
        return f"[size: {res}{dup}]"
    if o["verdict"] == "normal":
        return f"[size: {res}, normal{dup}]"
    return f"[size: {res}, {o['verdict']} {o['ratio']}×{dup}]"


def size_context_for(*, tmdb_id=None, tvdb_id=None, plex_rating_key=None,
                     media_type=None) -> str:
    """One-line SIZE CONTEXT string for the curator (pitch / discussion), or "".

    Translates the outlier verdict into plain language the curator weighs:
    NORMAL → don't treat size as a flaw; BLOATED → evidence for the DOWNSCALE
    flag (never a deletion argument); LEAN → unusually small (possible
    low-quality rip)."""
    prof = tech_profile_for(tmdb_id=tmdb_id, tvdb_id=tvdb_id,
                            plex_rating_key=plex_rating_key,
                            media_type=media_type)
    if not prof or not prof.get("mb_per_min"):
        return ""
    o = size_outlier(prof["media_type"], prof["resolution"], prof["codec"],
                     prof["mb_per_min"], is_remux=prof.get("is_remux", False))
    dup = _dup_note(prof) + _cross_dup_note(
        tmdb_id, tvdb_id, media_type or prof.get("media_type"))
    gb = (prof["size_mb"] or 0) / 1024
    if not o:
        # No class norm yet, but a redundant duplicate is still worth flagging.
        return f"SIZE CONTEXT: {gb:.0f} GB.{dup}" if dup else ""
    # Surface size to the curator ONLY when it is genuinely actionable as a
    # deletion argument: a BLOATED outlier (a real storage win) or a redundant
    # duplicate. NORMAL is not a flaw, and LEAN (a small / low-bitrate file)
    # wastes NO storage — surfacing those two turned size into a crutch the
    # curator name-dropped in nearly every pitch, and it kept misreading
    # "unusually small" as "inefficient waste of storage" (a 1.7 GB film is not
    # a storage problem). So normal + lean stay SILENT unless a duplicate exists.
    if o["verdict"] != "bloated" and not dup:
        return ""
    # Absolute-waste gate: a high bitrate RATIO on a SMALL file reclaims almost
    # nothing — a 2 GB SD film at 1.7× the SD norm wastes <1 GB. Surfacing that
    # as a "storage waste" is what made no sense (spotted on a 1.66 GB film).
    # Treat bloat as a storage argument ONLY when re-encoding to the class norm
    # would free MEANINGFUL space; otherwise stay silent (the taste/premise
    # mismatch carries the pitch). A real duplicate always counts.
    ratio = o["ratio"] or 1.0
    waste_gb = gb * (1.0 - 1.0 / ratio) if ratio > 0 else 0.0
    if o["verdict"] == "bloated" and waste_gb >= 4.0:
        res = (prof["resolution"] or "?").upper()
        codec = (prof["codec"] or "?") + (" REMUX" if prof.get("is_remux") else "")
        klass = "REMUX class" if prof.get("is_remux") else "class"
        eps = (f", {prof['item_count']} episodes" if prof.get("item_count", 1) > 1 else "")
        return (f"SIZE CONTEXT: {gb:.0f} GB — {res} {codec}{eps}, "
                f"{o['mb_per_min']:.0f} MB/min vs {klass} median {o['median']:.0f} "
                f"({ratio:.1f}×, ~{waste_gb:.0f} GB reclaimable above the {klass} norm) "
                f"— GENUINELY oversized for its class. This argues a DOWNSCALE of "
                f"the FILE on a keep, never deletion of the WORK — not a reason, "
                f"tiebreaker, or closer for a delete, and 'delete now, re-acquire "
                f"leaner later' is not a path to offer: the downscale flag IS the "
                f"lean path.{dup}")
    # Trivial waste, or normal/lean → flag ONLY a duplicate if present; never a
    # size verdict the curator could misframe as "waste" on a small file.
    return f"SIZE CONTEXT: {gb:.0f} GB.{dup}" if dup else ""
