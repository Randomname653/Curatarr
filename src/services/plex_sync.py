"""
ARR Suite LLM - Plex Watch History Sync + Taste Vector Builder

Pulls full playback history from Plex for every known user,
stores it in watch_history table, then computes taste vectors
and a plain-text LLM summary for each user.
"""

import asyncio
import calendar
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from src.config import settings
from src.database.connection import get_db_session
from src.services.app_state import get_datetime, set_datetime, get_state, set_state
from src.database.models import (
    User, WatchHistoryEntry, TasteVectorEntry, PlexRating,
)

logger = logging.getLogger(__name__)

from src.services.task_monitor import task_monitor
from src.services.episodic_memory import retrieve_memories, format_memories_for_context
from src.services.llm_utils import (
    strip_think_tags, ollama_options, curator_options,
    detect_user_language, language_directive,
)


# ─────────────────────────────────────────────────────────────────────────────
# PLEX MUSIC RATING SYNC (Pass 82b)
# ─────────────────────────────────────────────────────────────────────────────

# Plex content-type numbers we sweep for ratings. Music ONLY — Kometa
# overwrites userRating on movies/shows with aggregated platform ratings,
# so using those as "personal opinion" would be misleading.
# Order matters for the artist_name extraction below: each level's metadata
# uses a different field name for the artist (see ``_artist_name_for_level``).
_RATING_LEVELS = (
    ("10", "track"),   # tracks — most common PlexAmp rating target
    ("9",  "album"),   # albums
    ("8",  "artist"),  # artists themselves
)


def _artist_name_for_level(item: dict, level: str) -> str:
    """Extract the artist's display name from a Plex metadata item.

    Plex's music hierarchy uses different field names per level:
      - track  (type 10): grandparentTitle = artist
      - album  (type 9):  parentTitle      = artist
      - artist (type 8):  title            = artist itself
    """
    if level == "track":
        return (item.get("grandparentTitle") or "").strip()
    if level == "album":
        return (item.get("parentTitle") or "").strip()
    if level == "artist":
        return (item.get("title") or "").strip()
    return ""


