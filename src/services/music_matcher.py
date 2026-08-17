"""
Curatarr — Music Matcher & Last.fm Genre Enricher

Two-phase pipeline for Spotify-imported watch_history entries:

  Phase 1 — Plex Match
    Loads every track from configured Plex music library sections (type=10).
    Fuzzy-matches by normalised (artist, title) against Spotify plays that
    still have source="spotify" and plex_item_id starting with "spotify:".
    On a match: updates plex_item_id to Plex ratingKey and copies genres.

  Phase 2 — Last.fm Genre Enrichment
    For every music play still missing genres, calls Last.fm track.getInfo.
    Grouped by (artist, title) to minimise API calls (one call per unique track,
    not per play).  Writes the top-5 tags as comma-separated genres.

Both phases check the AppState flag "music_pipeline_stop_requested" between
batches so the pipeline can be interrupted cleanly via the GUI.

Rate limits:
  Last.fm free tier: ~5 req/s sustained. We default to 4 req/s (250ms sleep).
"""

import asyncio
import logging
import re
import unicodedata

import httpx

from src.config import settings
from src.database.connection import get_db_session
from src.database.models import LibraryConfig, WatchHistoryEntry

logger = logging.getLogger(__name__)

# ── Normalisation ─────────────────────────────────────────────────────────────

_FEAT_RE    = re.compile(r'\s*[\(\[](feat\.?|ft\.?|with|x)\s+[^\)\]]+[\)\]]', re.I)
_EDITION_RE = re.compile(
    r'\s*[-–]\s*(radio edit|single (edit|version)|remaster(ed)?|'
    r'live( at .+)?|acoustic|remix|instrumental|extended|deluxe|'
    r'anniversary edition|\d{4} remaster)',
    re.I,
)
_PUNCT_RE   = re.compile(r"[^\w\s]")


def _normalize(s: str) -> str:
    """Lowercase, strip feat/edition suffixes, remove punctuation, normalize unicode."""
    if not s:
        return ""
    s = _FEAT_RE.sub("", s)
    s = _EDITION_RE.sub("", s)
    s = unicodedata.normalize("NFKD", s)
    s = _PUNCT_RE.sub("", s)
    return " ".join(s.lower().split())


def _stop_requested() -> bool:
    """Return True when the GUI/API has requested a pipeline stop."""
    try:
        from src.services.app_state import get_state
        return get_state("music_pipeline_stop_requested") == "1"
    except Exception:
        return False


# ── Phase 1: Plex match ───────────────────────────────────────────────────────

# Flush matched plays to DB every N items (keeps memory low, allows partial saves)
_MATCH_FLUSH = 500


