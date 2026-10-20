"""
Curatarr - Re-Embed Backfill (now a wrapper around the daily reconcile)
======================================================================

DEPRECATED as a standalone tool (2026-10). The app does this itself: the
custodian task ``vector_reconcile`` (src/services/vector_reconcile.py)
finds every enriched row without a ChromaDB doc and re-embeds it from the
cached profile, daily and at the first tick after a start that sees the two
stores disagree. Run this script only to drain that backlog in one go,
e.g. right after restoring curatarr.db over an empty vector store.

The old implementation looked profiles up as ``enriched:{cat}:{plex key}``,
but profiles are cached under the external id (``enriched:{cat}:{tmdb or
anilist or ... id}``), so it found almost none and reported them "no cache
entry". It now runs the same pass the app runs, which joins through each
profile's own ``plex_rating_key`` (and title), until nothing is left or a
pass makes no progress. Its old flags (--dry-run, --limit, --category,
--batch) are gone.

Run with the Curatarr app STOPPED — ChromaDB's PersistentClient is
single-writer (the wrapper refuses a second process), and the SQLite DB
shouldn't be written by two processes at once.

Usage
-----
    python scripts/revectorize_backfill.py                 # drain the backlog
    python scripts/revectorize_backfill.py --max-passes 5  # stop after 5 passes
"""

import argparse
import asyncio
import logging
import sys

sys.path.insert(0, ".")

# Windows consoles default to cp1252 and choke on the progress glyphs.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s  %(levelname)s  %(name)s — %(message)s")

_REMOVED_FLAGS = ("--dry-run", "--limit", "--category", "--batch")


async def main(max_passes: int) -> int:
    from src.services.data_custodian import TaskFailed
    from src.services.vector_reconcile import reconcile_vectors

    for n in range(1, max_passes + 1):
        report: dict = {}
        try:
            done = await reconcile_vectors(report=report)
        except TaskFailed as e:
            print(f"pass {n}: FAILED — {e}")
            return 1
        print(f"pass {n}: {report.get('summary', 'no enriched rows')}", flush=True)
        if done:
            print("\nDone: every enriched row has its vector (or was re-queued for "
                  "enrichment, which the app's next enrichment cycle rebuilds).\n")
            return 0
        if not report.get("reembedded") and not report.get("requeued"):
            print(f"\nStopped: pass {n} made no progress ({report.get('left', '?')} left). "
                  "Is the embedding model available in Ollama?\n")
            return 1
    print(f"\nStopped after {max_passes} passes; the app's daily reconcile "
          "continues from here.\n")
    return 0


if __name__ == "__main__":
    print("NOTE: revectorize_backfill.py is deprecated — the app's daily "
          "'vector_reconcile' maintenance task does this on its own.\n")
    used = [a for a in sys.argv[1:] if a.split("=", 1)[0] in _REMOVED_FLAGS]
    if used:
        sys.exit(f"{', '.join(used)}: removed. The script now runs the app's "
                 "reconcile pass (see the docstring); the Activity view and "
                 "the log show what each pass did.")
    ap = argparse.ArgumentParser(description="Drain the vector reconcile backlog now.")
    ap.add_argument("--max-passes", type=int, default=1000,
                    help="Stop after this many passes (each re-embeds up to 300 profiles).")
    sys.exit(asyncio.run(main(ap.parse_args().max_passes)))