async def _sync_music_ratings(
    plex_url: str,
    headers: dict,
    lib_configs: dict,
    owner_user_id: int,
) -> int:
    """Pull user-set ratings from every music library section and upsert them
    into the ``plex_ratings`` table.

    For each music section we make three filtered fetches (track / album /
    artist) using Plex's ``userRating>>=1`` filter so only items the user
    has actually rated come back — keeps the response payload tiny even
    on large libraries. ``userRating>>`` with value ``"0"`` matches any
    rating strictly greater than zero, i.e. ≥ 0.5 stars on the UI scale.

    Attribution: ratings are credited to ``owner_user_id`` — the Plex token
    in settings is single-admin and Plex's section-fetch only returns the
    token-holder's userRating. Multi-user rating capture would need per-user
    Plex tokens, deferred until anyone asks.

    Returns the number of rows upserted (insert + update combined).
    """
    music_keys = [
        (sec_key, sec_title)
        for sec_key, (category, _sec_type, sec_title) in lib_configs.items()
        if category == "music"
    ]
    if not music_keys:
        return 0

    upserted = 0
    skipped_no_key = 0

    async with httpx.AsyncClient(timeout=60) as client:
        for sec_key, sec_title in music_keys:
            for type_num, level in _RATING_LEVELS:
                try:
                    resp = await client.get(
                        f"{plex_url}/library/sections/{sec_key}/all",
                        headers=headers,
                        # ``userRating>>`` with value ``"0"`` = strictly
                        # greater than zero. Plex's filter operator
                        # convention matches ``viewCount>>=0`` already used
                        # elsewhere in this file.
                        params={"type": type_num, "userRating>>": "0"},
                    )
                except Exception as e:
                    logger.warning("[ratings] %s %s fetch failed: %s",
                                   sec_title, level, e)
                    continue
                if resp.status_code != 200:
                    # 404 typically means the section was removed mid-sync;
                    # the main sync loop already handles that case + cleans
                    # up the LibraryConfig row. Anything else is logged so
                    # a transient Plex outage is visible.
                    if resp.status_code != 404:
                        logger.warning("[ratings] %s %s returned HTTP %s",
                                       sec_title, level, resp.status_code)
                    continue

                items = resp.json().get("MediaContainer", {}).get("Metadata", []) or []
                if not items:
                    continue
                logger.info("[ratings] %r: %d rated %s(s)", sec_title, len(items), level)

                with get_db_session() as db:
                    for item in items:
                        rating_key = item.get("ratingKey")
                        user_rating = item.get("userRating")
                        if not rating_key:
                            skipped_no_key += 1
                            continue
                        if user_rating is None:
                            # Shouldn't happen given the filter, but defend
                            # against a Plex API behaviour change.
                            continue
                        try:
                            rating_val = float(user_rating)
                        except (TypeError, ValueError):
                            continue
                        if rating_val <= 0:
                            continue

                        artist_name = _artist_name_for_level(item, level)
                        rated_at = None
                        last_rated_ts = item.get("lastRatedAt")
                        if last_rated_ts:
                            try:
                                rated_at = datetime.utcfromtimestamp(int(last_rated_ts))
                            except (TypeError, ValueError):
                                rated_at = None

                        existing = db.query(PlexRating).filter(
                            PlexRating.user_id == owner_user_id,
                            PlexRating.plex_item_id == str(rating_key),
                        ).first()
                        if existing:
                            existing.rating = rating_val
                            existing.rated_at = rated_at
                            existing.artist_name = artist_name or existing.artist_name
                            existing.media_type = level
                        else:
                            db.add(PlexRating(
                                user_id=owner_user_id,
                                plex_item_id=str(rating_key),
                                media_type=level,
                                rating=rating_val,
                                rated_at=rated_at,
                                artist_name=artist_name or None,
                            ))
                        upserted += 1
                    db.commit()

    if skipped_no_key:
        logger.debug("[ratings] skipped %d items lacking ratingKey", skipped_no_key)
    if upserted:
        logger.info("[ratings] upserted %d total rating row(s) for user_id=%d",
                    upserted, owner_user_id)
    return upserted


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

    # Build the timestamp filter for incremental syncs.
    # last_sync is a naive UTC datetime; .timestamp() would reinterpret it as
    # local — use calendar.timegm to convert deterministically.
    last_sync_ts = None
    if not is_initial and last_sync:
        last_sync_ts = calendar.timegm(last_sync.utctimetuple())
        logger.info("Incremental sync — filtering items with lastViewedAt >= %d (%s UTC)",
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

    # ── Per-event accountID lookup ─────────────────────────────────────────────
    # /library/sections/all has no per-account fields — `lastViewedAt` is the
    # max timestamp across ALL accounts on the server. To attribute each play
    # to the user who actually watched it, we cross-reference against
    # /status/sessions/history/all, which lists individual play events with
    # `accountID` per row. The shared helper below paginates the full history
    # so libraries with hundreds of thousands of plays don't run dry on the
    # first page (the cause of the 99% unattributable rate seen earlier).
    try:
        history_account_lookup, history_by_rating_key = await _fetch_plex_history_lookup(
            plex_url, headers, since_ts=last_sync_ts,
        )
    except Exception as e:
        logger.warning(
            "Plex history fetch failed: %s — falling back to admin attribution", e,
        )
        history_account_lookup = {}
        history_by_rating_key = {}

    def _resolve_account_for_event(rk: str, vts: int) -> Optional[int]:
        """Find the Plex accountID that watched ratingKey=rk at viewedAt=vts.

        Tries an exact (rk, vts) hit first; else picks the play event with the
        closest viewedAt within ±90 s (Plex sometimes truncates seconds
        between the section endpoint and the history endpoint).
        """
        exact = history_account_lookup.get((rk, vts))
        if exact is not None:
            return exact
        candidates = history_by_rating_key.get(rk)
        if not candidates:
            return None
        best = min(candidates, key=lambda c: abs(c[0] - vts))
        if abs(best[0] - vts) <= 90:
            return best[1]
        return None

    # Write to DB
    synced = 0
    skipped = 0
    unattributed = 0  # play events whose accountID couldn't be resolved → fell back to admin

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

            viewed_at = datetime.utcfromtimestamp(int(viewed_at_ts))

            # ── Per-event user attribution ────────────────────────────────────
            # Cross-reference with the history endpoint to find the accountID
            # that actually watched this play. In-progress entries have no
            # corresponding history row yet, so they always fall back to the
            # admin (current Plex API limitation — fixed once the play
            # completes and shows up in /status/sessions/history/all).
            is_in_progress = entry.get("_in_progress", False)
            account_id = None
            if not is_in_progress:
                account_id = _resolve_account_for_event(rating_key, int(viewed_at_ts))

            user = None
            if account_id is not None:
                user = resolved_account_map.get(str(account_id))
            if user is None:
                # Either no history row matched (in-progress / cleared history)
                # or the accountID resolves to a Plex user who hasn't logged
                # into Curatarr yet. Park the row on admin and remember the
                # actual Plex accountID so a later re-attribution pass (Pass
                # 4b admin tool) can fix it.
                user = admin_user
                if account_id is not None:
                    unattributed += 1
            if not user:
                continue

            # The Plex accountID lives on plex_user_id so re-attribution can
            # find the row even if the Curatarr user_id was a fallback.
            entry_plex_account = str(account_id) if account_id is not None else str(user.plex_user_id or "")

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
                # Update completion if this is in-progress and we have better data.
                # Filter on the FULL dedup key (incl. viewed_at) so we don't
                # clobber prior plays of the same item.
                if is_in_progress and view_offset_ms:
                    row = db.query(WatchHistoryEntry).filter(
                        WatchHistoryEntry.user_id == user.id,
                        WatchHistoryEntry.plex_item_id == rating_key,
                        WatchHistoryEntry.viewed_at == viewed_at,
                    ).first()
                    if row:
                        row.view_offset_ms = int(view_offset_ms)
                        row.completed = completed
                skipped += 1
                continue

            # audit 11d: Plex moves lastViewedAt on every sync for IN-
            # PROGRESS items — the (user, key, viewed_at) dedup then turned
            # each evening of the same unfinished film into a NEW row
            # (playcount inflation). Advance the unfinished row instead.
            if is_in_progress:
                _prev = db.query(WatchHistoryEntry).filter(
                    WatchHistoryEntry.user_id == user.id,
                    WatchHistoryEntry.plex_item_id == rating_key,
                    WatchHistoryEntry.completed == False,  # noqa: E712
                ).order_by(WatchHistoryEntry.viewed_at.desc()).first()
                if _prev is not None and _prev.viewed_at != viewed_at:
                    existing.discard((user.id, rating_key, _prev.viewed_at))
                    _prev.viewed_at = viewed_at
                    _prev.view_offset_ms = int(view_offset_ms or 0)
                    _prev.completed = completed
                    existing.add(dedup_key)
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
                plex_user_id=entry_plex_account or str(user.plex_user_id),
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

    logger.info(
        "Plex sync done: %d new entries, %d skipped, %d events parked on admin (unattributed Plex accounts)",
        synced, skipped, unattributed,
    )

    # Pass 82b: music-rating sweep. Runs after the play-history commit so the
    # rating sync is decoupled — a Plex hiccup during the rating fetch
    # doesn't lose any of the play data already persisted above. Best-effort:
    # logs failures, doesn't raise.
    try:
        with get_db_session() as _db:
            _admin = _db.query(User).filter(User.is_admin == True).first()  # noqa: E712
            _admin_id = _admin.id if _admin else None
        if _admin_id is not None:
            await _sync_music_ratings(plex_url, headers, lib_configs, _admin_id)
        else:
            logger.debug("[ratings] no admin user yet — skipping rating sweep")
    except Exception as e:
        logger.warning("[ratings] sweep failed: %s", e)

    set_datetime("last_sync_at", datetime.utcnow())
    _done_msg = f"Done: {synced} new entries, {skipped} skipped"
    if unattributed:
        _done_msg += f" ({unattributed} parked on admin — run Re-Attribution after the user logs in)"
    task_monitor.done(_sync_task, _done_msg)

    # Delegate taste vector recompute to taste_engine (canonical implementation)
    if synced > 0:
        # New watch data → invalidate downstream recommendation caches.
        set_datetime("recs_invalidate_at", datetime.utcnow())
        from src.services.taste_engine import compute_all_taste_vectors
        with get_db_session() as db:
            active_users = db.query(User).filter(User.is_active == True).all()
            user_ids = [u.id for u in active_users]
        for uid in user_ids:
            await compute_all_taste_vectors(uid)
    else:
        logger.info("No new entries — skipping taste vector recompute")

    return {
        "synced": synced,
        "skipped": skipped,
        "unattributed": unattributed,
        "total_fetched": len(all_entries),
    }


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN ACTIONS — re-attribute existing watch history + drop orphan rows
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_plex_history_lookup(
    plex_url: str,
    headers: dict,
    since_ts: Optional[int] = None,
) -> tuple[dict, dict]:
    """Fetch /status/sessions/history/all and build accountID lookups.

    Returns ``(exact_lookup, by_rating_key)`` where:
      - ``exact_lookup[(rating_key, viewedAt_seconds)] = accountID``
      - ``by_rating_key[rating_key] = [(viewedAt, accountID), ...]``  (for
        fuzzy fallback within ±90s).
    Paginated: walks the full history via ``X-Plex-Container-Start`` /
    ``X-Plex-Container-Size`` headers. Without this, Plex returns only
    a default-size container (~100 events) which is useless for libraries
    with hundreds of thousands of plays.

    ``since_ts`` (Unix seconds, optional): pass to limit the walk to events
    on or after that timestamp via the ``viewedAt>>`` query param. Used by
    incremental sync; re-attribution passes None to walk the full history.
    """
    exact: dict[tuple[str, int], int] = {}
    by_rk: dict[str, list[tuple[int, int]]] = {}
    PAGE_SIZE = 1000
    SAFETY_CAP = 5_000_000  # absolute hard limit so we can't infinite-loop
    start = 0
    total_seen = 0
    pages = 0
    async with httpx.AsyncClient(timeout=60) as client:
        while True:
            page_headers = {
                **headers,
                "Accept": "application/json",
                "X-Plex-Container-Start": str(start),
                "X-Plex-Container-Size": str(PAGE_SIZE),
            }
            params = {"sort": "viewedAt:desc"}
            if since_ts:
                params["viewedAt>>"] = str(since_ts - 60)
            r = await client.get(
                f"{plex_url}/status/sessions/history/all",
                headers=page_headers,
                params=params,
            )
            if r.status_code != 200:
                logger.warning(
                    "Plex history fetch: HTTP %s at start=%d (after %d events)",
                    r.status_code, start, total_seen,
                )
                break
            mc = r.json().get("MediaContainer", {}) or {}
            items = mc.get("Metadata", []) or []
            if not items:
                break
            for h in items:
                rk = str(h.get("ratingKey", ""))
                vts = h.get("viewedAt")
                acc_id = h.get("accountID")
                if not rk or vts is None or acc_id is None:
                    continue
                vts = int(vts)
                acc_id = int(acc_id)
                exact[(rk, vts)] = acc_id
                by_rk.setdefault(rk, []).append((vts, acc_id))
            page_count = len(items)
            total_seen += page_count
            pages += 1
            # Last page reached: server returned fewer than we asked for.
            if page_count < PAGE_SIZE:
                break
            if total_seen >= SAFETY_CAP:
                logger.warning(
                    "Plex history fetch: safety cap %d reached, stopping pagination",
                    SAFETY_CAP,
                )
                break
            start += page_count
    logger.info(
        "Plex history fetch: %d play events across %d pages → %d unique (rk, viewedAt) buckets",
        total_seen, pages, len(exact),
    )
    return exact, by_rk


async def _fetch_account_to_user_map(plex_url: str, headers: dict, users: list) -> dict:
    """Fetch /accounts and resolve to local DB User objects keyed by accountID-as-str.

    accountID="1" always maps to the admin (server owner). Other rows match
    by case-insensitive Plex username.
    """
    admin_user = next((u for u in users if u.is_admin), users[0] if users else None)
    out: dict[str, "User"] = {}
    if not admin_user:
        return out
    out["1"] = admin_user
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{plex_url}/accounts",
                headers={**headers, "Accept": "application/json"},
            )
        if r.status_code != 200:
            return out
        accounts = (
            r.json().get("MediaContainer", {}).get("Account")
            or r.json().get("MediaContainer", {}).get("Accounts")
            or []
        )
        if isinstance(accounts, dict):
            accounts = [accounts]
        for acc in accounts:
            local_id = str(acc.get("id", ""))
            name = (acc.get("name") or acc.get("title") or "").strip()
            if not local_id or local_id == "1" or not name:
                continue
            matched = next(
                (u for u in users if (u.plex_username or "").lower() == name.lower()),
                None,
            )
            if matched:
                out[local_id] = matched
    except Exception as e:
        logger.warning("Re-attribution: /accounts fetch failed: %s", e)
    return out


