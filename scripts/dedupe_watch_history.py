"""Repair watch-history rows that record one viewing twice.

    python scripts/dedupe_watch_history.py          # dry run (default)
    python scripts/dedupe_watch_history.py --apply  # actually delete

Two historical artifacts inflate play counts, and both read downstream as
"the user keeps rewatching this":

1. RESUMED VIEWS. Plex logs a partial view, then logs the same item again as
   finished once it crosses the watched threshold. Those arrive through two
   different queries and nothing reconciled them, so one viewing left two
   rows behind. plex_sync now promotes the unfinished row instead (see
   RESUME_WINDOW_DAYS there); this cleans up what it already wrote.

2. TIMEZONE RE-IMPORTS. A re-sync re-imported viewings with their timestamps
   shifted by a whole hour, which slipped past the (user, item, viewed_at)
   dedup key and created a parallel set of rows. Recognised by an exact
   1 h or 2 h gap AND identical progress — a real second attempt does not
   stop at the same millisecond.

Only unfinished rows are ever deleted; a row marking a completed view is
never touched. Run with the app stopped if you want to be strict, though the
transaction is small enough for WAL to absorb.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.database.connection import get_db_session          # noqa: E402
from src.database.models import WatchHistoryEntry           # noqa: E402
from src.services.plex_sync import RESUME_WINDOW_DAYS       # noqa: E402

SHIFT_SECONDS = (3600, 7200)
SHIFT_TOLERANCE = 2


def find_resumed(rows):
    """Unfinished rows a later completed row already accounts for."""
    completed = {}
    for r in rows:
        if r.completed:
            completed.setdefault((r.user_id, r.plex_item_id), []).append(r.viewed_at)
    doomed = []
    for r in rows:
        if r.completed or r.viewed_at is None:
            continue
        for done_at in completed.get((r.user_id, r.plex_item_id), ()):
            if done_at is None or done_at < r.viewed_at:
                continue
            if (done_at - r.viewed_at).days <= RESUME_WINDOW_DAYS:
                doomed.append((r, f"finished view at {done_at:%Y-%m-%d %H:%M}"))
                break
    return doomed


def find_timezone_twins(rows):
    """One of each pair of unfinished rows that differ by a whole-hour shift."""
    by_item = {}
    for r in rows:
        if not r.completed and r.viewed_at is not None:
            by_item.setdefault((r.user_id, r.plex_item_id), []).append(r)
    doomed = []
    for group in by_item.values():
        group.sort(key=lambda r: r.viewed_at)
        kept = []
        for r in group:
            twin = next(
                (k for k in kept
                 if k.view_offset_ms == r.view_offset_ms
                 and any(abs(abs((r.viewed_at - k.viewed_at).total_seconds()) - s)
                         <= SHIFT_TOLERANCE for s in SHIFT_SECONDS)),
                None)
            if twin is not None:
                doomed.append((r, f"{abs((r.viewed_at - twin.viewed_at).total_seconds()) / 3600:.0f} h "
                                  f"shift of row {twin.id}"))
            else:
                kept.append(r)
    return doomed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="delete instead of reporting")
    ap.add_argument("--include-music", action="store_true",
                    help="also repair music rows (default: video only)")
    args = ap.parse_args()

    with get_db_session() as db:
        q = db.query(WatchHistoryEntry)
        if not args.include_music:
            q = q.filter(WatchHistoryEntry.media_type.notin_(
                ("track", "music", "artist", "album")))
        rows = q.all()
        print(f"scanning {len(rows)} rows"
              f"{'' if args.include_music else ' (video only)'}\n")

        doomed = {}
        for row, why in find_resumed(rows):
            doomed[row.id] = (row, "resumed view", why)
        for row, why in find_timezone_twins(rows):
            doomed.setdefault(row.id, (row, "timezone twin", why))

        if not doomed:
            print("nothing to repair")
            return 0

        for _id, (row, kind, why) in sorted(doomed.items()):
            label = row.series_title or row.title
            ep = f" S{row.season}E{row.episode}" if row.episode is not None else ""
            print(f"  [{kind:14s}] id={row.id:<7} {str(label)[:34]:36s}{ep:9s}"
                  f" {row.viewed_at:%Y-%m-%d %H:%M}  ({why})")

        print(f"\n{len(doomed)} unfinished row(s) identified")
        if not args.apply:
            print("dry run — re-run with --apply to delete them")
            return 0

        for _id, (row, _kind, _why) in doomed.items():
            db.delete(row)
        db.commit()
        print(f"deleted {len(doomed)} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
