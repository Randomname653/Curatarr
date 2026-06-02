"""
Curatarr - Library sorting / config audit (Sonarr anime ↔ TV).

Checks every Sonarr series against the rules for its *true* category and
surfaces anything mis-filed or mis-configured:

  True category is decided by: on the anime-lists **AniDB** mapping, OR an
  **Asian** TMDB origin (JP/CN/KR/TW/HK) → ``anime``; a Western TMDB origin
  → ``tv``; neither (no origin, not on AniDB) → ``uncertain`` (left for the
  admin — usually an old JP OVA the databases just don't list).

Per-category expected settings (from the owner's setup):
  * **anime** → root ``…/AnimeShows``, ``seriesType=anime``, quality profile
    = the one with "anime" in its name (``[Anime] Remux-1080p``). ``Any`` /
    ``Any - 4K`` are NOT anime profiles.
  * **tv**    → root ``…/tv``, ``seriesType`` standard (daily ok), quality
    profile = anything that is NOT the anime profile (``Any`` / ``Any - 4K``).

A series with any field off its expected value becomes a candidate, grouped:
  * ``to_tv``        — Western cartoon in the Anime library (root move)
  * ``to_anime``     — AniDB/Asian title in the TV library (root move)
  * ``fix_settings`` — right library, wrong seriesType and/or quality profile
  * ``uncertain``    — couldn't determine origin (in the anime root) — review

Read-only scan. The actual Sonarr write (PUT root/seriesType/qualityProfile +
``moveFiles``) lives in the router so the write path stays explicit + gated.
"""

import asyncio
import logging
from typing import Optional

import httpx

from src.config import settings
from src.services.anime_mapping import get_anime_mapping
from src.services.app_state import get_state, set_state

logger = logging.getLogger(__name__)

ASIAN_ORIGINS = {"JP", "CN", "KR", "TW", "HK"}


def _poster_url(series: dict) -> Optional[str]:
    for im in series.get("images", []) or []:
        if im.get("coverType") == "poster":
            return im.get("remoteUrl") or im.get("url")
    return None


def _pick_roots(series: list, rootfolders: list) -> tuple[Optional[str], Optional[str]]:
    """(anime_root, tv_root) — by where each seriesType mostly lives, with a
    path-name hint as tie-break."""
    paths = [rf.get("path") for rf in rootfolders if rf.get("path")]
    anime_count = {p: 0 for p in paths}
    std_count = {p: 0 for p in paths}
    for s in series:
        p = s.get("rootFolderPath")
        if p not in anime_count:
            continue
        (anime_count if s.get("seriesType") == "anime" else std_count)[p] += 1
    anime_root = max(paths, key=lambda p: (anime_count[p], "anime" in p.lower()), default=None)
    tv_candidates = [p for p in paths if p != anime_root] or paths
    tv_root = max(tv_candidates, key=lambda p: (std_count[p], "tv" in p.lower() or "show" in p.lower()),
                  default=None)
    return anime_root, tv_root


def _profiles(qps: list) -> tuple[Optional[int], Optional[str], Optional[int], dict, set]:
    """(anime_pid, anime_pname, tv_default_pid, name_by_id, anime_pids).

    Anime profile = any whose name contains "anime". TV default = a profile
    named exactly "any" if present, else the first non-anime profile."""
    by_id = {p["id"]: p.get("name") for p in qps}
    anime_pids = {p["id"] for p in qps if "anime" in (p.get("name") or "").lower()}
    anime_pid = min(anime_pids) if anime_pids else None
    tv_ids = [p["id"] for p in qps if p["id"] not in anime_pids]
    tv_default = next((p["id"] for p in qps if (p.get("name") or "").strip().lower() == "any"),
                      tv_ids[0] if tv_ids else None)
    return anime_pid, by_id.get(anime_pid), tv_default, by_id, anime_pids