async def import_plex_history_for_user(user_id: int) -> dict:
    """Pull a user's OWN Plex watch history (by their Plex account) into
    local watch_history rows.

    Unlike ``sync_plex_history`` (which walks the whole server keyed on the
    current library) and ``reattribute_watch_history`` (which only re-labels
    rows that already exist locally), this targets ONE user and is built for
    auto-onboarding a freshly-arrived second account:

      1. Resolve the user's Plex accountID by matching their ``plex_username``
         against ``/accounts``.
      2. Pull their play events from ``/status/sessions/history/all?accountID=``
         (paginated). These events carry the title/series/season/episode but —
         for plays in an old, since-removed library section — NO ``ratingKey``.
      3. Resolve each title against the shared ``enrichment_status`` knowledge
         base to get a canonical key + category. This works even for titles the
         admin never watched (ARR-library items enriched as ``sonarr:``/
         ``radarr:`` keys), and the key matches what the taste engine looks the
         enrichment up under, so a taste vector can actually be built.
      4. Insert one row per play event (real ``viewedAt``) so the taste
         engine's per-category play-count weighting is preserved.

    Idempotent: re-running skips rows it already inserted (by key + viewed_at).
    Best-effort; returns a summary dict.
    """
    from collections import defaultdict
    from src.database.models import WatchHistoryEntry, EnrichmentStatus, User

    plex_url = settings.effective_plex_url
    plex_token = settings.effective_plex_token
    if not plex_url or not plex_token:
        return {"error": "Plex not configured"}
    headers = {
        "Accept": "application/json",
        "X-Plex-Token": plex_token,
        "X-Plex-Client-Identifier": settings.PLEX_CLIENT_ID,
    }

    with get_db_session() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return {"error": "user not found"}
        username = (user.plex_username or "").strip()
        is_admin = bool(user.is_admin)
    # The admin (server owner) is the data source; their plays are synced the
    # normal way. Don't import "their" account history on top of it.
    if is_admin or not username:
        return {"imported": 0, "reason": "admin or no username"}

    # 1. Resolve accountID from /accounts by username (case-insensitive).
    account_id = None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{plex_url}/accounts", headers=headers)
        accounts = r.json().get("MediaContainer", {}).get("Account") or []
        if isinstance(accounts, dict):
            accounts = [accounts]
        for a in accounts:
            nm = (a.get("name") or a.get("title") or "").strip()
            if nm and nm.lower() == username.lower():
                account_id = str(a.get("id"))
                break
    except Exception as e:
        logger.warning("[history-import] /accounts failed for user %s: %s", user_id, e)
        return {"error": "accounts fetch failed"}
    if not account_id:
        logger.info("[history-import] no Plex account matches %r — nothing to import", username)
        return {"imported": 0, "reason": "no matching plex account"}

    # 2. Pull the user's play events (paginated, filtered to their account).
    events: list = []
    start = 0
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                ph = {**headers, "X-Plex-Container-Start": str(start),
                      "X-Plex-Container-Size": "1000"}
                r = await client.get(
                    f"{plex_url}/status/sessions/history/all",
                    headers=ph, params={"accountID": account_id},
                )
                items = r.json().get("MediaContainer", {}).get("Metadata") or []
                if not items:
                    break
                events.extend(items)
                if len(items) < 1000:
                    break
                start += 1000
                if start > 200_000:
                    break
    except Exception as e:
        logger.warning("[history-import] history fetch failed for user %s: %s", user_id, e)
        return {"error": "history fetch failed"}
    if not events:
        return {"imported": 0, "reason": "no plex history events", "account_id": account_id}

    def _canon(m: dict) -> tuple:
        is_ep = m.get("type") == "episode"
        t = ((m.get("grandparentTitle") if is_ep else m.get("title")) or "").strip()
        return t, is_ep

    # 3. Resolve each distinct title -> (key, category) via enrichment_status.
    #    Prefer a row that already has a vector and whose category matches the
    #    play kind (episode -> anime/show, movie -> movie).
    resolved: dict = {}
    unresolved: list = []
    with get_db_session() as db:
        titles = {}
        for m in events:
            t, is_ep = _canon(m)
            if t:
                titles[t] = is_ep
        for t, is_ep in titles.items():
            rows = db.query(EnrichmentStatus).filter(EnrichmentStatus.title == t).all()
            cands = [r for r in rows if (r.vector_ready or r.enriched) and r.plex_rating_key]
            if not cands:
                unresolved.append(t)
                continue
            want = ("anime", "show") if is_ep else ("movie",)
            pick = next((r for r in cands if r.media_category in want), cands[0])
            resolved[t] = (pick.plex_rating_key, pick.media_category)

    # 4. Insert one row per play event (real viewedAt). Dedupe on re-run.
    imported = 0
    by_cat: dict = defaultdict(int)
    with get_db_session() as db:
        existing = {
            (w.plex_item_id, w.viewed_at)
            for w in db.query(WatchHistoryEntry.plex_item_id, WatchHistoryEntry.viewed_at)
                       .filter(WatchHistoryEntry.user_id == user_id).all()
        }
        for m in events:
            t, is_ep = _canon(m)
            if t not in resolved:
                continue
            key, cat = resolved[t]
            va = m.get("viewedAt")
            vt = datetime.utcfromtimestamp(int(va)) if va else None
            if (key, vt) in existing:
                continue
            db.add(WatchHistoryEntry(
                user_id=user_id,
                plex_user_id=account_id,
                plex_item_id=key,
                title=(m.get("title") or t),
                media_type=cat,
                series_title=t if is_ep else None,
                season=m.get("parentIndex"),
                episode=m.get("index"),
                viewed_at=vt,
                completed=1,
                source=None,
            ))
            existing.add((key, vt))
            imported += 1
            by_cat[cat] += 1
        db.commit()

    logger.info("[history-import] user %s (acct %s): imported %d rows %s, %d titles unresolved",
                user_id, account_id, imported, dict(by_cat), len(unresolved))
    return {
        "imported": imported,
        "by_category": dict(by_cat),
        "unresolved": unresolved,
        "account_id": account_id,
    }