async def match_spotify_to_plex(user_id: int) -> dict:
    """
    Match Spotify-sourced plays to actual Plex tracks.

    Returns {"matched": N, "unmatched": M, "skipped": K}
    skipped = Spotify plays that already have a Plex ratingKey (re-run safe).

    INCREMENTAL: each play gets exactly ONE match attempt. The per-user cursor
    (app_state ``music_match_cursor:<uid>``) marks how far we got; only plays
    NEWER than it are attempted, and with zero new plays the function returns
    before even fetching the Plex index. Without this, every nightly pipeline
    re-scanned the full ~200k historic Spotify plays that will never exist in
    Plex (matched=0 forever) — ~60 s of pure event-loop starvation that froze
    the entire app. Newly added Plex music does NOT retro-match old plays;
    delete the cursor row to force a one-off full re-match.
    """
    plex_url   = settings.effective_plex_url
    plex_token = settings.effective_plex_token

    if not plex_url or not plex_token:
        return {"error": "Plex not configured"}

    from src.services.app_state import get_state, set_state
    cursor_key = f"music_match_cursor:{user_id}"
    try:
        since_id = int(get_state(cursor_key) or 0)
    except (TypeError, ValueError):
        since_id = 0

    # ── 1. Find configured music library sections ─────────────────────────────
    with get_db_session() as db:
        music_sections = [
            c.plex_section_key
            for c in db.query(LibraryConfig)
            .filter(LibraryConfig.media_category == "music")
            .all()
        ]

        # Early-out BEFORE the Plex fetch: nothing new since the cursor means
        # no work — skip the section download + index build entirely.
        new_plays = (
            db.query(WatchHistoryEntry.id)
            .filter(
                WatchHistoryEntry.user_id    == user_id,
                WatchHistoryEntry.source     == "spotify",
                WatchHistoryEntry.media_type == "music",
                WatchHistoryEntry.plex_item_id.like("spotify%"),
                WatchHistoryEntry.id > since_id,
            )
            .count()
        )
    if new_plays == 0:
        logger.info("[music_matcher] No new Spotify plays since cursor %d — "
                    "match phase up to date", since_id)
        return {"matched": 0, "unmatched": 0, "skipped": 0, "up_to_date": True}

    if not music_sections:
        logger.warning("[music_matcher] No music library sections configured")
        return {"matched": 0, "unmatched": 0, "skipped": 0,
                "warning": "No music library configured"}

    headers = {
        "Accept": "application/json",
        "X-Plex-Token": plex_token,
        "X-Plex-Client-Identifier": settings.PLEX_CLIENT_ID,
    }

    # ── 2. Build Plex track index ─────────────────────────────────────────────
    # Key: (norm_artist, norm_title) → {"rating_key": str, "genres": list[str]}
    plex_index: dict[tuple, dict] = {}

    async with httpx.AsyncClient(timeout=120) as client:
        for sec_key in music_sections:
            if _stop_requested():
                break
            try:
                resp = await client.get(
                    f"{plex_url}/library/sections/{sec_key}/all",
                    headers=headers,
                    params={"type": "10"},   # type 10 = track
                )
                if resp.status_code != 200:
                    logger.warning("[music_matcher] Section %s returned HTTP %s",
                                   sec_key, resp.status_code)
                    continue

                items = resp.json().get("MediaContainer", {}).get("Metadata", []) or []
                logger.info("[music_matcher] Section %s: %d tracks", sec_key, len(items))

                for item in items:
                    # originalTitle = track artist (differs from album artist for features)
                    artist = item.get("originalTitle") or item.get("grandparentTitle", "")
                    title  = item.get("title", "")
                    key    = (_normalize(artist), _normalize(title))
                    if key[0] and key[1]:
                        genres = [g["tag"] for g in (item.get("Genre") or [])]
                        plex_index[key] = {
                            "rating_key": str(item["ratingKey"]),
                            "genres":     genres,
                        }
            except Exception as exc:
                logger.error("[music_matcher] Failed to fetch section %s: %s", sec_key, exc)

    if not plex_index:
        logger.warning("[music_matcher] No tracks found in Plex music libraries")
        return {"matched": 0, "unmatched": 0, "skipped": 0,
                "warning": "No tracks found in Plex"}

    logger.info("[music_matcher] Plex index: %d unique (artist, title) pairs", len(plex_index))

    # ── 3. Match Spotify plays ────────────────────────────────────────────────
    matched   = 0
    unmatched = 0
    skipped   = 0
    buffer    = []   # (play_id, rating_key, genres_str | None)

    completed = False
    with get_db_session() as db:
        spotify_plays = (
            db.query(WatchHistoryEntry)
            .filter(
                WatchHistoryEntry.user_id    == user_id,
                WatchHistoryEntry.source     == "spotify",
                WatchHistoryEntry.media_type == "music",
                # Unmatched plays still have the truncated spotify URI here
                WatchHistoryEntry.plex_item_id.like("spotify%"),
                # Incremental: one match attempt per play, ever (see docstring)
                WatchHistoryEntry.id > since_id,
            )
            .order_by(WatchHistoryEntry.id.asc())
            .all()
        )
        total_plays = len(spotify_plays)
        logger.info("[music_matcher] %d Spotify plays to match (cursor %d)",
                    total_plays, since_id)

        for i, play in enumerate(spotify_plays):
            if _stop_requested():
                logger.info("[music_matcher] Stop requested — flushing %d buffered matches", len(buffer))
                break

            key = (_normalize(play.series_title or ""), _normalize(play.title or ""))
            hit = plex_index.get(key)

            if hit:
                play.plex_item_id = hit["rating_key"]
                if hit["genres"] and not play.genres:
                    play.genres = ",".join(hit["genres"])
                matched += 1
            else:
                unmatched += 1

            # Flush every MATCH_FLUSH items so progress is persistent
            if (i + 1) % _MATCH_FLUSH == 0:
                db.flush()
                # This loop has no natural await — without yielding here it
                # starves the event loop for the whole run and the entire app
                # (every HTTP request) freezes until it finishes.
                await asyncio.sleep(0)
                logger.info("[music_matcher] … %d/%d plays processed (matched=%d)",
                            i + 1, total_plays, matched)
        else:
            completed = True

        db.commit()

        # Advance the cursor only after a COMPLETE, un-interrupted pass — a
        # stop-requested break re-tries the same window next run.
        if completed and spotify_plays:
            set_state(cursor_key, str(spotify_plays[-1].id))
            logger.info("[music_matcher] cursor advanced to %d", spotify_plays[-1].id)

    logger.info("[music_matcher] Match complete — matched=%d unmatched=%d skipped=%d",
                matched, unmatched, skipped)
    return {"matched": matched, "unmatched": unmatched, "skipped": skipped}