async def _tmdb_meta(client: httpx.AsyncClient, tvdb_id: int) -> tuple[list, bool]:
    """(origin_country list, is_animated) for a tvdbId via TMDB /find.

    ``is_animated`` = TMDB carries the Animation genre (id 16). That flag is
    what separates real anime from Japanese **live-action** (dramas,
    live-action adaptations): both are Asian-origin, but only animation
    belongs in the anime library. Cached together in app_state.
    """
    ck = f"tvdb_meta:{tvdb_id}"
    cached = get_state(ck)
    if cached is not None:
        o, _, a = cached.partition("#")
        return [c for c in o.split(",") if c], a == "1"
    origin: list = []
    animated = False
    try:
        r = await client.get(
            f"https://api.themoviedb.org/3/find/{tvdb_id}",
            params={"external_source": "tvdb_id", "api_key": settings.TMDB_API_KEY},
        )
        if r.status_code == 200:
            res = r.json().get("tv_results") or []
            if res:
                origin = res[0].get("origin_country") or []
                animated = 16 in (res[0].get("genre_ids") or [])
    except Exception as e:
        logger.debug("[lib-sort] TMDB meta lookup failed for tvdb %s: %s", tvdb_id, e)
    set_state(ck, f"{','.join(origin)}#{'1' if animated else '0'}")
    return origin, animated


async def scan_misclassified() -> dict:
    sonarr = settings.effective_sonarr_url
    key = settings.SONARR_API_KEY
    if not sonarr or not key:
        return {"error": "Sonarr not configured"}

    mapping = await get_anime_mapping()
    headers = {"X-Api-Key": key}
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            series = (await client.get(f"{sonarr}/api/v3/series", headers=headers)).json()
            rootfolders = (await client.get(f"{sonarr}/api/v3/rootfolder", headers=headers)).json()
            qps = (await client.get(f"{sonarr}/api/v3/qualityprofile", headers=headers)).json()
        except Exception as e:
            logger.warning("[lib-sort] Sonarr fetch failed: %s", e)
            return {"error": "Sonarr unreachable"}

        anime_root, tv_root = _pick_roots(series, rootfolders)
        anime_pid, anime_pname, tv_default_pid, pname, anime_pids = _profiles(qps)

        def _lib(root):
            return "Anime" if root == anime_root else ("TV Shows" if root == tv_root else "?")

        def _card(s, cat, cur_root, cur_type, cur_pid,
                  exp_root, exp_type, exp_pid, issues, origin, on_anidb):
            slug = s.get("titleSlug")
            card = {
                "sonarr_id":   s.get("id"),
                "title":       s.get("title"),
                "tvdb_id":     s.get("tvdbId"),
                "poster":      _poster_url(s),
                "sonarr_link": f"{sonarr.rstrip('/')}/series/{slug}" if slug else None,
                "true_category": cat,
                "origin":      origin,
                "on_anidb":    on_anidb,
                "current": {"library": _lib(cur_root), "root": cur_root,
                            "series_type": cur_type, "profile": pname.get(cur_pid)},
            }
            if cat != "uncertain":
                card["expected"] = {"library": _lib(exp_root), "root": exp_root,
                                    "series_type": exp_type, "profile": pname.get(exp_pid)}
                card["issues"] = issues
                card["fix"] = {"rootFolderPath": exp_root, "seriesType": exp_type,
                               "qualityProfileId": exp_pid, "moveFiles": cur_root != exp_root}
            return card

        to_tv, to_anime, fix_settings, uncertain = [], [], [], []

        for s in series:
            tvdb = s.get("tvdbId")
            on_anidb = bool(tvdb and tvdb in mapping.tvdb_to_anidb)
            stype = s.get("seriesType")
            cur_root = s.get("rootFolderPath")
            cur_pid = s.get("qualityProfileId")

            # ── true category — TMDB origin only where it can change the answer ──
            # AniDB membership alone settles most anime (incl. anime mis-filed in
            # the TV library). We only spend a TMDB /find when origin is the
            # deciding factor: an anime-library title not on AniDB (Western? → TV),
            # or a TV-library title carrying anime-ish settings (a possible
            # mis-file). Everything else in the TV library is TV without a lookup —
            # that's what kept the full sweep from hammering ~1000 series.
            origin, animated = [], False
            if on_anidb:
                cat = "anime"                       # AniDB is anime-only → trust it
            elif cur_root == anime_root:
                origin, animated = (await _tmdb_meta(client, tvdb)) if tvdb else ([], False)
                genres = s.get("genres") or []
                if origin and (set(origin) & ASIAN_ORIGINS) and animated:
                    cat = "anime"                   # Asian + animated → real anime, keep
                elif origin:
                    cat = "tv"                      # Western, or Asian live-action → TV
                elif "Anime" in genres:
                    cat = "anime"                   # no TMDB origin, but TVDB tags it Anime → trust it
                elif "Animation" not in genres:
                    cat = "tv"                      # not animated at all → live-action mis-file → TV
                else:
                    cat = "uncertain"               # animated but origin unknown → JP-anime vs Western, admin decides
            elif tvdb and (stype == "anime" or cur_pid in anime_pids):
                origin, animated = await _tmdb_meta(client, tvdb)
                cat = ("anime" if (origin and (set(origin) & ASIAN_ORIGINS) and animated)
                       else "tv")                   # Asian live-action stays TV (fix profile)
            else:
                cat = "tv"

            if cat == "uncertain":   # only ever set for an anime-library title
                card = _card(s, "uncertain", cur_root, stype, cur_pid,
                             None, None, None, [], origin, on_anidb)
                # No confident origin → let the admin pick the direction. Attach
                # both ready-made fixes so the UI can apply either with one click.
                card["fix_to_anime"] = {"rootFolderPath": anime_root, "seriesType": "anime",
                                        "qualityProfileId": anime_pid,
                                        "moveFiles": cur_root != anime_root}
                card["fix_to_tv"] = {"rootFolderPath": tv_root, "seriesType": "standard",
                                     "qualityProfileId": tv_default_pid,
                                     "moveFiles": cur_root != tv_root}
                uncertain.append(card)
                continue

            # ── expected settings for the true category ────────────────────
            if cat == "anime":
                exp_root, exp_type, exp_pid = anime_root, "anime", anime_pid
            else:
                exp_root = tv_root
                exp_type = stype if stype in ("standard", "daily") else "standard"
                exp_pid = cur_pid if cur_pid not in anime_pids else tv_default_pid

            issues = []
            if anime_root and tv_root and cur_root != exp_root:
                issues.append("root")
            if exp_type and stype != exp_type:
                issues.append("type")
            if exp_pid and cur_pid != exp_pid:
                issues.append("profile")
            if not issues:
                continue

            card = _card(s, cat, cur_root, stype, cur_pid, exp_root, exp_type, exp_pid,
                         issues, origin, on_anidb)
            if "root" in issues:
                (to_tv if cat == "tv" else to_anime).append(card)
            else:
                fix_settings.append(card)

    for lst in (to_tv, to_anime, fix_settings, uncertain):
        lst.sort(key=lambda c: (c["title"] or "").lower())
    return {
        "anime_root": anime_root,
        "tv_root": tv_root,
        "anime_profile": anime_pname,
        "to_tv": to_tv,
        "to_anime": to_anime,
        "fix_settings": fix_settings,
        "uncertain": uncertain,
        "counts": {
            "to_tv": len(to_tv),
            "to_anime": len(to_anime),
            "fix_settings": len(fix_settings),
            "uncertain": len(uncertain),
        },
    }