async def reattribute_watch_history() -> dict:
    """Admin action: re-pull Plex history and fix user attribution on existing rows.

    For every WatchHistoryEntry whose plex_item_id is a numeric Plex
    ratingKey, look up the play in /status/sessions/history/all by
    (ratingKey, viewedAt±90s) and read the real accountID. If that
    accountID resolves to a User who has logged in via Plex OAuth,
    update the row's ``user_id`` and ``plex_user_id``. Spotify entries
    (``plex_item_id`` starts with ``spotify:``) are skipped — they have
    no Plex history row.

    Idempotent. Safe to run repeatedly.
    """
    plex_url = settings.effective_plex_url
    plex_token = settings.effective_plex_token
    if not plex_url or not plex_token:
        return {"error": "Plex not configured"}

    headers = {
        "Accept": "application/json",
        "X-Plex-Token": plex_token,
        "X-Plex-Client-Identifier": settings.PLEX_CLIENT_ID,
    }

    exact, by_rk = await _fetch_plex_history_lookup(plex_url, headers)

    # Diagnostics: total Plex play events seen + how many distinct ratingKeys
    # they cover. Surfaced in the response so the UI can show the user
    # exactly what Plex returned.
    plex_events_total = sum(len(v) for v in by_rk.values())
    plex_unique_rks = len(by_rk)

    # Sample a few unattributable rows for the response (helps diagnose
    # whether watch_history was imported from outside Plex).
    unattributable_samples: list[dict] = []

    with get_db_session() as db:
        users = db.query(User).all()
        if not users:
            return {"error": "No users in DB"}
        account_to_user = await _fetch_account_to_user_map(plex_url, headers, users)

        # Filter out anything that originated from Spotify, even if
        # music_matcher later linked it to a Plex music ratingKey: those
        # plays happened on Spotify, not Plex, so they have no entry in
        # /status/sessions/history/all and their per-track viewed_at can't
        # be cross-referenced. Excluding spotify-prefixed pids alone misses
        # ~150k matched-into-Plex rows.
        from sqlalchemy import or_
        rows = db.query(WatchHistoryEntry).filter(
            or_(
                WatchHistoryEntry.source.is_(None),
                WatchHistoryEntry.source != "spotify",
            ),
            ~WatchHistoryEntry.plex_item_id.like("spotify:%"),
        ).all()

        updated = 0
        unchanged = 0
        unattributable = 0

        # How many of our local watch_history ratingKeys does Plex even know about?
        local_rks = {r.plex_item_id for r in rows if r.plex_item_id}
        rks_in_plex_history = len(local_rks & by_rk.keys())
        rks_missing_from_plex = len(local_rks - by_rk.keys())

        for row in rows:
            rk = row.plex_item_id
            if not rk:
                continue
            vts = calendar.timegm(row.viewed_at.utctimetuple()) if row.viewed_at else None

            # Find accountID — exact match preferred, fuzzy ±90s as fallback
            acc_id = None
            if vts is not None:
                acc_id = exact.get((rk, vts))
                if acc_id is None:
                    candidates = by_rk.get(rk) or []
                    if candidates:
                        best = min(candidates, key=lambda c: abs(c[0] - vts))
                        if abs(best[0] - vts) <= 90:
                            acc_id = best[1]

            if acc_id is None:
                unattributable += 1
                if len(unattributable_samples) < 5:
                    unattributable_samples.append({
                        "plex_item_id": rk,
                        "viewed_at": row.viewed_at.isoformat() if row.viewed_at else None,
                        "title": (row.title or "")[:80],
                        "rk_in_plex_history": rk in by_rk,
                    })
                continue

            target_user = account_to_user.get(str(acc_id))
            if target_user is None:
                # Account exists on the Plex server but no Curatarr user has
                # logged in for it yet. Update plex_user_id so we can find
                # the row again later, but leave user_id parked on whoever
                # owns it now.
                if row.plex_user_id != str(acc_id):
                    row.plex_user_id = str(acc_id)
                    updated += 1
                else:
                    unchanged += 1
                continue

            changed = False
            if row.user_id != target_user.id:
                row.user_id = target_user.id
                changed = True
            if row.plex_user_id != str(acc_id):
                row.plex_user_id = str(acc_id)
                changed = True
            if changed:
                updated += 1
            else:
                unchanged += 1

        db.commit()

    # Re-attribution shifts which user owns which entries → invalidate cached
    # recommendations across the board so the next read regenerates against
    # the corrected taste data.
    set_datetime("recs_invalidate_at", datetime.utcnow())

    logger.info(
        "Re-attribution done: %d updated, %d unchanged, %d unattributable | "
        "Plex returned %d events across %d ratingKeys; "
        "%d local rks present in Plex history, %d missing",
        updated, unchanged, unattributable,
        plex_events_total, plex_unique_rks,
        rks_in_plex_history, rks_missing_from_plex,
    )
    return {
        "updated": updated,
        "unchanged": unchanged,
        "unattributable": unattributable,
        "total_examined": updated + unchanged + unattributable,
        # Diagnostics
        "plex_history_events": plex_events_total,
        "plex_history_rating_keys": plex_unique_rks,
        "local_rks_in_plex_history": rks_in_plex_history,
        "local_rks_missing_from_plex_history": rks_missing_from_plex,
        "unattributable_samples": unattributable_samples,
    }