# ── Phase 1.4: MusicBrainz artist-MBID pre-resolve (Pass 16f) ────────────────

async def resolve_artist_mbids(user_id: int, batch: int = 200) -> dict:
    """
    For every Spotify-source play missing ``artist_mbid``, look up the
    MusicBrainz artist ID and write it back to all plays for that artist.

    Pre-resolves the MBID needed by the Spotify-Backlog → Lidarr-add flow
    (Pass 16g) so the user gets an instant "Add to Lidarr" button instead
    of waiting on a live MusicBrainz lookup at click time.

    Rate-limit: MusicBrainz public API caps at ~1 req/s; we go through
    ``fetch_musicbrainz_artist`` which already implements polite delays
    + caches results in api_cache so re-runs are cheap.

    ``batch`` caps unique artist names per run to avoid hammering MB on
    a fresh import. With 200 unique artists at 1 req/s this run takes
    ~3.5 minutes; the rest carries over to the next pipeline iteration.

    Returns {resolved: N, failed: M, queried: K, total_unique: ..., remaining: ...}
    """
    from src.services.music_metadata import fetch_musicbrainz_artist

    # Find unresolved unique artist names (one row per name; we update ALL
    # plays for that name once we have the MBID).
    with get_db_session() as db:
        from sqlalchemy import distinct
        rows = (
            db.query(distinct(WatchHistoryEntry.series_title))
            .filter(
                WatchHistoryEntry.user_id    == user_id,
                WatchHistoryEntry.media_type == "music",
                WatchHistoryEntry.source     == "spotify",
                WatchHistoryEntry.artist_mbid.is_(None),
                WatchHistoryEntry.series_title.isnot(None),
            )
            .all()
        )
    artist_names = [r[0] for r in rows if r[0]]
    total_unique = len(artist_names)

    if not artist_names:
        return {"resolved": 0, "failed": 0, "queried": 0,
                "total_unique": 0, "remaining": 0}

    # Apply batch cap
    if batch and batch > 0 and total_unique > batch:
        names = artist_names[:batch]
        logger.info(
            "[music_matcher] Phase 1.4: %d/%d unique artists (batch=%d, "
            "%d remain for next run)",
            len(names), total_unique, batch, total_unique - batch,
        )
    else:
        names = artist_names
        logger.info("[music_matcher] Phase 1.4: %d unique artists to resolve via MusicBrainz",
                    len(names))

    resolved = 0
    failed   = 0
    queried  = 0

    # Pass 16l: Phase 1.4 was previously silent in the UI for ~3.5 min on
    # the first run with 200+ unique artists. Match the progress shape
    # that Phase 1.5 / Phase 2 use so the music-pipeline status panel
    # actually animates instead of jumping from "phase 1" straight to
    # "phase 1.5". Emit every _STOP_CHECK iterations + once at the end.
    def _emit_progress(phase_label: str = "mbid_resolve"):
        try:
            import json as _json
            from src.services.app_state import set_state as _set_state
            _set_state("music_pipeline_progress", _json.dumps({
                "phase":         phase_label,
                "queried":       queried,
                "resolved":      resolved,
                "failed":        failed,
                "total_unique":  total_unique,
                "batch_size":    len(names),
                "pct":           round(100 * queried / max(len(names), 1)),
            }))
        except Exception:
            pass

    _emit_progress()  # initial — mark Phase 1.4 as live in the UI

    for i, name in enumerate(names):
        # Stop check + progress emit every _STOP_CHECK iterations
        if i % _STOP_CHECK == 0:
            _emit_progress()
            if _stop_requested():
                logger.info("[music_matcher] Phase 1.4: stop requested after %d artists", queried)
                break

        try:
            profile = await fetch_musicbrainz_artist(name)
            queried += 1
        except Exception as exc:
            logger.debug("[music_matcher] MB lookup failed for %r: %s", name, exc)
            failed += 1
            continue

        mbid = (profile or {}).get("mbid")
        if not mbid:
            failed += 1
            continue

        # Write MBID back to ALL plays for this artist
        try:
            with get_db_session() as db:
                count = (
                    db.query(WatchHistoryEntry)
                    .filter(
                        WatchHistoryEntry.user_id    == user_id,
                        WatchHistoryEntry.media_type == "music",
                        WatchHistoryEntry.series_title == name,
                        WatchHistoryEntry.artist_mbid.is_(None),
                    )
                    .update({"artist_mbid": mbid}, synchronize_session=False)
                )
                db.commit()
                resolved += 1
                if count > 1:
                    logger.debug("[music_matcher] MBID %s -> %d plays for %r",
                                 mbid, count, name)
        except Exception as exc:
            logger.warning("[music_matcher] MBID writeback failed for %r: %s", name, exc)
            failed += 1

    _emit_progress("mbid_resolve_done")  # final — caller advances to Phase 1.5 next

    logger.info(
        "[music_matcher] Phase 1.4 done — resolved=%d failed=%d queried=%d "
        "(total_unique=%d, %d remain)",
        resolved, failed, queried, total_unique, max(0, total_unique - len(names)),
    )

    return {
        "resolved":     resolved,
        "failed":       failed,
        "queried":      queried,
        "total_unique": total_unique,
        "remaining":    max(0, total_unique - len(names)),
    }


