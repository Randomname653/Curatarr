"""
scripts/music_enricher.py — standalone music enrichment runner (Pass 77).

Why this exists
---------------
Music enrichment via the in-app pipeline is slow (per-artist MusicBrainz +
Last.fm + Deezer calls, MB rate-limit ~1 req/s) — a full sweep of a few
thousand artists takes hours. Restarting Curatarr kills the in-flight run,
so progress never accumulates.

This script runs OUT-OF-PROCESS: Curatarr restarts don't touch it. It is
LLM-free (only external HTTP APIs), so it does not contend with the curator
or a running game for VRAM — safe to run alongside everything else.

Coordination with the in-app pipeline
-------------------------------------
Sets ``music_pipeline_running="1"`` in AppState while active so the in-app
``job_music_pipeline`` sees the flag and skips. Refuses to start if the
flag is already ``"1"`` (another runner active). Releases the flag on a
clean exit AND on Ctrl+C / SIGTERM.

Each artist commit is atomic and independent, so interruption between
artists is always safe — pick up where you left off on the next run.

Usage
-----
    # Default run: Phase 0 (Spotify track genres) then Phase 1 (artist
    # metadata via MB + Last.fm + Deezer).
    python scripts/music_enricher.py

    # Audit & seed missing rows (watch history + Lidarr), then enrich.
    python scripts/music_enricher.py --seed

    # Reset rows with REAL error markers back to enriched=False, then enrich.
    # Successfully-enriched-but-LLM-pending rows are preserved (Pass 83).
    python scripts/music_enricher.py --retry-errors

    # Stop after enriching N artists (useful for a test run).
    python scripts/music_enricher.py --limit 50

    # Mark each row as fully enriched (no further LLM polish step). The
    # default is to leave the 'api_cached — LLM pending' marker so the next
    # in-app enrichment run picks the row up and does the small-LLM polish
    # step (verified: enrichment.py's pre-filter skips only enriched=True
    # AND error IS NULL — pending-marker rows survive into the next run).
    python scripts/music_enricher.py --final

    # Skip the Spotify Phase 0 (e.g. you already ran it, or want artist-only).
    python scripts/music_enricher.py --skip-spotify

    # Run ONLY the Spotify Phase 0 and exit (no artist enrichment this run).
    python scripts/music_enricher.py --only-spotify

    # Raise the Phase-0 cap from 10000 to power through a big backlog
    # in one go. The internal resolver self-bails on hard rate-limits, so
    # a too-high cap just means an earlier bail-out — not API abuse.
    python scripts/music_enricher.py --spotify-batch 50000

    # Tee logs to a file (default is stdout only).
    python scripts/music_enricher.py --log-file run.log
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import time as _t_mod
from datetime import datetime
from pathlib import Path
from typing import Optional

# Make ``src.*`` importable when invoked from anywhere, and pin cwd to the
# project root so settings (.env, data/curatarr.db, etc.) resolve right.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))
os.chdir(_PROJECT_ROOT)

import httpx  # noqa: E402
from sqlalchemy import func as _func  # noqa: E402

from src.config import settings  # noqa: E402
from src.database.connection import get_db_session  # noqa: E402
from src.database.models import EnrichmentStatus, User, WatchHistoryEntry  # noqa: E402
from src.services.app_state import get_state, set_state, force_set_state  # noqa: E402
from src.services.music_metadata import enrich_artist  # noqa: E402


logger = logging.getLogger("music_enricher")

# MusicBrainz asks for ~1 req/sec for non-commercial use. enrich_artist
# already issues several requests internally (MB artist + MB albums + Last.fm
# + Deezer), so a 1 s gap between ARTISTS is the friendly minimum when the
# inner sub-fetches actually hit the network.
_PER_ARTIST_SLEEP_S = 1.0

# Pass 83b: when the per-artist enrichment returns faster than this
# threshold, EVERY sub-fetch was served from the local api_cache (no live
# HTTP made it out). On NVMe SQLite the round-trip for two cache lookups
# + the profile write is ~30-50 ms; 200 ms is comfortably above that and
# comfortably below the ~1.1 s a cold MB call costs (``_mb_request``
# enforces a 1.1 s gap internally — that's where the politeness lives
# now, not in the outer worker loop). Pure cache iterations skip the
# outer sleep entirely; mixed / live-API iterations still get the full
# 1 s politeness gap.
_CACHE_HIT_THRESHOLD_S = 0.2

_PENDING_LLM_MARKER = "api_cached — LLM pending"


# ── logging ──────────────────────────────────────────────────────────────────


def setup_logging(log_file: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


# ── coordination flag (mutex with in-app pipeline) ───────────────────────────


def acquire_lock() -> tuple[bool, Optional[str]]:
    """Try to acquire the standalone-runner mutex.

    Returns ``(success, blocking_flag)``. ``blocking_flag`` is the name
    of the AppState flag that prevented acquisition (when ``success`` is
    False) or ``None`` (when ``success`` is True).

    Pass 89: refuses to start when EITHER ``music_pipeline_running`` OR
    ``enrichment_running`` is ``"1"``. Both pipelines write to the same
    ``enrichment_status`` table, and pre-89 they could race on the
    SQLite write-lock — long-running batches in either side then
    starved the scheduler/proactive jobs and cascaded into "database is
    locked" errors.

    Pass 94: also return WHICH flag blocked. The pre-94 acquire_lock
    returned a bare bool and the caller's error message was hardcoded
    to "music_pipeline_running" regardless of the real blocker —
    misleading when ``enrichment_running`` was stale and the user spent
    time clearing the wrong flag.
    """
    if get_state("music_pipeline_running") == "1":
        return False, "music_pipeline_running"
    if get_state("enrichment_running") == "1":
        return False, "enrichment_running"
    set_state("music_pipeline_running", "1")
    return True, None


def release_lock() -> None:
    # Pass 89b: bypass SQLAlchemy for the cleanup write. If the engine is
    # in a contention-cascade state — exactly the case where releasing
    # matters most — ``set_state`` would also fail and leave the flag
    # stale, blocking every future pipeline run until manual reset.
    # ``force_set_state`` opens a fresh sqlite3 connection with a 120 s
    # busy-wait, so the release goes through whenever the DB becomes
    # writable in that window.
    ok_run  = force_set_state("music_pipeline_running", "0")
    ok_stop = force_set_state("music_pipeline_stop_requested", "0")
    if not (ok_run and ok_stop):
        logger.error(
            "[release_lock] FAILED to clear pipeline flags after 120 s busy-wait. "
            "Manual reset: python -c \"from src.services.app_state import "
            "force_set_state; force_set_state('music_pipeline_running', '0')\""
        )


# ── seed: walk watch-history + Lidarr, insert missing EnrichmentStatus rows ──


async def _fetch_lidarr_artists() -> list[str]:
    """Return all artistName strings from Lidarr, or [] when not configured."""
    url = getattr(settings, "LIDARR_URL", None)
    key = getattr(settings, "LIDARR_API_KEY", None)
    if not url or not key:
        logger.info("Lidarr not configured — skipping Lidarr seed source")
        return []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{url.rstrip('/')}/api/v1/artist",
                headers={"X-Api-Key": key},
            )
        if r.status_code != 200:
            logger.warning("Lidarr /api/v1/artist returned %s — skipping", r.status_code)
            return []
        return [(a.get("artistName") or "").strip() for a in r.json() if a.get("artistName")]
    except Exception as e:
        logger.warning("Lidarr fetch failed: %s — skipping", e)
        return []


async def seed_missing() -> int:
    """Union watch-history artists + Lidarr artists, insert any not yet in
    EnrichmentStatus. Match by lowercase title so case variants don't
    duplicate. Returns the count of newly-inserted rows.
    """
    # Watch-history artists
    history_lower_to_canonical: dict[str, str] = {}
    with get_db_session() as db:
        rows = (
            db.query(_func.distinct(WatchHistoryEntry.series_title))
            .filter(
                WatchHistoryEntry.media_type == "music",
                WatchHistoryEntry.series_title.isnot(None),
            )
            .all()
        )
        for (name,) in rows:
            name = (name or "").strip()
            if not name:
                continue
            history_lower_to_canonical.setdefault(name.lower(), name)
    logger.info("Watch-history music artists: %d unique", len(history_lower_to_canonical))

    # Lidarr artists (best effort)
    seed_lower_to_canonical = dict(history_lower_to_canonical)
    for name in await _fetch_lidarr_artists():
        seed_lower_to_canonical.setdefault(name.lower(), name)
    extra_from_lidarr = len(seed_lower_to_canonical) - len(history_lower_to_canonical)
    logger.info("Lidarr added: +%d artists (union total: %d)",
                extra_from_lidarr, len(seed_lower_to_canonical))

    # Existing EnrichmentStatus music titles (case-insensitive)
    with get_db_session() as db:
        existing_lower = {
            (row.title or "").lower()
            for row in db.query(EnrichmentStatus.title).filter(
                EnrichmentStatus.media_category == "music"
            ).all()
        }
        inserted = 0
        for lo, canonical in seed_lower_to_canonical.items():
            if lo in existing_lower:
                continue
            # Synthetic plex_rating_key for script-seeded rows; the unique
            # constraint is on plex_rating_key so the prefix guarantees no
            # clash with real Plex ids. The status endpoint counts DISTINCT
            # title, so a later Plex-sync row with the same title doesn't
            # double-count in the UI.
            db.add(EnrichmentStatus(
                plex_rating_key=f"ext-script:music:{lo[:200]}",
                title=canonical,
                media_category="music",
                enriched=False,
            ))
            inserted += 1
        db.commit()
    logger.info("Seeded %d new EnrichmentStatus rows.", inserted)
    return inserted


# ── retry: reset error rows ──────────────────────────────────────────────────


def reset_errors() -> int:
    """Flip rows with a REAL error marker back to enriched=False, error=None.

    Pass 83: explicitly exclude rows that just carry the
    ``_PENDING_LLM_MARKER`` ("api_cached — LLM pending"). Those represent
    SUCCESSFUL enrichment awaiting the in-app LLM polish — see
    ``_mark_enriched(..., final=False)`` above. The pre-83 filter was
    only ``error IS NOT NULL``, which on a 10k+ successful-row library
    wiped the entire run's progress in a single ``--retry-errors`` call
    (user reported "Reset 10654 error rows" after one click — 10,648 of
    those were successful pending-LLM rows, only 6 were real failures).

    A subsequent worker pass will retry the actual error rows. Returns
    the count reset.

    Silver lining if you trip this footgun on an older build: the
    MetadataCache in ``data/cache/metadata.db::api_cache`` still has
    every MB/Last.fm response cached, so the re-run is cache-served
    (≈ 1 artist/sec dominated by politeness sleep) with zero API quota
    cost — just time.
    """
    with get_db_session() as db:
        n = (
            db.query(EnrichmentStatus)
            .filter(
                EnrichmentStatus.media_category == "music",
                EnrichmentStatus.error.isnot(None),
                EnrichmentStatus.error != _PENDING_LLM_MARKER,
            )
            .update({"enriched": False, "error": None}, synchronize_session=False)
        )
        db.commit()
    logger.info("Reset %d real-error rows for retry "
                "(pending-LLM-marker rows preserved).", n)
    return n


# ── Phase 0: Spotify track-genre resolution (Pass 83) ────────────────────────


async def _resolve_admin_user_id() -> Optional[int]:
    """Return the admin user's row id, or None if no admin is configured yet.

    Spotify Client-Credentials uses a single app-level token (no per-user
    OAuth), so the resolved genres are credited to the admin account by
    convention — same attribution model as the Plex-token-driven sync
    paths elsewhere in the codebase.
    """
    with get_db_session() as db:
        admin = db.query(User).filter(User.is_admin == True).first()  # noqa: E712
        return admin.id if admin else None


async def _run_spotify_phase(
    user_id: int,
    batch: int,
    stop_event: asyncio.Event,
) -> dict:
    """Phase 0: resolve Spotify-track genres for spotify-only watch_history rows.

    Why this lives in the standalone runner: ``job_music_pipeline``'s
    daily call processes only ``batch=300`` unique tracks per run, so a
    library with tens of thousands of unresolved Spotify-only tracks
    chips through over months. Running the same logic out-of-process
    with a generous batch knocks the same backlog out in minutes — Spotify
    Client-Credentials tolerates ~180 req/min, and each 50-track API call
    covers 50 unique tracks (plus a deduplicated second hop for artist
    genres). 10 000 unique tracks ≈ ~3 minutes of API time end-to-end.

    Rate-limit handling: we don't add our own — the underlying
    ``resolve_track_genres`` (spotify_client.py) already bails after two
    consecutive 429s or a single Retry-After > 60s and returns
    ``spotify_rate_limited=True`` in the result dict. When that happens,
    we log a clear "moving on" line and return so the artist phase still
    gets to run.

    Returns the raw dict from ``enrich_music_genres_spotify`` (empty
    dict on skip / failure paths).
    """
    if stop_event.is_set():
        logger.info("[spotify] Stop already signalled — skipping Phase 0")
        return {}

    # Reuse the in-app pipeline's resolver — single source of truth for
    # the workset query, batch capping, and write-back logic. Pulling the
    # import in here (vs. at module top) keeps the script importable even
    # on installs that haven't configured Spotify credentials yet.
    try:
        from src.services.music_matcher import enrich_music_genres_spotify
    except Exception as e:
        logger.warning("[spotify] Import failed (%s) — skipping Phase 0", e)
        return {}

    logger.info("[spotify] Phase 0 starting (cap: %d unique tracks)", batch)
    try:
        result = await enrich_music_genres_spotify(user_id, batch=batch)
    except Exception as e:
        logger.warning("[spotify] Phase 0 errored — %s: %s", type(e).__name__, e)
        return {}

    if result.get("note"):
        # Most common note today: "Spotify credentials not configured" —
        # surface it at INFO so the user knows why this phase no-op'd.
        logger.info("[spotify] Phase 0 skipped: %s", result["note"])
        return result

    rate_limited = result.get("spotify_rate_limited", False)
    enriched     = result.get("enriched_plays", 0)
    queried      = result.get("tracks_queried", 0)
    resolved     = result.get("tracks_resolved", 0)
    remaining    = result.get("tracks_remaining", 0)
    total_unique = result.get("tracks_total_unique", 0)

    if rate_limited:
        logger.warning(
            "[spotify] Phase 0 bailed early — Spotify rate-limited. "
            "%d play(s) enriched / %d unique tracks queried / %d remaining "
            "(of %d total unresolved). Moving on to artist phase; the rest "
            "rolls over to the next run.",
            enriched, queried, remaining, total_unique,
        )
    else:
        logger.info(
            "[spotify] Phase 0 done — %d play(s) enriched, %d/%d unique tracks "
            "resolved this run, %d unique tracks still unresolved (queue).",
            enriched, resolved, queried, remaining,
        )
    return result


# ── worker: loop over not-enriched music rows, enrich one at a time ──────────


def _pop_next_row_id() -> tuple[int, str] | None:
    """Return (id, title) of the next not-enriched music artist, or None when
    nothing's left. Selected by id ascending = oldest first. No row is held
    across the enrich call — we re-fetch by id to update.

    Pass 80: also skip rows that already carry an ``error`` marker. Without
    this filter a MISS (artist not in MB nor Last.fm — e.g. "Axwell /\\
    Ingrosso") left ``enriched=False`` and ``error="…no record found"``, so
    on the very next iteration the same lowest-id row came back, hit the
    same failing APIs, and got re-marked — every ~2 s, forever. The log
    showed thousands of identical MISS warnings for one artist in an hour.
    ``--retry-errors`` (``reset_errors``) clears ``error`` and lets rows
    flow through again on the next run.
    """
    with get_db_session() as db:
        row = (
            db.query(EnrichmentStatus.id, EnrichmentStatus.title)
            .filter(
                EnrichmentStatus.media_category == "music",
                EnrichmentStatus.enriched == False,  # noqa: E712
                EnrichmentStatus.error.is_(None),
            )
            .order_by(EnrichmentStatus.id.asc())
            .first()
        )
        if not row:
            return None
        return int(row.id), (row.title or "").strip()


def _mark_enriched(row_id: int, final: bool) -> None:
    """Write the API-data-ready state for an artist.

    ``final=False`` (the default) leaves an "api_cached — LLM pending" marker
    in the ``error`` column so a later in-app enrichment run picks the row
    up and runs the small-LLM polish step (the in-app producer's pre-filter
    skips only ``enriched=True AND error IS NULL``).

    ``final=True`` clears the marker — the row is treated as fully enriched
    and will never be re-touched. Use only when you do NOT want a later LLM
    polish at all.
    """
    with get_db_session() as db:
        es = db.get(EnrichmentStatus, row_id)
        if not es:
            return
        es.enriched = True
        es.enriched_at = datetime.utcnow()
        es.error = None if final else _PENDING_LLM_MARKER
        db.commit()


def _mark_error(row_id: int, exc: BaseException) -> None:
    with get_db_session() as db:
        es = db.get(EnrichmentStatus, row_id)
        if not es:
            return
        es.error = f"music_script: {type(exc).__name__}: {str(exc)[:200]}"
        db.commit()


async def worker(
    limit: int | None,
    final: bool,
    stop_event: asyncio.Event,
) -> int:
    """Process not-enriched music rows one at a time. Returns the count
    processed (successes + failures). ``final`` see ``_mark_enriched``.

    Pass 83b: cache-aware throttling. The outer ``_PER_ARTIST_SLEEP_S``
    sleep used to fire after EVERY artist — wasteful on a full-cache
    re-run (e.g. after a ``--retry-errors`` mistake) where every
    ``enrich_artist`` call returns in ~50 ms from the local SQLite
    api_cache. We now time each call: under ``_CACHE_HIT_THRESHOLD_S``
    means no HTTP made it out (MB + Last.fm + Deezer were all served
    from cache), so we skip the politeness sleep entirely. Live or
    mixed calls still get the full 1 s gap. End-of-run we log the
    fast/slow ratio so the speedup is visible.

    Safety: MB's actual 1 req/sec ceiling is enforced INSIDE
    ``_mb_request`` (the ``_MB_SEM`` lock + 1.1 s minimum gap) — that's
    still in force even when this outer sleep is skipped. The outer
    sleep was only ever a "be friendly to last.fm/deezer too" gesture;
    those tolerate much higher rates, so cache-only iterations don't
    need it.
    """
    processed  = 0
    fast_hits  = 0   # enrich_artist returned in < _CACHE_HIT_THRESHOLD_S
    slow_hits  = 0   # enrich_artist took longer (live API or failure)

    while not stop_event.is_set():
        nxt = _pop_next_row_id()
        if nxt is None:
            logger.info("No more not-enriched music artists. Done.")
            break
        row_id, artist = nxt
        if not artist:
            # Defensive: empty title — skip and mark so we never loop on it.
            with get_db_session() as db:
                es = db.get(EnrichmentStatus, row_id)
                if es:
                    es.enriched = True
                    es.error = "music_script: empty title"
                    db.commit()
            continue

        _t0 = _t_mod.monotonic()
        try:
            data = await enrich_artist(artist)
            if data is None:
                # enrich_artist returned None — no record at any source
                _mark_error(row_id, RuntimeError("no artist record found"))
                logger.warning("[%d] MISS: %s (no record)", processed + 1, artist)
            else:
                _mark_enriched(row_id, final)
                tag = "" if final else " (LLM-pending)"
                tags_preview = ", ".join((data.get("tags") or [])[:3])
                logger.info("[%d] OK:   %s%s — tags: %s",
                            processed + 1, artist, tag, tags_preview or "(none)")
        except Exception as e:
            _mark_error(row_id, e)
            logger.warning("[%d] FAIL: %s: %s: %s",
                           processed + 1, artist, type(e).__name__, e)
        elapsed = _t_mod.monotonic() - _t0

        processed += 1
        if limit and processed >= limit:
            logger.info("Limit reached (%d). Stopping.", limit)
            break

        if elapsed < _CACHE_HIT_THRESHOLD_S:
            # Pure cache hit — no API was contacted. Skip the politeness
            # sleep but still yield to the event loop so a pending Ctrl+C
            # signal gets a chance to flip ``stop_event`` between iterations.
            fast_hits += 1
            await asyncio.sleep(0)
            if stop_event.is_set():
                break
        else:
            # Live or mixed call — full politeness gap. Also serves as the
            # Ctrl+C latency cap (max 1 s before the next stop check).
            slow_hits += 1
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_PER_ARTIST_SLEEP_S)
                # ``wait_for`` returning without TimeoutError = stop signalled.
                break
            except asyncio.TimeoutError:
                pass

    if processed:
        pct_fast = 100 * fast_hits / processed
        logger.info(
            "Cache-hit ratio: %d/%d (%.1f%%) cache-fast, %d slow (live API or fail).",
            fast_hits, processed, pct_fast, slow_hits,
        )
    return processed


# ── main ─────────────────────────────────────────────────────────────────────


async def amain(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Standalone music enrichment runner (LLM-free, restart-safe).",
    )
    ap.add_argument("--seed", action="store_true",
                    help="Audit watch-history + Lidarr and insert missing EnrichmentStatus rows.")
    ap.add_argument("--retry-errors", action="store_true",
                    help="Reset rows with error markers back to enriched=False before the worker runs.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Stop after enriching N artists this run.")
    ap.add_argument("--final", action="store_true",
                    help='Mark rows as fully enriched (clear the error marker). Default '
                         'is to leave "%s" in the error column so the next in-app '
                         'enrichment run picks them up for the small-LLM polish step.'
                         % _PENDING_LLM_MARKER)
    ap.add_argument("--log-file", default=None,
                    help="Tee log output to this file (in addition to stdout).")
    ap.add_argument("--skip-spotify", action="store_true",
                    help="Skip Phase 0 (Spotify track-genre resolution). Default is to "
                         "run Spotify lookups BEFORE the artist phase, with auto-bail "
                         "to the artist phase if Spotify hard-rate-limits.")
    ap.add_argument("--only-spotify", action="store_true",
                    help="Run ONLY Phase 0 (Spotify track-genre resolution) and exit. "
                         "Useful for power-running the Spotify backlog without doing "
                         "any artist enrichment in the same invocation.")
    ap.add_argument("--spotify-batch", type=int, default=10000,
                    help="Cap on unique Spotify track-IDs per Phase 0 run "
                         "(default: 10000). Tracks beyond this cap roll over to the "
                         "next run. Spotify Client-Credentials tolerates ~180 req/min, "
                         "so even 10k unique tracks (~400 API calls split across two "
                         "hops) complete in ~3 minutes when not rate-limited.")
    args = ap.parse_args(argv)

    setup_logging(args.log_file)
    logger.info("music_enricher starting — project root: %s", _PROJECT_ROOT)

    ok, blocker = acquire_lock()
    if not ok:
        # Pass 94: name the ACTUAL blocking flag. Pre-94 this message was
        # hardcoded to "music_pipeline_running" even when the real
        # blocker was the in-app ``enrichment_running`` flag — users
        # then spent time clearing the wrong flag and got the same
        # error on retry.
        if blocker == "enrichment_running":
            logger.error(
                "enrichment_running is '1' — the in-app enrichment consumer "
                "is active (or its flag is stale after a server crash). "
                "Wait for it to finish, OR clear the stale flag with:\n"
                "  python -c \"from src.services.app_state import "
                "force_set_state; force_set_state('enrichment_running', '0')\""
            )
        else:
            logger.error(
                "%s is '1' — another music runner is active (the in-app "
                "job_music_pipeline or another copy of this script). "
                "Wait for it to finish, OR clear the stale flag with:\n"
                "  python -c \"from src.services.app_state import "
                "force_set_state; force_set_state('%s', '0')\"",
                blocker, blocker,
            )
        return 2

    # Signal handlers: finish current artist, then exit cleanly.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop():
        if not stop_event.is_set():
            logger.info("Stop signal received — finishing the current artist, then exiting.")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows: signal handlers via add_signal_handler aren't supported.
            # Fall back to signal.signal — coarser (interrupts mid-API-call) but
            # the per-artist commit means even a hard interrupt is safe.
            signal.signal(sig, lambda *_: _request_stop())

    processed = 0
    try:
        if args.retry_errors:
            reset_errors()
        if args.seed:
            await seed_missing()

        # Pass 83: Phase 0 — Spotify track-genre resolution, runs FIRST so
        # the artist phase always gets a turn even when Spotify is moody.
        # The resolver self-bails on hard rate-limits (see
        # ``_run_spotify_phase`` docstring); we don't need a separate
        # backoff layer here. ``--skip-spotify`` opts out; ``--only-spotify``
        # short-circuits before the artist phase so a fast Spotify-only
        # run is a single flag away.
        if not args.skip_spotify and not stop_event.is_set():
            admin_id = await _resolve_admin_user_id()
            if admin_id is None:
                logger.info("[spotify] No admin user yet — skipping Phase 0")
            else:
                await _run_spotify_phase(admin_id, args.spotify_batch, stop_event)

        if args.only_spotify:
            logger.info("--only-spotify set — exiting after Phase 0 (no artist enrichment).")
        elif stop_event.is_set():
            logger.info("Stop signalled before artist phase — exiting cleanly.")
        else:
            processed = await worker(args.limit, args.final, stop_event)
    finally:
        release_lock()
        logger.info("Released music_pipeline_running flag. Processed %d artist(s) this run.",
                    processed)

    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        # add_signal_handler / signal.signal handle the graceful path; this
        # only catches very-early Ctrl+C before the handler is installed.
        return 130


if __name__ == "__main__":
    sys.exit(main())