async def cleanup_orphan_watch_history() -> dict:
    """Admin action: drop watch_history rows pointing at media no longer in Plex.

    Walks every configured library, collects all ratingKeys currently in
    Plex, then deletes WatchHistoryEntry rows whose ``plex_item_id`` is a
    numeric Plex ratingKey not in that set. Spotify entries are kept
    (matched by ``plex_item_id LIKE 'spotify:%'``) — they aren't part of
    Plex's library universe.
    """
    plex_url = settings.effective_plex_url
    plex_token = settings.effective_plex_token
    if not plex_url or not plex_token:
        return {"error": "Plex not configured"}

    headers = {
        "Accept": "application/json",
        "X-Plex-Token": plex_token,
        "X-Plex-Client-Identifier": settings.PLEX_CLIENT_ID,
    }

    # Collect all rating keys currently visible to Plex
    from src.database.models import LibraryConfig
    with get_db_session() as db:
        sections = db.query(LibraryConfig).all()
        section_keys = [s.plex_section_key for s in sections]

    live_keys: set[str] = set()
    async with httpx.AsyncClient(timeout=60) as client:
        for sec_key in section_keys:
            try:
                r = await client.get(
                    f"{plex_url}/library/sections/{sec_key}/all",
                    headers=headers,
                    params={"includeGuids": "0"},
                )
                if r.status_code != 200:
                    logger.warning(
                        "Orphan-cleanup: section %s returned HTTP %s — skipping",
                        sec_key, r.status_code,
                    )
                    continue
                items = r.json().get("MediaContainer", {}).get("Metadata", []) or []
                for it in items:
                    rk = str(it.get("ratingKey", ""))
                    if rk:
                        live_keys.add(rk)
            except Exception as e:
                logger.warning("Orphan-cleanup: section %s fetch failed: %s", sec_key, e)
                # If we can't read a section we DON'T proceed — better to abort
                # than to delete the user's history because of a transient
                # network blip.
                return {
                    "error": f"Could not read library section {sec_key}: {e}",
                    "deleted": 0,
                }

    if not live_keys:
        return {"error": "No live ratingKeys collected — refusing to delete history",
                "deleted": 0}

    logger.info("Orphan-cleanup: %d live ratingKeys collected from Plex", len(live_keys))

    with get_db_session() as db:
        # Same filter as re-attribution: keep ALL Spotify-sourced rows even
        # if music_matcher gave them a numeric Plex ratingKey, because the
        # play happened on Spotify and the underlying Plex music track may
        # legitimately not be in any configured library section.
        from sqlalchemy import or_
        rows = db.query(WatchHistoryEntry).filter(
            or_(
                WatchHistoryEntry.source.is_(None),
                WatchHistoryEntry.source != "spotify",
            ),
            ~WatchHistoryEntry.plex_item_id.like("spotify:%"),
        ).all()
        to_delete_ids = []
        for row in rows:
            rk = row.plex_item_id
            if not rk:
                continue
            if rk in live_keys:
                continue
            # Only consider numeric ratingKeys (skip oddball custom IDs)
            if not rk.isdigit():
                continue
            to_delete_ids.append(row.id)

        if to_delete_ids:
            db.query(WatchHistoryEntry).filter(
                WatchHistoryEntry.id.in_(to_delete_ids)
            ).delete(synchronize_session=False)
            db.commit()

    if to_delete_ids:
        set_datetime("recs_invalidate_at", datetime.utcnow())

    logger.info("Orphan-cleanup done: %d rows deleted", len(to_delete_ids))
    return {
        "deleted": len(to_delete_ids),
        "live_keys_seen": len(live_keys),
        "examined": len(rows),
    }


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
    genre_aversion: dict = None,
    dropped_titles: list = None,
) -> str:
    """Ask Ollama to write a concise per-type taste summary using the CURATOR model."""
    top_genres = sorted(genre_affinity.items(), key=lambda x: x[1], reverse=True)[:8]
    genres_str = ", ".join(g for g, _ in top_genres)
    titles_str = ", ".join(top_titles[:12])
    themes_str = ", ".join(themes or [])
    moods_str  = ", ".join(moods or [])
    type_label = TYPE_LABELS.get(media_type, media_type)

    noun = "tracks/artists" if media_type == "music" else "titles/series" if media_type in ("show","anime") else "films"

    # Build aversion context strings
    aversion_str = ", ".join(list((genre_aversion or {}).keys())[:8]) or "None detected"
    dropped_titles_str = ", ".join((dropped_titles or [])[:5]) or "None detected"

    # Load domain-filtered memories so music profiles never pull film memories
    memories = await retrieve_memories(
        user_id,
        f"general taste rules exceptions for {media_type}",
        top_k=5,
        media_category=media_type,
    )
    memory_context = format_memories_for_context(memories)

    # Pass 47 (Pass-40 gap #2): the taste summary is downstream fuel for
    # every pitch, deletion proposal, and chat opener — if it's written in
    # English it acts as a hidden English anchor in the system. Detect the
    # user's chat language and pass the directive in so the summary lands
    # in the same language as the other LLM surfaces (Pass 40).
    from src.database.connection import get_db_session
    with get_db_session() as _db:
        user_lang = detect_user_language(user_id, _db)
    lang_line = language_directive(user_lang)

    # Pass 47 (A4-drift): use the canonical BLACKLISTED_PITCH_PHRASES list
    # via ``_blacklist_rule`` instead of an inline shorter copy that had
    # drifted out of sync (was missing 7 of the 17 banned phrases).
    from src.services.recommendations_engine import _blacklist_rule

    prompt = f"""[MODE: USER TASTE PROFILE GENERATION]
{lang_line}

You are Curatarr, an elite, analytical media curator. Write a 3-4 sentence summary of the user's taste in the '{type_label}' category.

DATA:
- Top Watched / Favorite Titles: {titles_str}
- Top Genres: {genres_str}
- Top Moods/Themes: {moods_str} | {themes_str}
- Abandoned / Dropped Genres: {aversion_str}
- User Memories: {memory_context if memory_context else "None"}

CRITICAL RULES (YOU MUST OBEY):
1. ABSTRACTION OVER ANCHORING: DO NOT explicitly name the 'Top Watched' titles in your text. Instead, synthesize the *aesthetic, thematic, and structural qualities* those titles share.
{_blacklist_rule(2)}
3. LEXICAL DIVERSITY: Use precise, sophisticated cinematic or literary vocabulary.
4. EMBRACE CONTRADICTIONS: If the data shows conflicting patterns (e.g., heavy drama vs. light sitcoms), highlight this contrast playfully without forcing a unifying theory.
5. NEGATIVE SPACE: You MUST dedicate at least one sentence to explicitly analyzing what the user rejects or abandons (based on the Abandoned/Dropped data). Defining their boundaries is critical.
6. TONE: Highly perceptive, slightly snarky, and brutally honest. Speak directly to the user in the second person.
"""

    try:
        ollama_url = settings.effective_ollama

        # Try SUMMARIZER (8B) first — sufficient for 3-4 sentences and much faster.
        # Fall back to CURATOR (32B) if summarizer isn't available.
        model_order = [
            settings.SUMMARIZER_MODEL, settings.BASE_SUMMARIZER_MODEL,
            settings.CURATOR_MODEL,    settings.BASE_CURATOR_MODEL,
        ]
        # Yield to an active chat before generating — this can fall back to
        # the big curator model, and even the 8B shouldn't fight a live chat
        # for the single GPU.
        from src.services.llm_priority import wait_for_curator
        await wait_for_curator()
        for model in model_order:
            if not model:
                continue
            # 300s: DeepSeek-R1 think tokens can be slow; 8B is fast but 32B needs headroom
            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post(
                    f"{ollama_url}/api/chat",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": False,
                        **curator_options(temperature=0.8, num_predict=2000),
                    },
                )
            if resp.status_code == 200:
                return strip_think_tags(resp.json().get("message", {}).get("content", "").strip())
            if resp.status_code == 404:
                logger.info("Taste summary: model %r not found, trying next...", model)
                continue
            logger.warning("Taste summary: Ollama returned %s for model %r", resp.status_code, model)
            break
    except httpx.TimeoutException:
        logger.warning("Taste summary timed out after 300s — using rule-based fallback")
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
    # last_arr_sync is a naive UTC datetime; use calendar.timegm so .timestamp()
    # doesn't reinterpret it as local time.
    cutoff_ts = calendar.timegm(last_arr_sync.utctimetuple()) if last_arr_sync else 0
    new_items = []
    radarr_ok = sonarr_ok = True

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
            else:
                radarr_ok = False
                logger.warning("Radarr incremental sync HTTP %s", r.status_code)
        except Exception as e:
            radarr_ok = False
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
            else:
                sonarr_ok = False
                logger.warning("Sonarr incremental sync HTTP %s", r.status_code)
        except Exception as e:
            sonarr_ok = False
            logger.warning("Sonarr incremental sync failed: %s", e)

    # Only advance the marker if both ARR endpoints we attempted actually
    # returned cleanly. Otherwise items added during the failure window would
    # be skipped on the next successful sync.
    if radarr_ok and sonarr_ok:
        set_datetime("last_arr_sync_at", datetime.utcnow())
    return {"new_items": new_items, "total": len(new_items)}