# ── Stage 2: apply selected reclassifications (the only write path) ──────────

def _recategorize_local(sonarr_id: int, new_cat: str) -> Optional[str]:
    """Re-file one item's category inside Curatarr WITHOUT re-enriching.

    Four cheap, best-effort updates that keep ``(title, category)`` aligned so
    the next enrichment scan SKIPS the item (no TMDB re-fetch, no re-embed, no
    LLM): the two persisted category columns, the MetadataCache profile key,
    and the ChromaDB vector's domain/media_type quarantine metadata. Returns
    the previous category (for the result log).
    """
    key = f"sonarr:{sonarr_id}"
    old_cat = None
    try:
        from src.database.connection import get_db_session
        from src.database.models import EnrichmentStatus, ArrEnrichmentStatus
        with get_db_session() as db:
            es = db.query(EnrichmentStatus).filter(
                EnrichmentStatus.plex_rating_key == key).first()
            if es:
                old_cat = es.media_category
                es.media_category = new_cat       # leave enriched/vector_ready/error intact
            arr = db.query(ArrEnrichmentStatus).filter(
                ArrEnrichmentStatus.service == "sonarr",
                ArrEnrichmentStatus.arr_id == sonarr_id).first()
            if arr:
                old_cat = old_cat or arr.category
                arr.category = new_cat
            db.commit()
    except Exception as e:
        logger.warning("[lib-sort] DB recategorize failed for %s: %s", key, e)

    if not old_cat or old_cat == new_cat:
        return old_cat

    # MetadataCache: copy the already-built profile to the new category key
    # (the skip-filter and the taste/recs lookups are both category-keyed).
    try:
        from src.cache.metadata_cache import MetadataCache
        mc = MetadataCache()
        prof = mc.get_cache(f"enriched:{old_cat}:{key}")
        if prof is not None:
            mc.set_cache(f"enriched:{new_cat}:{key}", prof, days=90)
    except Exception as e:
        logger.warning("[lib-sort] cache re-key failed for %s: %s", key, e)

    # ChromaDB: flip the vector's domain/media_type so gated retrieval files it
    # under the new category. Metadata-only — the embedding itself is untouched.
    try:
        from src.vector_store.chromadb_wrapper import get_chroma_db
        chroma = get_chroma_db()
        doc = chroma.get_by_id(key)
        if doc and doc.get("metadata"):
            meta = dict(doc["metadata"])
            meta["domain"] = new_cat
            meta["media_type"] = new_cat
            chroma.update_metadata(key, meta)
    except Exception as e:
        logger.warning("[lib-sort] vector re-categorize failed for %s: %s", key, e)
    return old_cat