# ── Phase 1.5: Spotify genre enrichment ──────────────────────────────────────

async def enrich_music_genres_spotify(user_id: int, batch: int = 200) -> dict:
    """
    Enrich genres for Spotify plays using the Client Credentials flow.

    For every play that still has a 'spotify:track:' plex_item_id AND is missing
    genres, we extract the Spotify track ID, batch-fetch artist genres via
    /v1/tracks + /v1/artists (no user token needed), and write them back.

    This runs after Plex matching and before Last.fm so Last.fm only picks up
    tracks that Spotify also doesn't know.

    ``batch`` caps how many UNIQUE Spotify track IDs are queried per run. The
    previous behaviour pulled every unresolved track in one go, ignoring the
    UI's batch input — for libraries with hundreds of thousands of plays this
    meant the Spotify API got hammered for minutes per run. The cap is per
    unique track, but each track update writes back to all of its plays, so
    a batch of 200 typically covers far more than 200 rows.

    Resume is implicit: rows where ``genres`` was successfully written drop
    out of the next run's selection set (``genres IS NULL`` filter). Tracks
    Spotify could not resolve stay NULL and either get retried next run OR
    get picked up by Phase 2 (Last.fm), whichever runs first.

    Returns {"enriched_plays": N, "tracks_queried": M, "skipped": K,
             "tracks_remaining": ..., "tracks_total_unique": ...}
    """
    from src.services.spotify_client import resolve_track_genres

    client_id     = getattr(settings, "SPOTIFY_CLIENT_ID", None)
    client_secret = getattr(settings, "SPOTIFY_CLIENT_SECRET", None)

    if not client_id or not client_secret:
        return {"skipped": 0, "enriched_plays": 0, "tracks_queried": 0,
                "note": "Spotify credentials not configured"}

    # ── Collect plays with a spotify:track: URI and no genres ─────────────────
    with get_db_session() as db:
        rows = (
            db.query(
                WatchHistoryEntry.id,
                WatchHistoryEntry.plex_item_id,
                WatchHistoryEntry.series_title,
                WatchHistoryEntry.title,
            )
            .filter(
                WatchHistoryEntry.user_id    == user_id,
                WatchHistoryEntry.media_type == "music",
                WatchHistoryEntry.genres.is_(None),
                WatchHistoryEntry.plex_item_id.like("spotify:track:%"),
            )
            .all()
        )

    if not rows:
        return {"skipped": 0, "enriched_plays": 0, "tracks_queried": 0,
                "tracks_remaining": 0, "tracks_total_unique": 0}

    # Map: track_id → list of play IDs
    track_plays: dict[str, list[int]] = {}
    for play_id, plex_item_id, _artist, _title in rows:
        # plex_item_id format: "spotify:track:4uLU6hMCjMI75M1A2tKUQC"
        parts = (plex_item_id or "").split(":")
        if len(parts) == 3 and parts[1] == "track" and parts[2]:
            track_plays.setdefault(parts[2], []).append(play_id)

    all_unique_ids = list(track_plays.keys())
    total_unique   = len(all_unique_ids)
    if not all_unique_ids:
        return {"skipped": len(rows), "enriched_plays": 0, "tracks_queried": 0,
                "tracks_remaining": 0, "tracks_total_unique": 0}

    # Cap to batch — the per-run limit set by the user (or the scheduler default).
    if batch and batch > 0 and total_unique > batch:
        unique_track_ids = all_unique_ids[:batch]
        logger.info(
            "[music_matcher] Spotify enrichment: %d/%d unique tracks (batch=%d, "
            "%d remain for the next run)",
            len(unique_track_ids), total_unique, batch, total_unique - batch,
        )
    else:
        unique_track_ids = all_unique_ids
        logger.info(
            "[music_matcher] Spotify enrichment: %d unique track IDs (%d plays)",
            len(unique_track_ids), len(rows),
        )

    # ── Resolve genres ────────────────────────────────────────────────────────
    genre_map, rate_limited = await resolve_track_genres(
        unique_track_ids, client_id, client_secret
    )

    # ── Write back to DB ──────────────────────────────────────────────────────
    # Pass 16m: defensive .get() — the resolver should now key by the
    # original (linked_from) ID we asked about, but if Spotify ever
    # ships a quirk that hands us a track ID we never queried, we'd
    # rather skip it than crash the whole pipeline.
    #
    # Pass 89: chunked commits. The pre-89 version wrapped ALL writes in
    # a single ``with get_db_session(): … db.commit()`` block — fine
    # for the daily batch=300 path, but the standalone runner
    # (``music_enricher.py --only-spotify --spotify-batch 50000``)
    # bombed the write-lock for minutes per call. Concurrent writers
    # (in-app enrichment consumer, scheduler heartbeats, /api routes)
    # then hit the 60 s ``busy_timeout`` and cascaded into the
    # ``database is locked`` error chain reported in production.
    # Committing every ``_DB_COMMIT_CHUNK`` tracks releases the
    # write-lock between chunks; other writers get their turn instead
    # of being starved for the whole duration of the resolve.
    enriched_plays = 0
    unknown_ids   = 0
    _DB_COMMIT_CHUNK = 100   # tracks per commit — tuned for ~50ms write windows
    items = list(genre_map.items())
    for chunk_start in range(0, len(items), _DB_COMMIT_CHUNK):
        chunk = items[chunk_start:chunk_start + _DB_COMMIT_CHUNK]
        with get_db_session() as db:
            for track_id, genres in chunk:
                play_ids = track_plays.get(track_id)
                if not play_ids:
                    unknown_ids += 1
                    continue
                genres_str = ",".join(genres)
                count = (
                    db.query(WatchHistoryEntry)
                    .filter(WatchHistoryEntry.id.in_(play_ids))
                    .update({"genres": genres_str}, synchronize_session=False)
                )
                enriched_plays += count
            # ``get_db_session`` context manager commits on exit; explicit
            # ``db.commit()`` would be redundant.
    if unknown_ids:
        logger.warning(
            "[music_matcher] Spotify resolver returned %d track IDs we didn't ask about — skipped",
            unknown_ids,
        )

    logger.info(
        "[music_matcher] Spotify enrichment done — tracks_queried=%d enriched_plays=%d%s",
        len(unique_track_ids), enriched_plays,
        " (rate-limited — handing remainder to Last.fm)" if rate_limited else "",
    )

    return {
        "enriched_plays":         enriched_plays,
        "tracks_queried":         len(unique_track_ids),
        "tracks_resolved":        len(genre_map),
        "skipped":                0,
        "tracks_total_unique":    total_unique,
        "tracks_remaining":       max(0, total_unique - len(unique_track_ids)),
        "spotify_rate_limited":   rate_limited,
    }