# ════════════════════════════════════════════════════════════════════════════
#  TECHNICAL METADATA SYNC (size-outlier intelligence)
# ════════════════════════════════════════════════════════════════════════════
# Collect per-item resolution / codec / HDR / size / runtime from Plex so the
# size-norm engine can learn "normal MB-per-minute" per class and the curator
# can flag genuine bloat instead of blanket file size. Distinct from the watch
# sync above: covers the WHOLE library (no viewCount filter) and reads the
# Media/Part/Stream tree the watch sync ignores. Movies = one row each; shows =
# episodes (Plex type 4) aggregated per series incl. specials, with series ids
# merged from the show items (type 2); music = tracks (type 10) per artist.

_PLEX_TECH_TYPE = {"movie": "1", "show": "4", "anime": "4", "music": "10"}


def _res_bucket(res: str) -> Optional[str]:
    """Normalise Plex videoResolution → a coarse bucket for class grouping."""
    r = (res or "").lower().strip()
    if not r:
        return None
    if r in ("4k", "2160", "2160p"):
        return "4k"
    if r.startswith("1080"):
        return "1080"
    if r.startswith("720"):
        return "720"
    if r in ("sd", "480", "480p", "576", "576p", "240", "360"):
        return "sd"
    return r


def _parse_guids(item: dict) -> dict:
    """Pull tmdb_id / tvdb_id ints out of a Plex item's Guid[] array."""
    out = {"tmdb_id": None, "tvdb_id": None}
    for g in (item.get("Guid") or []):
        gid = (g.get("id") or "")
        if gid.startswith("tmdb://"):
            v = gid[7:].split("?")[0]
            if v.isdigit():
                out["tmdb_id"] = int(v)
        elif gid.startswith("tvdb://"):
            v = gid[7:].split("?")[0]
            if v.isdigit():
                out["tvdb_id"] = int(v)
    return out


