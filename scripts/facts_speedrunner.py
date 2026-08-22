"""
scripts/facts_speedrunner.py — clear the archive-metadata backlog in one go.

Why this exists
---------------
Four sources feed the pillar that decides whether a title has objective
stature: Wikipedia significance (distilled), community reception, Wikidata
facts and OMDb ratings. The background custodian fills them at ~150 titles a
day, which is the right pace for a library that has been running for months
and a horizon for one set up this week — and every verdict passed in the
meantime is reasoned from whatever happened to be there.

Same walkers, no daily ceiling. The Knowledge Base offers the same thing per
source with a button; this is the version you can leave running overnight.

What it does NOT need
---------------------
The vector store — only the SQLite metadata cache, HTTP, and (for
significance and reception) the summariser. So it runs beside the app,
unlike the test battery.

What it DOES share
------------------
The GPU. ``--skip-significance`` leaves the summariser alone for the sources
that are pure lookups, which is the mode to use during a game.

Usage
-----
    python scripts/facts_speedrunner.py                  # everything
    python scripts/facts_speedrunner.py --skip-significance
    python scripts/facts_speedrunner.py --only wikidata
    python scripts/facts_speedrunner.py --limit 25 --dry-run

Ctrl+C stops after the current title; everything written stays written and
re-running resumes.
"""
import argparse
import asyncio
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.cache.metadata_cache import MetadataCache             # noqa: E402
from src.services.archive_backfill import coverage, run_source  # noqa: E402

_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    print("\n  stopping after the current title — progress is already saved")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="per source; 0 = no ceiling")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-significance", action="store_true",
                    help="no summariser work — safe beside a game")
    ap.add_argument("--only", help="one source key: significance, reception, "
                                   "wikidata, omdb")
    args = ap.parse_args()
    signal.signal(signal.SIGINT, _on_signal)

    cov = coverage()
    print(f"library: {cov['total']} video titles\n")
    order = []
    for src in cov["sources"]:
        print(f"  {src['label']:26s} {src['pct']:5.1f}%   "
              f"{src['missing']:6d} to go")
        if args.only and src["key"] != args.only:
            continue
        if args.skip_significance and src["key"] == "significance":
            continue
        if src["missing"]:
            order.append(src["key"])

    if not order:
        print("\n  nothing to do")
        return 0
    if args.dry_run:
        print(f"\n  would run: {', '.join(order)}  (dry run — nothing written)")
        return 0

    cache = MetadataCache()
    try:
        for key in order:
            if _stop:
                break
            label = next(s["label"] for s in cov["sources"] if s["key"] == key)
            print(f"\n-- {label} --")
            t0 = time.monotonic()
            res = await run_source(key, limit=args.limit, cache=cache,
                                   should_stop=lambda: _stop)
            secs = max(time.monotonic() - t0, 1e-9)
            print(f"   visited {res['visited']}, added {res['added']}"
                  f"  ({res['visited'] / secs * 60:.0f}/min)")
    finally:
        cache.close()

    print("\n-- coverage now --")
    for src in coverage()["sources"]:
        tail = "" if src["offer"] else "   (the daily ticks can finish this)"
        print(f"  {src['label']:26s} {src['pct']:5.1f}%{tail}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
