"""
scripts/mbid_speedrunner.py — standalone MusicBrainz MBID resolver (Pass 84).

Why this exists
---------------
Phase 1.4 of the daily ``job_music_pipeline`` resolves Spotify-history
artist MBIDs at ``batch=300`` per run. With ~12k unresolved artists in
a typical Spotify-import library that's ~62 days to clear the backlog
at one resolve per day — and during that wait the "Add to Lidarr"
shortcut on the Spotify-Backlog screen has to live-query MB at click
time for any still-unresolved artist (slow, fragile).

This script bypasses the daily cap by calling ``resolve_artist_mbids``
with no batch ceiling. The inner ``fetch_musicbrainz_artist`` already
has the Pass-80 positive + negative cache, so artists previously
queried by ``music_enricher.py`` are sub-ms hits. Only genuinely-new
artists hit MB live (1.1 s/req gap enforced by ``_mb_request``).

Coordination
------------
Sets ``music_pipeline_running="1"`` in AppState while active so the
in-app ``job_music_pipeline`` and ``music_enricher.py`` skip. Refuses
to start if the flag is already set. Releases on clean exit AND on
Ctrl+C / SIGTERM, and clears the stop-request flag on exit so a
trailing "1" can't block the next run.

Usage
-----
    # Resolve every unresolved Spotify-source artist MBID.
    python scripts/mbid_speedrunner.py

    # Cap the run at N unique artists (default 0 = unlimited).
    python scripts/mbid_speedrunner.py --batch 500

    # Run as a specific user_id (default: auto-detect admin).
    python scripts/mbid_speedrunner.py --user-id 1

    # Tee logs to a file.
    python scripts/mbid_speedrunner.py --log-file mbid.log
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import time as _t_mod
from pathlib import Path

# Make ``src.*`` importable when invoked from anywhere, and pin cwd to
# the project root so settings (.env, data/curatarr.db) resolve right.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))
os.chdir(_PROJECT_ROOT)

from src.database.connection import get_db_session  # noqa: E402
from src.database.models import User  # noqa: E402
from src.services.app_state import get_state, set_state, force_set_state  # noqa: E402
from src.services.music_matcher import resolve_artist_mbids  # noqa: E402


logger = logging.getLogger("mbid_speedrunner")


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


# ── coordination flag (mutex with other music runners only — Pass 97) ────────


def acquire_lock() -> tuple[bool, "Optional[str]"]:
    """Try to acquire the standalone-runner mutex.

    Returns ``(success, blocking_flag)`` — see music_enricher.py's
    matching helper for the full rationale and Pass 89 / 94 / 97 history.
    Same change as there: the cross-mutex with ``enrichment_running``
    was dropped in Pass 97. The MBID speedrunner only writes
    ``WatchHistoryEntry.artist_mbid`` rows, and the MusicBrainz
    1-req/s throttle makes DB contention with the in-app enrichment
    consumer negligible.
    """
    if get_state("music_pipeline_running") == "1":
        return False, "music_pipeline_running"
    set_state("music_pipeline_running", "1")
    return True, None


def release_lock() -> None:
    # Pass 89b: bypass SQLAlchemy for the cleanup write — see the matching
    # comment in scripts/music_enricher.py::release_lock for the rationale.
    # Under heavy write-lock contention the engine-routed ``set_state`` fails
    # too, leaving stale flags that block every future pipeline run.
    # ``force_set_state`` opens a fresh sqlite3 connection with a 120 s
    # busy-wait and writes atomically.
    ok_run  = force_set_state("music_pipeline_running", "0")
    ok_stop = force_set_state("music_pipeline_stop_requested", "0")
    if not (ok_run and ok_stop):
        logger.error(
            "[release_lock] FAILED to clear pipeline flags after 120 s busy-wait. "
            "Manual reset: python -c \"from src.services.app_state import "
            "force_set_state; force_set_state('music_pipeline_running', '0')\""
        )


def _resolve_admin_user_id() -> int | None:
    with get_db_session() as db:
        admin = db.query(User).filter(User.is_admin == True).first()  # noqa: E712
        return admin.id if admin else None


# ── main ─────────────────────────────────────────────────────────────────────


async def amain(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Standalone MusicBrainz MBID resolver for Spotify-source plays "
                    "— bypasses the daily Phase 1.4 batch cap.",
    )
    ap.add_argument("--user-id", type=int, default=None,
                    help="Specific user_id to resolve for (default: auto-detect admin).")
    ap.add_argument("--batch", type=int, default=0,
                    help="Cap on unique artists per run (default: 0 = unlimited). "
                         "Cached lookups are sub-ms; only fresh artists pay the "
                         "MB 1.1 s/req throttle in ``_mb_request``.")
    ap.add_argument("--log-file", default=None,
                    help="Tee log output to this file (in addition to stdout).")
    args = ap.parse_args(argv)

    setup_logging(args.log_file)
    logger.info("mbid_speedrunner starting — project root: %s", _PROJECT_ROOT)

    ok, blocker = acquire_lock()
    if not ok:
        # Pass 97: only one possible blocker now (``music_pipeline_running``).
        # The cross-mutex with ``enrichment_running`` was removed so this
        # runner can coexist with the in-app movie/show/anime enrichment.
        logger.error(
            "%s is '1' — another music runner is active (job_music_pipeline "
            "or music_enricher.py). Wait for it to finish, OR clear the "
            "stale flag with:\n"
            "  python -c \"from src.services.app_state import "
            "force_set_state; force_set_state('%s', '0')\"",
            blocker, blocker,
        )
        return 2

    # Ctrl+C — set the AppState stop flag the resolver checks every
    # ``_STOP_CHECK`` iterations (music_matcher.py). This gives us a
    # cooperative cancel: current artist finishes, then the loop exits.
    def _request_stop():
        if get_state("music_pipeline_stop_requested") != "1":
            logger.info("Stop signal received — finishing current artist, then exiting.")
        set_state("music_pipeline_stop_requested", "1")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows: add_signal_handler is not supported — fall back to
            # signal.signal. Coarser (interrupts mid-call) but the per-artist
            # MBID write commits independently, so a hard interrupt is safe.
            signal.signal(sig, lambda *_: _request_stop())

    try:
        user_id = args.user_id or _resolve_admin_user_id()
        if user_id is None:
            logger.error("No admin user found and no --user-id given. Aborting.")
            return 2
        logger.info("Resolving MBIDs for user_id=%d (batch=%s)",
                    user_id, "∞" if args.batch == 0 else args.batch)

        t0 = _t_mod.monotonic()
        result = await resolve_artist_mbids(user_id, batch=args.batch)
        elapsed = _t_mod.monotonic() - t0

        resolved     = result.get("resolved", 0)
        failed       = result.get("failed", 0)
        queried      = result.get("queried", 0)
        total_unique = result.get("total_unique", 0)
        remaining    = result.get("remaining", 0)

        if total_unique == 0:
            logger.info("Nothing to resolve — all Spotify-source artists already have MBIDs.")
        else:
            rate = queried / elapsed if elapsed > 0 else 0
            logger.info(
                "Done in %.1f s @ %.1f artists/s — resolved=%d failed=%d "
                "queried=%d/%d total_unique, %d rolled over (cap).",
                elapsed, rate, resolved, failed, queried, total_unique, remaining,
            )
    finally:
        release_lock()
        logger.info("Released music_pipeline_running flag.")

    return 0


def main() -> int:
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        # add_signal_handler / signal.signal cover the graceful path; this
        # only fires for a very-early Ctrl+C before handlers install.
        return 130


if __name__ == "__main__":
    sys.exit(main())