def _parse_media(item: dict) -> dict:
    """Extract size / duration / resolution / codec / HDR / langs from a Plex
    item's Media[]/Part[]/Stream[] tree. Reads ALL Media entries (a title kept in
    several qualities — 4K Remux + 1080p Bluray — has multiple Media): total size
    sums every version (real footprint), but resolution/codec/HDR and the
    bitrate basis come from the PRIMARY (largest) version, so a duplicate doesn't
    masquerade as bitrate bloat. ``versions`` = how many copies, ``primary_bytes``
    = the main copy's size (for mb_per_min). Best-effort; missing → None/0."""
    medias = item.get("Media") or []
    duration_ms = item.get("duration") or 0
    if not medias:
        return {"size_bytes": 0, "primary_bytes": 0, "duration_ms": duration_ms,
                "resolution": None, "codec": None, "hdr": False,
                "audio": set(), "subs": set(), "versions": 0}
    total_size = 0
    primary = medias[0]
    primary_size = 0
    for media in medias:
        part = (media.get("Part") or [{}])[0] or {}
        sz = part.get("size") or 0
        total_size += sz
        duration_ms = max(duration_ms, media.get("duration") or 0)
        if sz >= primary_size:
            primary_size, primary = sz, media

    ppart = (primary.get("Part") or [{}])[0] or {}
    streams = ppart.get("Stream") or primary.get("Stream") or []
    hdr = False
    audio, subs = set(), set()
    for s in streams:
        st = s.get("streamType")
        if st == 1:  # video
            trc = (s.get("colorTrc") or "").lower()
            disp = ((s.get("displayTitle") or "") + " "
                    + (s.get("extendedDisplayTitle") or "")).lower()
            if (s.get("DOVIPresent") or trc in ("smpte2084", "arib-std-b67")
                    or "hdr" in disp or "dolby vision" in disp):
                hdr = True
        elif st == 2 and s.get("languageCode"):
            audio.add(s["languageCode"])
        elif st == 3 and s.get("languageCode"):
            subs.add(s["languageCode"])
    vdr = (primary.get("videoDynamicRange") or "").lower()
    if vdr and vdr != "sdr":
        hdr = True
    return {
        "size_bytes": total_size, "primary_bytes": primary_size,
        "duration_ms": duration_ms,
        "resolution": _res_bucket(primary.get("videoResolution")),
        "codec": (primary.get("videoCodec") or "").lower() or None,
        "hdr": hdr, "audio": audio, "subs": subs, "versions": len(medias),
    }


async def _fetch_section_all(client, plex_url, headers, sec_key, type_num) -> list:
    """Paginated fetch of every item of a Plex type in a section (no view filter)."""
    PAGE = 400
    out, start = [], 0
    while True:
        try:
            r = await client.get(
                f"{plex_url}/library/sections/{sec_key}/all",
                headers={**headers, "X-Plex-Container-Start": str(start),
                         "X-Plex-Container-Size": str(PAGE)},
                params={"type": type_num, "includeGuids": "1"},
            )
        except Exception as e:
            logger.warning("[tech] fetch %s/%s failed: %s", sec_key, type_num, e)
            break
        if r.status_code != 200:
            break
        items = r.json().get("MediaContainer", {}).get("Metadata", []) or []
        out.extend(items)
        if len(items) < PAGE:
            break
        start += len(items)
    return out