async def apply_reclassify(items: list) -> dict:
    """Execute the selected reclassifications — Sonarr write + local re-file.

    Each item: ``{"sonarr_id": int, "fix": {rootFolderPath, seriesType,
    qualityProfileId, moveFiles}}``. PUTs the series back to Sonarr (``moveFiles``
    queues Sonarr's physical relocation when the root changes), then re-files it
    inside Curatarr via ``_recategorize_local``. Nothing here re-fetches
    metadata, re-embeds, or calls an LLM.
    """
    sonarr = settings.effective_sonarr_url
    key = settings.SONARR_API_KEY
    if not sonarr or not key:
        return {"error": "Sonarr not configured"}
    from src.services.arr_client import classify_sonarr_category
    headers = {"X-Api-Key": key}
    results = []
    async with httpx.AsyncClient(timeout=60) as client:
        for it in items or []:
            sid = it.get("sonarr_id")
            fix = it.get("fix") or {}
            title = None
            try:
                # Title + genres for the result log and classify (genres feed
                # classify_sonarr_category alongside the new seriesType).
                r = await client.get(f"{sonarr}/api/v3/series/{sid}", headers=headers)
                series = r.json() if r.status_code == 200 else {}
                title = series.get("title")
                move = bool(fix.get("moveFiles"))
                # Use the series-EDITOR endpoint, not PUT /series/{id}. A plain
                # PUT updates ``rootFolderPath`` but leaves ``path`` (the
                # authoritative on-disk location) untouched, so files never move.
                # The editor recomputes ``path`` from the new root and performs
                # the physical relocation when ``moveFiles`` is set.
                payload = {"seriesIds": [sid], "moveFiles": move}
                if move and fix.get("rootFolderPath"):
                    payload["rootFolderPath"] = fix["rootFolderPath"]
                if fix.get("seriesType"):       payload["seriesType"]       = fix["seriesType"]
                if fix.get("qualityProfileId"): payload["qualityProfileId"] = fix["qualityProfileId"]
                put = await client.put(f"{sonarr}/api/v3/series/editor",
                                       headers=headers, json=payload)
                if put.status_code not in (200, 202):
                    results.append({"sonarr_id": sid, "title": title, "ok": False,
                                    "error": f"Sonarr editor {put.status_code}: {put.text[:140]}"})
                    continue
                # New category = new seriesType + the (unchanged) genres.
                sim = dict(series)
                sim["seriesType"] = fix.get("seriesType", series.get("seriesType"))
                new_cat = classify_sonarr_category(sim) if series else (
                    "anime" if fix.get("seriesType") == "anime" else "show")
                old_cat = await asyncio.to_thread(_recategorize_local, sid, new_cat)
                results.append({"sonarr_id": sid, "title": title, "ok": True, "moved": move,
                                "old_category": old_cat, "new_category": new_cat})
            except Exception as e:
                logger.warning("[lib-sort] apply failed for %s: %s", sid, e)
                results.append({"sonarr_id": sid, "title": title, "ok": False,
                                "error": str(e)[:200]})
    ok = sum(1 for r in results if r.get("ok"))
    return {"applied": ok, "failed": len(results) - ok, "results": results}
