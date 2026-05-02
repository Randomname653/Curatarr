"""
ARR Suite LLM - Plex Watch History Sync + Taste Vector Builder

Pulls full playback history from Plex for every known user,
stores it in watch_history table, then computes taste vectors
and a plain-text LLM summary for each user.
"""

import asyncio
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

import httpx

from src.config import settings
from src.database.connection import get_db_session
from src.services.app_state import get_datetime, set_datetime, get_state, set_state
from src.database.models import (
    User, WatchHistoryEntry, TasteVectorEntry, BatchJob
)

logger = logging.getLogger(__name__)

from src.services.task_monitor import task_monitor


# ─────────────────────────────────────────────────────────────────────────────
# PLEX SYNC
# ─────────────────────────────────────────────────────────────────────────────

async def sync_plex_history(job_id: Optional[int] = None, force: bool = False) -> dict:
    """
    Incremental sync: fetch watched items per Plex library.
    Rate-limited to once per hour unless force=True.
    """
    plex_url = settings.effective_plex_url
    plex_token = settings.effective_plex_token

    if not plex_url or not plex_token:
        return {"error": "PLEX_URL / PLEX_TOKEN not configured"}

    # Rate-limit: skip if synced < 60 min ago and not forced
    last_sync = get_datetime("last_sync_at")
    if not force and last_sync:
        age_min = (datetime.utcnow() - last_sync).total_seconds() / 60
        if age_min < 60:
            return {"skipped": True, "reason": f"Synced {age_min:.0f}m ago", "last_sync": last_sync.isoformat()}

    is_initial = last_sync is None
    logger.info("Starting %s Plex sync", "initial" if is_initial else "incremental")
    _sync_task = task_monitor.create(
        name=f"{'Initial' if is_initial else 'Incremental'} Plex sync",
        category="sync"
    )
    task_monitor.start(_sync_task)

    headers = {
        "Accept": "application/json",
        "X-Plex-Token": plex_token,
        "X-Plex-Client-Identifier": settings.PLEX_CLIENT_ID,
    }

    # Load library config from DB
    from src.database.models import LibraryConfig
    with get_db_session() as db:
        lib_configs = {
            c.plex_section_key: (c.media_category, c.plex_section_type, c.plex_section_title)
            for c in db.query(LibraryConfig).all()
        }

    if not lib_configs:
        return {"error": "No library config. Configure libraries first via the setup wizard."}

    # /library/sections/{key}/all?viewCount>>=0 returns ALL ever-watched items.
    # For incremental syncs we add lastViewedAt>>=<last_sync_ts> so Plex
    # only returns items that were watched since our last sync.
    PLEX_TYPE_NUM = {"movie": "1", "show": "4", "anime": "4", "music": "10"}

    # Build the timestamp filter for incremental syncs
    last_sync_ts = None
    if not is_initial and last_sync:
        last_sync_ts = int(last_sync.timestamp())
        logger.info("Incremental sync — filtering items with lastViewedAt >= %d (%s)",
                    last_sync_ts, last_sync.isoformat())

    all_entries: list = []

    async with httpx.AsyncClient(timeout=60) as client:
        for sec_key, (category, plex_sec_type, sec_title) in lib_configs.items():
            if category == "ignore":
                continue
            type_num = PLEX_TYPE_NUM.get(category)
            if not type_num:
                continue

            params = {"type": type_num, "viewCount>>": "0", "includeGuids": "1"}
            if last_sync_ts:
                # Only items whose lastViewedAt is >= our last sync timestamp
                params["lastViewedAt>>"] = str(last_sync_ts - 60)  # 60s overlap for safety

            resp = await client.get(
                f"{plex_url}/library/sections/{sec_key}/all",
                headers=headers,
                params=params,
            )
            if resp.status_code == 404:
                # Library no longer exists in Plex — remove from config
                logger.warning("Library %r (key=%s) returned 404 — removing from config", sec_title, sec_key)
                try:
                    from src.database.models import LibraryConfig as LC
                    with get_db_session() as _db:
                        dead = _db.query(LC).filter(LC.plex_section_key == sec_key).first()
                        if dead:
                            _db.delete(dead)
                            _db.commit()
                except Exception as _e:
                    logger.debug("Could not remove dead library config: %s", _e)
                continue
            if resp.status_code != 200:
                logger.warning("Library fetch failed for %r: HTTP %s", sec_title, resp.status_code)
                continue

            items = resp.json().get("MediaContainer", {}).get("Metadata", []) or []
            logger.info("Library %r (cat=%s): %d %s items",
                        sec_title, category, len(items),
                        "new/updated" if last_sync_ts else "watched")
            task_monitor.log(_sync_task, f"{sec_title}: {len(items)} items")

            for item in items:
                item["_category"] = category
                item["_library_title"] = sec_title
                for guid in (item.get("Guid") or []):
                    gid = guid.get("id", "")
                    if gid.startswith("tmdb://"):
                        item["_tmdb_id"] = gid[7:]
                    elif gid.startswith("tvdb://"):
                        item["_tvdb_id"] = gid[7:]
                    elif gid.startswith("imdb://"):
                        item["_imdb_id"] = gid[7:]
                    elif gid.startswith("hama://anidb-"):
                        item["_anidb_id"] = gid[13:]
                    elif gid.startswith("anilist://"):
                        item["_anilist_id"] = gid[10:]
                    elif gid.startswith("mal://"):
                        item["_mal_id"] = gid[6:]

            all_entries.extend(items)

    logger.info("Total %s items: %d",
                "new/updated" if last_sync_ts else "watched",
                len(all_entries))

    # Also fetch in-progress items (viewOffset>0, viewCount=0)
    # These give us real completion data and act as negative/ambiguous signals
    PLEX_TYPE_INPROGRESS = {"movie": "1", "show": "4", "anime": "4"}
    in_progress_entries = []

    async with httpx.AsyncClient(timeout=60) as client:
        for sec_key, (category, plex_sec_type, sec_title) in lib_configs.items():
            if category not in PLEX_TYPE_INPROGRESS or category == "ignore":
                continue
            type_num = PLEX_TYPE_INPROGRESS[category]

            resp = await client.get(
                f"{plex_url}/library/sections/{sec_key}/all",
                headers=headers,
                params={"type": type_num, "viewOffset>>": "1",
                        "viewCount": "0", "includeGuids": "1"},
            )
            if resp.status_code != 200:
                continue
            items = resp.json().get("MediaContainer", {}).get("Metadata", []) or []
            logger.info("Library %r in-progress: %d items", sec_title, len(items))

            for item in items:
                item["_category"] = category
                item["_library_title"] = sec_title
                item["_in_progress"] = True
                for guid in (item.get("Guid") or []):
                    gid = guid.get("id", "")
                    if gid.startswith("tmdb://"):
                        item["_tmdb_id"] = gid[7:]

            in_progress_entries.extend(items)

    logger.info("In-progress items: %d", len(in_progress_entries))
    all_entries.extend(in_progress_entries)


    if not all_entries:
        return {"synced": 0, "message": "No watched items found. Have you watched anything on Plex?"}

    # Genres come inline from /library/sections/all — no separate metadata fetch needed.
    # We keep a small fallback cache for items missing Genre tags.
    unique_rating_keys = list({e.get("ratingKey") for e in all_entries
                               if e.get("ratingKey") and not e.get("Genre")})
    metadata_cache: dict = {}
    cap = min(200, len(unique_rating_keys))
    if cap > 0:
        logger.info("Fetching genres for %d items missing inline Genre tags (capped at %d)", len(unique_rating_keys), cap)

    import xml.etree.ElementTree as ET

    async def fetch_one_meta(client, rk):
        try:
            r = await client.get(
                f"{plex_url}/library/metadata/{rk}",
                headers={**headers, "Accept": "application/xml"},
            )
            if r.status_code == 200:
                root = ET.fromstring(r.text)
                item = root.find(".//Video") or root.find(".//Track") or root.find(".//Directory")
                if item is not None:
                    genres = [g.get("tag", "") for g in item.findall("Genre")]
                    return rk, {"genres": genres, "tmdb_id": None}
        except Exception:
            pass
        return rk, None

    sem = asyncio.Semaphore(20)  # 20 parallel requests

    async def fetch_with_sem(client, rk):
        async with sem:
            return await fetch_one_meta(client, rk)

    async with httpx.AsyncClient(timeout=10) as client:
        tasks = [fetch_with_sem(client, rk) for rk in unique_rating_keys[:cap]]
        results = await asyncio.gather(*tasks)
        for rk, meta in results:
            if meta:
                metadata_cache[rk] = meta

    logger.info("Metadata cache built: %d/%d items enriched with genres", len(metadata_cache), cap)

    # Build local accountID -> DB user map BEFORE opening DB session
    # Fetch /accounts outside of sync context to avoid async-in-sync issues
    local_account_map: dict = {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            acc_resp = await client.get(
                f"{plex_url}/accounts",
                headers={**headers, "Accept": "application/json"},
            )
        if acc_resp.status_code == 200:
            acc_data = acc_resp.json()
            accounts = (
                acc_data.get("MediaContainer", {}).get("Account")
                or acc_data.get("MediaContainer", {}).get("Accounts")
                or []
            )
            if isinstance(accounts, dict):
                accounts = [accounts]
            # Build a name->local_id lookup first
            for acc in accounts:
                local_id = str(acc.get("id", ""))
                name = (acc.get("name") or acc.get("title") or "").strip()
                local_account_map[local_id] = name  # store name for now, resolve to User below
            logger.info("Plex /accounts returned %d entries: %s",
                        len(local_account_map),
                        {k: v for k, v in local_account_map.items()})
    except Exception as e:
        logger.warning("Could not fetch /accounts: %s", e)

    # Write to DB
    synced = 0
    skipped = 0

    with get_db_session() as db:
        users = db.query(User).all()

        # Primary map: plex.tv global ID -> local user (set on OAuth login)
        plex_id_to_user = {u.plex_user_id: u for u in users}

        # Resolve name strings in local_account_map to actual User objects
        # accountID=1 is always the local server owner → map to admin
        admin_user = next((u for u in users if u.is_admin), users[0] if users else None)
        single_user = users[0] if len(users) == 1 else None
        resolved_account_map: dict = {}
        for local_id, name in local_account_map.items():
            if local_id == "1":
                # accountID=1 is always the Plex server owner
                resolved_account_map[local_id] = admin_user
            elif name:
                matched = next(
                    (u for u in users if u.plex_username.lower() == name.lower()),
                    None
                )
                if matched:
                    resolved_account_map[local_id] = matched
            # High numeric IDs (like 216511115) are plex.tv managed users
            # — they'll be caught by plex_id_to_user instead

        logger.info("Resolved account map: %s",
                    {k: v.plex_username for k, v in resolved_account_map.items()})
        logger.info("DB users: %s", [(u.id, u.plex_user_id, u.plex_username) for u in users])
        logger.info("local_account_map (raw names): %s", local_account_map)

        # Get existing entries to deduplicate
        existing = set()
        for row in db.query(
            WatchHistoryEntry.user_id,
            WatchHistoryEntry.plex_item_id,
            WatchHistoryEntry.viewed_at,
        ).all():
            existing.add((row.user_id, row.plex_item_id, row.viewed_at))

        # Local cache to avoid duplicate MediaIdentity queries within same sync
        local_mi_cache = {}

        for entry in all_entries:
            rating_key = str(entry.get("ratingKey", ""))
            if not rating_key:
                continue

            # /library/sections/all uses lastViewedAt (unix ts), not viewedAt
            viewed_at_ts = entry.get("lastViewedAt") or entry.get("viewedAt")
            if not viewed_at_ts:
                continue

            viewed_at = datetime.fromtimestamp(int(viewed_at_ts))

            user = admin_user
            if not user:
                continue

            is_in_progress = entry.get("_in_progress", False)
            duration_ms = entry.get("duration")
            view_offset_ms = entry.get("viewOffset", 0) if is_in_progress else duration_ms

            # Completion rate
            if is_in_progress and duration_ms and duration_ms > 0:
                completion_pct = min(1.0, (view_offset_ms or 0) / duration_ms)
                completed = completion_pct >= 0.9
            else:
                completion_pct = 1.0
                completed = True

            dedup_key = (user.id, rating_key, viewed_at)
            if dedup_key in existing:
                # Update completion if this is in-progress and we have better data
                if is_in_progress and view_offset_ms:
                    # Find and update existing entry
                    for row in db.query(WatchHistoryEntry).filter(
                        WatchHistoryEntry.user_id == user.id,
                        WatchHistoryEntry.plex_item_id == rating_key,
                    ).all():
                        row.view_offset_ms = int(view_offset_ms)
                        row.completed = completed
                skipped += 1
                continue

            genres_list = [g.get("tag", "") for g in (entry.get("Genre") or [])]
            genres_str = ",".join(g for g in genres_list if g)
            if not genres_str:
                meta = metadata_cache.get(rating_key, {})
                genres_str = ",".join(meta.get("genres", []))

            media_type = entry.get("_category", "other")
            series_title = entry.get("grandparentTitle")
            season = entry.get("parentIndex")
            episode_num = entry.get("index")
            # For episodes: use grandparentRatingKey to get Series-level IDs
            # Episode-level TMDB IDs are episode-specific and not usable for enrichment
            grandparent_key = entry.get("grandparentRatingKey")
            is_episode = media_type in ("show", "anime") and grandparent_key

            # For series/anime: prefer grandparent (series) IDs over episode IDs
            # Episode TMDB IDs like 4096836 are episode-specific, not series IDs
            if is_episode:
                # Use cached series IDs if already resolved
                from src.database.models import MediaIdentity as _MI
                _series_mi = db.query(_MI).filter(
                    _MI.plex_rating_key == str(grandparent_key)
                ).first()
                if _series_mi:
                    tmdb_id    = _series_mi.tmdb_id
                    tvdb_id    = _series_mi.tvdb_id
                    anilist_id = _series_mi.anilist_id
                    anidb_id   = _series_mi.anidb_id
                    imdb_id    = _series_mi.imdb_id
                    mal_id     = _series_mi.mal_id
                else:
                    # Series not yet resolved — store episode IDs, mark for resolution
                    tmdb_id    = None  # Don't store episode TMDB ID as series TMDB ID
                    tvdb_id    = entry.get("_tvdb_id")
                    imdb_id    = entry.get("_imdb_id")
                    anidb_id   = entry.get("_anidb_id")
                    anilist_id = entry.get("_anilist_id")
                    mal_id     = entry.get("_mal_id")
            else:
                tmdb_id    = entry.get("_tmdb_id")
                tvdb_id    = entry.get("_tvdb_id")
                imdb_id    = entry.get("_imdb_id")
                anidb_id   = entry.get("_anidb_id")
                anilist_id = entry.get("_anilist_id")
                mal_id     = entry.get("_mal_id")

            # Upsert MediaIdentity — store series key for episodes
            from src.database.models import MediaIdentity as _MI
            identity_key = str(grandparent_key) if is_episode else rating_key

            # Check local cache first to avoid repeated DB queries for same series
            _mi = local_mi_cache.get(identity_key)
            if not _mi:
                _mi = db.query(_MI).filter(_MI.plex_rating_key == identity_key).first()
                if not _mi:
                    _mi = _MI(
                        plex_rating_key=identity_key,
                        media_type=media_type,
                        title=series_title or entry.get("title", ""),
                        year=entry.get("year"),
                    )
                    db.add(_mi)
                local_mi_cache[identity_key] = _mi
            if tmdb_id and str(tmdb_id).isdigit():       _mi.tmdb_id    = int(tmdb_id)
            if tvdb_id and str(tvdb_id).isdigit():       _mi.tvdb_id    = int(tvdb_id)
            if imdb_id:                                  _mi.imdb_id    = str(imdb_id)
            if anidb_id and str(anidb_id).isdigit():     _mi.anidb_id   = int(anidb_id)
            if anilist_id and str(anilist_id).isdigit(): _mi.anilist_id = int(anilist_id)
            if mal_id and str(mal_id).isdigit():         _mi.mal_id     = int(mal_id)

            db.add(WatchHistoryEntry(
                user_id=user.id,
                plex_user_id=str(user.plex_user_id),
                plex_item_id=rating_key,
                title=entry.get("title", ""),
                media_type=media_type,
                series_title=series_title,
                season=int(season) if season else None,
                episode=int(episode_num) if episode_num else None,
                viewed_at=viewed_at,
                duration_ms=int(duration_ms) if duration_ms else None,
                view_offset_ms=int(view_offset_ms) if view_offset_ms else None,
                completed=completed,
                genres=genres_str,
                tmdb_id=int(tmdb_id) if tmdb_id and str(tmdb_id).isdigit() else None,
            ))
            existing.add(dedup_key)
            synced += 1

        db.commit()

    logger.info("Plex sync done: %d new entries, %d skipped", synced, skipped)
    set_datetime("last_sync_at", datetime.utcnow())
    task_monitor.done(_sync_task,
        f"Done: {synced} new entries, {skipped} skipped")

    # Delegate taste vector recompute to taste_engine (canonical implementation)
    if synced > 0:
        from src.services.taste_engine import compute_all_taste_vectors
        with get_db_session() as db:
            active_users = db.query(User).filter(User.is_active == True).all()
            user_ids = [u.id for u in active_users]
        for uid in user_ids:
            await compute_all_taste_vectors(uid)
    else:
        logger.info("No new entries — skipping taste vector recompute")

    return {"synced": synced, "skipped": skipped, "total_fetched": len(all_entries)}


async def _post_sync_verification(user_ids: list):
    """After sync, trigger verification questions for users with new data."""
    await asyncio.sleep(10)  # let taste vectors settle first
    try:
        from src.services.verification_session import start_verification_session
        for uid in user_ids:
            await start_verification_session(uid)
    except Exception as e:
        logger.debug("Post-sync verification failed: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# TASTE VECTOR COMPUTATION
# ─────────────────────────────────────────────────────────────────────────────

MEDIA_TYPE_GROUPS = {
    "music":  {"track", "album"},
    "movie":  {"movie"},
    "show":   {"episode", "season", "show"},
    "anime":  {"anime"},
}

def _classify_type(media_type: str, library_type: str = "", library_title: str = "") -> str:
    """Classify a Plex entry into music/movie/show/anime using library type first."""
    lib = library_type.lower()
    title = library_title.lower()
    mt = (media_type or "").lower()

    # Library type is most reliable
    if lib == "artist" or mt == "track":
        return "music"
    if lib == "movie" or mt == "movie":
        return "movie"
    if lib == "show":
        # Anime libraries often have 'anime' in the title
        if "anime" in title:
            return "anime"
        return "show"

    # Fallback on media_type alone
    if mt == "track":
        return "music"
    if mt in ("episode", "season"):
        return "show"
    return "other"


async def _compute_taste_vector(user_id: int, db) -> Optional[TasteVectorEntry]:
    """
    Compute per-type taste vectors from ALL watch history (no cap).
    Genres/themes come from enriched metadata cache where available,
    falling back to Plex genre tags.
    Summary text is generated by Ollama (small model) per category.
    """
    entries = (
        db.query(WatchHistoryEntry)
        .filter(WatchHistoryEntry.user_id == user_id)
        .order_by(WatchHistoryEntry.viewed_at.desc())
        .all()  # no limit — process everything
    )
    logger.info("Computing taste vectors from %d history entries for user %d",
                len(entries), user_id)

    if not entries:
        return None

    from src.cache.metadata_cache import MetadataCache
    cache = MetadataCache()

    def normalize(counter: Counter, top_n: int = 30) -> dict:
        if not counter:
            return {}
        top = counter.most_common(top_n)
        max_val = top[0][1] if top else 1
        return {k: round(v / max_val, 3) for k, v in top}

    # Aggregate per media type
    type_data: dict = {}  # type -> {genres, themes, moods, titles, completions}
    for e in entries:
        mtype = e.media_type or "other"  # already classified during sync
        if mtype not in type_data:
            type_data[mtype] = {
                "genres": Counter(), "themes": Counter(),
                "moods": Counter(), "titles": Counter(),
                "completions": [],
            }
        td = type_data[mtype]

        # Completion
        if e.duration_ms and e.duration_ms > 0 and e.view_offset_ms:
            rate = min(1.0, e.view_offset_ms / e.duration_ms)
        else:
            rate = 1.0 if e.completed else 0.5
        td["completions"].append(rate)

        # Try enriched profile from cache
        cache_key = f"enriched:{mtype}:{e.plex_item_id}"
        enriched = cache.get_cache(cache_key)
        profile = enriched["response"] if enriched else None

        if profile:
            for g in profile.get("genres", []):
                td["genres"][g] += 1
            for t in profile.get("themes", []):
                td["themes"][t] += 1
            for m in profile.get("mood", []):
                td["moods"][m] += 1
        else:
            if e.genres:
                for g in e.genres.split(","):
                    g = g.strip()
                    if g:
                        td["genres"][g] += 1

        label = e.series_title or e.title
        if label:
            td["titles"][label] += 1

    cache.close()

    # Build per-type summaries
    type_summaries = {}
    for mtype, td in type_data.items():
        completions = td["completions"]
        type_summaries[mtype] = {
            "genre_affinity": normalize(td["genres"], 20),
            "themes": normalize(td["themes"], 15),
            "moods": normalize(td["moods"], 10),
            "top_titles": list(normalize(td["titles"], 30).keys()),
            "watch_count": len(completions),
            "avg_completion": round(sum(completions) / len(completions), 3) if completions else 0.0,
        }

    # Generate per-type LLM summaries and combine
    summary_parts = []
    total_count = sum(v["watch_count"] for v in type_summaries.values())
    overall_completion = round(
        sum(v["avg_completion"] * v["watch_count"] for v in type_summaries.values()) / max(total_count, 1), 3
    )

    for mtype, ts in type_summaries.items():
        if ts["watch_count"] < 5:
            continue  # skip tiny categories
        part = await _generate_taste_summary(
            user_id=user_id,
            media_type=mtype,
            genre_affinity=ts["genre_affinity"],
            themes=list(ts["themes"].keys())[:8],
            moods=list(ts["moods"].keys())[:5],
            top_titles=ts["top_titles"][:15],
            watch_count=ts["watch_count"],
            avg_completion=ts["avg_completion"],
        )
        summary_parts.append(f"[{mtype.upper()}] {part}")

    summary_text = "\n\n".join(summary_parts)

    # Store: genre_affinity = full per-type JSON, actor_affinity = themes, director_affinity = moods
    existing = db.query(TasteVectorEntry).filter(TasteVectorEntry.user_id == user_id).first()
    data = dict(
        genre_affinity=json.dumps(type_summaries),   # full per-type data
        actor_affinity="{}",
        director_affinity="{}",
        top_titles=json.dumps({k: v["top_titles"][:20] for k, v in type_summaries.items()}),
        watch_count=total_count,
        avg_completion=overall_completion,
        computed_at=datetime.utcnow(),
        summary_text=summary_text,
    )
    if existing:
        for k, v in data.items():
            setattr(existing, k, v)
    else:
        db.add(TasteVectorEntry(user_id=user_id, **data))

    return existing


TYPE_LABELS = {
    "music": "music (artists, albums, tracks)",
    "movie": "movies",
    "show":  "TV series",
    "anime": "anime",
    "other": "other media",
}

async def _generate_taste_summary(
    user_id: int,
    genre_affinity: dict,
    top_titles: list,
    watch_count: int,
    avg_completion: float,
    themes: list = None,
    moods: list = None,
    media_type: str = "movie",
) -> str:
    """Ask Ollama to write a concise per-type taste summary using the CURATOR model."""
    top_genres = sorted(genre_affinity.items(), key=lambda x: x[1], reverse=True)[:8]
    genres_str = ", ".join(g for g, _ in top_genres)
    titles_str = ", ".join(top_titles[:12])
    themes_str = ", ".join(themes or [])
    moods_str  = ", ".join(moods or [])
    type_label = TYPE_LABELS.get(media_type, media_type)

    noun = "tracks/artists" if media_type == "music" else "titles/series" if media_type in ("show","anime") else "films"

    # Der Prompt wird für das große Curator-Modell geschärft
    prompt = f"""[MODE: TASTE SUMMARY]
Write 2-3 punchy sentences describing this person's {type_label} taste. You are an uncompromising, elite media curator. 

Data ({type_label}, {watch_count} {noun}):
- Top genres: {genres_str or 'N/A'}
- Themes: {themes_str or 'N/A'}
- Moods: {moods_str or 'N/A'}
- Top {noun}: {titles_str}

Rules:
- Second person ("You tend to...")
- Be brutally honest, highly opinionated, and observant. Do not use generic PR language.
- Explicitly highlight if the user prefers "polite monsters", escalating visual madness (Trigger style), or uncompromising narratives. 
- Acknowledge if the user avoids cheap "theatrical suffering" or watered-down kitsch.
- Connect 2-3 specific titles to these themes to prove your analysis.
- Do NOT sound like a bland summary algorithm."""

    try:
        ollama_url = settings.effective_ollama
        
        # WICHTIG: Wir wechseln hier auf das GROSSE Curator-Modell!
        for model in [settings.CURATOR_MODEL, settings.BASE_CURATOR_MODEL]:
            if not model:
                continue
            async with httpx.AsyncClient(timeout=120) as client: # Erhöhter Timeout für das große Modell
                resp = await client.post(
                    f"{ollama_url}/api/chat",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": False,
                        "options": {"temperature": 0.8, "num_predict": 250},
                    },
                )
            if resp.status_code == 200:
                return resp.json().get("message", {}).get("content", "").strip()
            if resp.status_code == 404:
                logger.info("Model %r not found, trying next fallback...", model)
                continue
            logger.warning("Taste summary: Ollama returned %s for model %r", resp.status_code, model)
            break
    except httpx.TimeoutException:
        logger.warning("Taste summary timed out after 120s — using rule-based fallback")
    except Exception as e:
        logger.warning("Taste summary generation failed: %s", e)

    # Fallback without LLM
    return (
        f"You've watched {watch_count} items ({avg_completion:.0%} avg completion). "
        f"Top genres: {genres_str}. "
        + (f"Recurring themes: {themes_str}. " if themes_str else "")
        + f"Frequently watched: {titles_str}."
    )


async def get_user_taste_context(user_id: int, query: str = "") -> str:
    """
    Return taste context for the LLM system prompt.
    When a query is provided, injects only the relevant categories
    to save tokens and reduce noise.
    """
    with get_db_session() as db:
        tv = db.query(TasteVectorEntry).filter(TasteVectorEntry.user_id == user_id).first()
        if not tv:
            return ""

        type_data = json.loads(tv.genre_affinity or "{}")
        top_titles_by_type = json.loads(tv.top_titles or "{}")
        summary_text = tv.summary_text or ""
        watch_count = tv.watch_count or 0
        computed_at = tv.computed_at

    # Detect which categories are relevant to the query
    relevant_cats = _detect_relevant_categories(query, type_data)

    type_labels = {
        "music": "🎵 Music", "movie": "🎬 Movies",
        "show": "📺 TV Series", "anime": "⛩️ Anime",
    }

    lines = [
        f"USER TASTE PROFILE — {watch_count} items total, "
        f"updated {computed_at.strftime('%Y-%m-%d') if computed_at else 'unknown'}",
        "",
    ]

    for mtype, ts in type_data.items():
        if mtype not in relevant_cats:
            continue
        if not isinstance(ts, dict) or ts.get("watch_count", 0) < 5:
            continue

        label = type_labels.get(mtype, mtype.upper())
        top_genres = sorted(
            (ts.get("genre_affinity") or {}).items(),
            key=lambda x: x[1], reverse=True
        )[:6]
        genres_str = ", ".join(g for g, _ in top_genres)
        top_themes = list((ts.get("themes") or {}).keys())[:5]
        titles = (top_titles_by_type.get(mtype) or ts.get("top_titles") or [])[:8]

        completion = ts.get("avg_completion", 0)
        lines.append(f"{label} ({ts['watch_count']} items, {completion:.0%} avg completion):")
        if genres_str:
            lines.append(f"  Genres/styles: {genres_str}")
        if top_themes:
            lines.append(f"  Themes: {', '.join(top_themes)}")
        if titles:
            lines.append(f"  Top titles: {', '.join(titles)}")
        lines.append("")

    # Add per-category LLM summaries for relevant cats only
    if summary_text:
        import re
        summary_parts = []
        for cat in relevant_cats:
            match = re.search(rf'\[{cat.upper()}\]([^\[]*)', summary_text)
            if match:
                summary_parts.append(f"[{cat.upper()}] {match.group(1).strip()}")
        if summary_parts:
            lines.append("TASTE SUMMARIES:")
            lines.extend(summary_parts)

    return "\n".join(lines)


def _detect_relevant_categories(query: str, type_data: dict) -> list:
    """
    Detect which media categories are relevant to the user's message.
    Falls back to all categories if query is general.
    """
    if not query:
        return list(type_data.keys())

    q = query.lower()

    # Explicit category keywords
    signals = {
        "music":  ["music", "song", "album", "artist", "track", "listen", "band",
                   "playlist", "spotify", "metal", "rock", "pop", "jazz", "hip hop",
                   "rap", "classical", "concert", "genre", "last.fm"],
        "movie":  ["movie", "film", "cinema", "watch", "director", "actor",
                   "box office", "netflix", "streaming", "sequel", "prequel",
                   "radarr", "imdb", "rotten tomatoes"],
        "anime":  ["anime", "manga", "episode", "season", "shonen", "shojo",
                   "isekai", "mecha", "crunchyroll", "funimation", "subtitles",
                   "dub", "sub", "anilist", "myanimelist"],
        "show":   ["series", "show", "episode", "season", "binge", "sonarr",
                   "tv", "television", "netflix", "hbo", "disney", "streaming"],
    }

    matched = set()
    for cat, keywords in signals.items():
        if any(kw in q for kw in keywords):
            matched.add(cat)

    # Title matching — check if query mentions titles from any category
    # (simple check, not exhaustive)
    if not matched:
        return list(type_data.keys())  # general query → all categories

    # Always include a category if it has significant history
    # and the query is somewhat general
    general_words = {"recommend", "suggest", "what should", "something", "anything",
                     "good", "like", "similar", "more", "next", "watch", "listen"}
    is_general = any(w in q for w in general_words)
    if is_general and len(matched) <= 1:
        # Include all well-populated categories for general recommendations
        for cat, ts in type_data.items():
            if isinstance(ts, dict) and ts.get("watch_count", 0) >= 5:
                matched.add(cat)

    return list(matched) if matched else list(type_data.keys())


# ─────────────────────────────────────────────────────────────────────────────
# ARR INCREMENTAL SYNC HELPER
# ─────────────────────────────────────────────────────────────────────────────

async def sync_arr_new_items() -> dict:
    """
    Fetch only items added to Radarr/Sonarr since last ARR sync.
    Returns list of new items to enrich.
    """
    last_arr_sync = get_datetime("last_arr_sync_at")
    cutoff_ts = int(last_arr_sync.timestamp()) if last_arr_sync else 0
    new_items = []

    if settings.RADARR_URL and settings.RADARR_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"{settings.RADARR_URL.rstrip('/')}/api/v3/movie",
                    headers={"X-Api-Key": settings.RADARR_API_KEY},
                    params={"sortKey": "added", "sortDir": "desc"},
                )
            if r.status_code == 200:
                movies = r.json()
                for m in movies:
                    added_str = m.get("added", "")
                    if added_str:
                        try:
                            added_ts = int(datetime.fromisoformat(
                                added_str.replace("Z", "+00:00")
                            ).timestamp())
                            if added_ts <= cutoff_ts:
                                break  # sorted desc — older items follow
                        except Exception:
                            pass
                    new_items.append({
                        "tmdb_id": m.get("tmdbId"),
                        "title": m.get("title", ""),
                        "media_type": "movie",
                        "plex_rating_key": None,
                    })
                logger.info("Radarr: %d new items since last sync", len(new_items))
        except Exception as e:
            logger.warning("Radarr incremental sync failed: %s", e)

    sonarr_start = len(new_items)
    if settings.SONARR_URL and settings.SONARR_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"{settings.SONARR_URL.rstrip('/')}/api/v3/series",
                    headers={"X-Api-Key": settings.SONARR_API_KEY},
                )
            if r.status_code == 200:
                for s in r.json():
                    added_str = s.get("added", "")
                    if added_str:
                        try:
                            added_ts = int(datetime.fromisoformat(
                                added_str.replace("Z", "+00:00")
                            ).timestamp())
                            if added_ts <= cutoff_ts:
                                continue
                        except Exception:
                            pass
                    new_items.append({
                        "tmdb_id": s.get("tmdbId"),
                        "title": s.get("title", ""),
                        "media_type": "tv",
                        "plex_rating_key": None,
                    })
            logger.info("Sonarr: %d new items since last sync", len(new_items) - sonarr_start)
        except Exception as e:
            logger.warning("Sonarr incremental sync failed: %s", e)

    set_datetime("last_arr_sync_at", datetime.utcnow())
    return {"new_items": new_items, "total": len(new_items)}