# ── Phase 2: Last.fm genre enrichment ────────────────────────────────────────

_LASTFM_BASE  = "https://ws.audioscrobbler.com/2.0/"
_LASTFM_DELAY = 0.25   # 250ms between requests ≈ 4 req/s (well within free-tier limits)
_STOP_CHECK   = 20     # check stop flag every N Last.fm calls

# Generic tags that add no meaningful genre signal
_SKIP_TAGS = frozenset({
    "seen live", "favourites", "favorite", "love", "awesome", "cool",
    "beautiful", "chill", "good", "great", "amazing", "perfect",
    "my favorite", "best", "classic", "under 2000 listeners",
})


async def enrich_music_genres_lastfm(
    user_id: int,
    batch: int = 300,
) -> dict:
    """
    Fetch genres from Last.fm for **Spotify-source** music plays that are still
    missing genres after Phase 1.5.

    Scope is intentionally narrow: only ``source='spotify'`` rows where Phase 1.5
    couldn't write a genre (Spotify rate-limited, Spotify had no metadata, or the
    batch cap blocked them). Plex-rip-only tracks are NOT touched here — they
    get their genres from Plex itself in Phase 1, and from the regular metadata
    enrichment pipeline later. Last.fm is positioned as the safety net for the
    Spotify cascade, not a catch-all.

    One API call per unique (artist, title) pair — results are written back to
    ALL plays for that track so a 300-track batch covers far more than 300 plays.

    Returns {"enriched_plays": N, "tracks_queried": M, "failed": K}
    """
    api_key = settings.LASTFM_API_KEY
    if not api_key:
        return {"error": "LASTFM_API_KEY not configured"}

    # ── Collect Spotify-source rows missing genres ───────────────────────────
    # Query only the columns we need as plain tuples — avoids DetachedInstanceError
    # when accessing attributes outside the session context.
    with get_db_session() as db:
        rows = (
            db.query(
                WatchHistoryEntry.id,
                WatchHistoryEntry.series_title,
                WatchHistoryEntry.title,
            )
            .filter(
                WatchHistoryEntry.user_id    == user_id,
                WatchHistoryEntry.media_type == "music",
                WatchHistoryEntry.source     == "spotify",
                WatchHistoryEntry.genres.is_(None),
            )
            .all()
        )
    # rows is a list of (id, series_title, title) named tuples — session-independent

    if not rows:
        return {"enriched_plays": 0, "tracks_queried": 0, "failed": 0}

    # Group by normalised (artist, title) — store original strings for API call
    # groups: norm_key → (original_artist, original_title, [play_id, ...])
    groups: dict[tuple, tuple] = {}
    for play_id, series_title, title in rows:
        norm_key = (_normalize(series_title or ""), _normalize(title or ""))
        if norm_key not in groups:
            groups[norm_key] = (series_title or "", title or "", [])
        groups[norm_key][2].append(play_id)

    logger.info(
        "[music_matcher] Phase 2 starting: %d unique Spotify-source tracks to enrich "
        "via Last.fm (%d plays), batch=%d",
        len(groups), len(rows), batch,
    )

    # Limit to `batch` unique tracks per run
    unique_tracks = list(groups.items())[:batch]

    # ── Query Last.fm ─────────────────────────────────────────────────────────
    # genre_map: norm_key → "tag1,tag2,..."  (tracks with usable genres)
    # no_data_keys: norm_keys where both track-level AND artist-level queries
    #               returned nothing — written back as genres="" so these tracks
    #               leave the genres-IS-NULL retry pool and don't burn API quota
    #               on every nightly run.
    genre_map:    dict[tuple, str]  = {}
    no_data_keys: set[tuple]        = set()
    failed          = 0
    queried         = 0
    artist_fallback = 0   # tracks resolved via artist.getTopTags instead of track.getInfo

    # Pre-load matching stats once so the progress block can include them
    _total_unmatched = 0
    try:
        with get_db_session() as _db:
            _total_unmatched = _db.query(WatchHistoryEntry).filter(
                WatchHistoryEntry.source == "spotify",
                WatchHistoryEntry.plex_item_id.like("spotify%"),
            ).count()
    except Exception:
        pass

    iterations_done = 0
    async with httpx.AsyncClient(timeout=15) as client:
        for i, (norm_key, (artist, title, _ids)) in enumerate(unique_tracks):
            iterations_done = i + 1
            if not artist or not title:
                failed += 1
                continue

            # Every _STOP_CHECK tracks: update AppState progress + check stop flag
            if i % _STOP_CHECK == 0:
                try:
                    import json as _json
                    from src.services.app_state import set_state as _set_state
                    _set_state("music_pipeline_progress", _json.dumps({
                        "phase":           "lastfm_enrich",
                        "tracks_queried":  queried,
                        "total_unique":    len(unique_tracks),
                        "unmatched":       _total_unmatched,
                        "enriched_plays":  0,   # written to DB at end of batch
                        "artist_fallback": artist_fallback,
                        "pct":            round(100 * queried / max(len(unique_tracks), 1)),
                    }))
                except Exception:
                    pass
                if _stop_requested():
                    logger.info("[music_matcher] Stop requested after %d Last.fm calls", queried)
                    break

            # ── Phase A: track.getInfo ────────────────────────────────────────
            good_tags: list[str] = []
            try:
                resp = await client.get(
                    _LASTFM_BASE,
                    params={
                        "method":      "track.getInfo",
                        "artist":      artist,
                        "track":       title,
                        "api_key":     api_key,
                        "format":      "json",
                        "autocorrect": 1,
                    },
                )
                data = resp.json()
                tags = data.get("track", {}).get("toptags", {}).get("tag", [])
                # Last.fm returns a single tag as a bare dict instead of [dict].
                if isinstance(tags, dict):
                    tags = [tags]
                queried += 1

                good_tags = [
                    t["name"] for t in tags
                    if isinstance(t, dict) and t.get("name") and t["name"].lower() not in _SKIP_TAGS
                ][:5]

            except Exception as exc:
                logger.debug("[music_matcher] track.getInfo failed for %r / %r: %s",
                             artist, title, exc)
                failed += 1

            await asyncio.sleep(_LASTFM_DELAY)

            if good_tags:
                genre_map[norm_key] = ",".join(good_tags)
                continue   # ✓ done — no need for artist fallback

            # ── Phase B: artist.getTopTags fallback ──────────────────────────
            # Many tracks aren't individually indexed on Last.fm (Spotify
            # exclusives, niche artists, title spelling differences).
            # Artist-level tags are coarser but far more available and still
            # carry genre signal (e.g. "indie rock", "electronic", "jazz").
            try:
                resp2 = await client.get(
                    _LASTFM_BASE,
                    params={
                        "method":      "artist.getTopTags",
                        "artist":      artist,
                        "api_key":     api_key,
                        "format":      "json",
                        "autocorrect": 1,
                    },
                )
                data2      = resp2.json()
                artist_tags = data2.get("toptags", {}).get("tag", [])
                if isinstance(artist_tags, dict):
                    artist_tags = [artist_tags]
                queried    += 1

                good_tags = [
                    t["name"] for t in artist_tags
                    if isinstance(t, dict) and t.get("name") and t["name"].lower() not in _SKIP_TAGS
                ][:5]

                if good_tags:
                    genre_map[norm_key] = ",".join(good_tags)
                    artist_fallback += 1
                else:
                    # Both queries returned nothing useful — mark for negative cache
                    no_data_keys.add(norm_key)

            except Exception as exc:
                logger.debug("[music_matcher] artist.getTopTags failed for %r: %s",
                             artist, exc)
                failed += 1
                no_data_keys.add(norm_key)

            await asyncio.sleep(_LASTFM_DELAY)

    stopped_early = iterations_done < len(unique_tracks)
    logger.info(
        "[music_matcher] Phase 2 finished: %d/%d planned tracks processed, "
        "track_hit=%d artist_fallback=%d no_data=%d failed=%d queried=%d, stopped_early=%s",
        iterations_done, len(unique_tracks),
        len(genre_map) - artist_fallback, artist_fallback,
        len(no_data_keys), failed, queried,
        stopped_early,
    )

    # ── Write genres back (batched) ───────────────────────────────────────────
    enriched_plays = 0
    with get_db_session() as db:
        # Tracks with usable genres
        for norm_key, genres_str in genre_map.items():
            play_ids = groups[norm_key][2]
            count = (
                db.query(WatchHistoryEntry)
                .filter(WatchHistoryEntry.id.in_(play_ids))
                .update({"genres": genres_str}, synchronize_session=False)
            )
            enriched_plays += count

        # Tracks with NO usable genres from either source: write empty string so
        # they leave the genres-IS-NULL retry pool.  The empty string is filtered
        # out in genre display code (split(",") → [""] → no tags shown).
        # Re-tries would only waste Last.fm quota since these artists/tracks are
        # consistently absent from Last.fm's database.
        if no_data_keys:
            no_data_ids = [
                pid
                for nk in no_data_keys
                for pid in groups[nk][2]
            ]
            db.query(WatchHistoryEntry).filter(
                WatchHistoryEntry.id.in_(no_data_ids),
                WatchHistoryEntry.genres.is_(None),  # don't overwrite real genres
            ).update({"genres": ""}, synchronize_session=False)
            logger.info("[music_matcher] Wrote no-data sentinel to %d plays (%d tracks)",
                        len(no_data_ids), len(no_data_keys))

        db.commit()

    logger.info("[music_matcher] Last.fm enrichment complete — "
                "plays enriched=%d artist_fallback=%d tracks_queried=%d failed=%d",
                enriched_plays, artist_fallback, queried, failed)

    # Final progress update so the frontend sees the completed state
    try:
        import json as _json
        from src.services.app_state import set_state as _set_state
        _set_state("music_pipeline_progress", _json.dumps({
            "phase":           "lastfm_batch_done",
            "tracks_queried":  queried,
            "total_unique":    len(unique_tracks),
            "enriched_plays":  enriched_plays,
            "artist_fallback": artist_fallback,
            "no_data":         len(no_data_keys),
            "failed":          failed,
            "pct":             100,
        }))
    except Exception:
        pass

    return {
        "enriched_plays":  enriched_plays,
        "tracks_queried":  queried,
        "artist_fallback": artist_fallback,
        "no_data":         len(no_data_keys),
        "failed":          failed,
    }