def _persist_tech_profiles(agg: dict) -> int:
    """Upsert MediaTechProfile rows from the accumulator (by plex_rating_key)."""
    from src.database.models import MediaTechProfile
    written = 0
    with get_db_session() as db:
        for ck, a in agg.items():
            dur_min = round((a["duration_ms"] or 0) / 60000.0, 2)
            size_mb = round((a["size_bytes"] or 0) / (1024 * 1024), 2)
            primary_mb = round((a.get("primary_bytes", 0) or 0) / (1024 * 1024), 2)
            redundant_mb = round((a.get("redundant_bytes", 0) or 0) / (1024 * 1024), 2)
            # bitrate from the PRIMARY (largest) version so a kept duplicate copy
            # doesn't inflate mb_per_min into a false "bloated" reading.
            mb_per_min = (round(primary_mb / dur_min, 2)
                          if dur_min >= 1 and primary_mb else None)
            res = a["res"].most_common(1)[0][0] if a["res"] else None
            codec = a["codec"].most_common(1)[0][0] if a["codec"] else None
            row = db.query(MediaTechProfile).filter(
                MediaTechProfile.plex_rating_key == ck).first()
            if not row:
                row = MediaTechProfile(plex_rating_key=ck)
                db.add(row)
            row.media_type = a["media_type"]
            row.title = (a["title"] or "")[:512] or None
            row.tmdb_id = a["tmdb_id"]
            row.tvdb_id = a["tvdb_id"]
            row.size_mb = size_mb
            row.duration_min = dur_min
            row.mb_per_min = mb_per_min
            row.resolution = res
            row.codec = codec
            row.hdr = bool(a["hdr"])
            row.audio_langs = (",".join(sorted(a["audio"]))[:200]) or None
            row.sub_langs = (",".join(sorted(a["subs"]))[:200]) or None
            row.item_count = a["count"]
            row.versions = a.get("versions", 1)
            row.redundant_mb = redundant_mb
            row.updated_at = datetime.utcnow()
            written += 1
        db.commit()
    return written


async def sync_tech_profiles(force: bool = False, include_music: bool = False) -> dict:
    """Whole-library technical-metadata sync → MediaTechProfile rows.

    Music is OFF by default: a large library is hundreds of thousands of tracks
    (≈one fetch page per 400), and the size pain the curator hits is video
    (4K/specials). The norms + scoring + curator context all work per media_type,
    so music can be switched on later via include_music without other changes."""
    from collections import Counter
    plex_url = settings.effective_plex_url
    plex_token = settings.effective_plex_token
    if not plex_url or not plex_token:
        return {"error": "PLEX_URL / PLEX_TOKEN not configured"}

    last = get_datetime("last_tech_sync_at")
    if not force and last and (datetime.utcnow() - last).total_seconds() < 12 * 3600:
        return {"skipped": True, "last": last.isoformat()}

    headers = {"Accept": "application/json", "X-Plex-Token": plex_token,
               "X-Plex-Client-Identifier": settings.PLEX_CLIENT_ID}
    from src.database.models import LibraryConfig
    with get_db_session() as db:
        lib_configs = {c.plex_section_key: (c.media_category, c.plex_section_title)
                       for c in db.query(LibraryConfig).all()}

    _task = task_monitor.create(name="Tech-metadata sync", category="sync")
    task_monitor.start(_task)
    agg: dict = {}

    async with httpx.AsyncClient(timeout=90) as client:
        for sec_key, (category, sec_title) in lib_configs.items():
            type_num = _PLEX_TECH_TYPE.get(category)
            if not type_num:
                continue
            if category == "music" and not include_music:
                continue
            items = await _fetch_section_all(client, plex_url, headers, sec_key, type_num)
            task_monitor.log(_task, f"{sec_title}: {len(items)} parts")
            for it in items:
                if category == "movie":
                    ck = str(it.get("ratingKey"))
                    title = it.get("title")
                    ids = _parse_guids(it)
                else:
                    ck = str(it.get("grandparentRatingKey")
                             or it.get("parentRatingKey") or it.get("ratingKey"))
                    title = it.get("grandparentTitle") or it.get("title")
                    ids = {"tmdb_id": None, "tvdb_id": None}
                if not ck or ck == "None":
                    continue
                a = agg.setdefault(ck, {
                    "rating_key": ck, "media_type": category, "title": title,
                    "tmdb_id": ids["tmdb_id"], "tvdb_id": ids["tvdb_id"],
                    "size_bytes": 0, "primary_bytes": 0, "redundant_bytes": 0,
                    "duration_ms": 0, "res": Counter(), "codec": Counter(),
                    "hdr": False, "audio": set(), "subs": set(),
                    "count": 0, "versions": 1})
                m = _parse_media(it)
                a["size_bytes"] += m["size_bytes"] or 0           # total footprint
                a["primary_bytes"] += m["primary_bytes"] or 0     # bitrate basis
                a["redundant_bytes"] += (m["size_bytes"] or 0) - (m["primary_bytes"] or 0)
                a["versions"] = max(a["versions"], m["versions"])
                a["duration_ms"] += m["duration_ms"] or 0
                if m["resolution"]:
                    a["res"][m["resolution"]] += 1
                if m["codec"]:
                    a["codec"][m["codec"]] += 1
                a["hdr"] = a["hdr"] or m["hdr"]
                a["audio"] |= m["audio"]
                a["subs"] |= m["subs"]
                a["count"] += 1

            # Shows: merge series ids/title from the show items (type 2).
            if category in ("show", "anime"):
                for sh in await _fetch_section_all(client, plex_url, headers, sec_key, "2"):
                    rk = str(sh.get("ratingKey"))
                    if rk in agg:
                        gids = _parse_guids(sh)
                        agg[rk]["tmdb_id"] = agg[rk]["tmdb_id"] or gids["tmdb_id"]
                        agg[rk]["tvdb_id"] = agg[rk]["tvdb_id"] or gids["tvdb_id"]
                        agg[rk]["title"] = agg[rk]["title"] or sh.get("title")

    written = _persist_tech_profiles(agg)
    set_datetime("last_tech_sync_at", datetime.utcnow())
    task_monitor.done(_task, f"{written} tech profiles")
    logger.info("[tech] wrote %d MediaTechProfile rows across %d items", written, len(agg))
    return {"profiles": written, "items": len(agg)}