# ── Combined pipeline (used by scheduler) ────────────────────────────────────

async def run_music_pipeline(user_id: int, batch: int = 300, task=None) -> dict:
    """Run Phase 1 (Plex match) → 1.4 (MBID resolve) → 1.5 (Spotify genres) →
    Phase 2 (Last.fm genres).

    ``batch`` caps unique items per *enrichment* phase per run. Phase 1
    (Plex match) is unaffected — it always processes every unmatched
    Spotify play because the match itself is cheap (string normalisation,
    no API calls). Phases 1.4 / 1.5 / 2 each respect the cap independently.

    Phase 1.4 (Pass 16f) pre-resolves MusicBrainz artist IDs for every
    Spotify-source play so the Spotify-Backlog → Lidarr-add flow is
    instant later. MusicBrainz is rate-limited to ~1 req/s; 200 unique
    artists per run = ~3.5 minutes. The rest carries over to the next
    pipeline iteration.
    """
    logger.info("[music_matcher] Starting full music pipeline for user %d (batch=%d)",
                user_id, batch)

    def _prog(message, done):
        # Activity-card phase progress for the custodian path (the manual
        # router run cards the phases itself). MBID alone is ~1 req/s —
        # minutes at 0% without this.
        if task is None:
            return
        try:
            from src.services.task_monitor import task_monitor
            task_monitor.update(task, message=message, processed=done, total=4)
        except Exception:
            pass

    _prog("Phase 1: matching Spotify plays to Plex tracks…", 0)
    phase1 = await match_spotify_to_plex(user_id)
    if "error" in phase1:
        logger.warning("[music_matcher] Phase 1 aborted: %s", phase1["error"])
    _prog(f"Phase 1.4: resolving artist MBIDs (~1/s, batch {batch})…", 1)
    phase_mbid = await resolve_artist_mbids(user_id, batch=batch)
    _prog(f"Phase 1.5: Spotify genres ({(phase_mbid or {}).get('resolved', 0)} MBIDs resolved)…", 2)
    phase_sp   = await enrich_music_genres_spotify(user_id, batch=batch)
    _prog(f"Phase 2: Last.fm genres ({(phase_sp or {}).get('enriched_plays', 0)} plays enriched)…", 3)
    phase2     = await enrich_music_genres_lastfm(user_id, batch=batch)
    if "error" in phase2:
        logger.warning("[music_matcher] Phase 2 aborted: %s", phase2["error"])
    _prog(f"Done: {(phase2 or {}).get('tracks_queried', 0)} tracks queried on Last.fm", 4)
    return {
        "phase1_plex_match":    phase1,
        "phase1_4_mbid":        phase_mbid,
        "phase1_5_spotify":     phase_sp,
        "phase2_lastfm_genres": phase2,
    }
