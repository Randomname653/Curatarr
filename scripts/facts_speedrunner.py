"""
scripts/facts_speedrunner.py — clear the archive-pillar backlog in one go.

Why this exists
---------------
Three sources feed the pillar that decides whether a title has objective
stature: Wikipedia significance (distilled), community reception, and
Wikidata facts. All three are filled by a background walker that processes
150 titles a day. On a library of this size that is not a backlog, it is a
horizon — and every title reached late is judged without them in the
meantime.

Worse, significance carries a version stamp (``_SIG_PROMPT_VERSION``): when
the distillation rules improve, every answer written under the old rules is
retired and has to be redone. That is correct, and it is also thousands of
titles the walker will not reach this year.

This script does the same work with no daily ceiling.

What it does NOT need
---------------------
The vector store. All three top-ups touch only the SQLite metadata cache,
HTTP, and (for significance) the summariser — so this runs happily while the
app is running, unlike the test battery.

What it DOES share
------------------
The GPU, for significance. ``--skip-significance`` leaves the summariser
alone entirely: reception and Wikidata are plain HTTP, so that mode is safe
to run during a game or a long chat session.

Usage
-----
    # Everything, no ceiling. Resumable: stop and re-run any time.
    python scripts/facts_speedrunner.py

    # No GPU work — reception + Wikidata only.
    python scripts/facts_speedrunner.py --skip-significance

    # A capped trial run, nothing written.
    python scripts/facts_speedrunner.py --limit 25 --dry-run
"""
import argparse
import asyncio
import json
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.cache.metadata_cache import MetadataCache          # noqa: E402
from src.services.media_enricher import (                    # noqa: E402
    _SIG_PROMPT_VERSION, topup_significance)
from src.services.reception import topup_reception           # noqa: E402
from src.services.wikidata import topup_wikidata, _LOGIC_VERSION  # noqa: E402

VIDEO = ("movie", "show", "anime")
_stop = False


def _on_signal(*_):
    global _stop
    _stop = True
    print("\n  stopping after the current title — progress is already saved")


def _candidates(cache) -> list[dict]:
    """Every live raw entry, with what each one is still missing."""
    cur = cache.conn.execute(
        """
        SELECT cache_key, response FROM api_cache
        WHERE (cache_key LIKE 'v2:raw:%' OR cache_key LIKE 'raw:%')
          AND expires_at > datetime('now')
        """
    )
    seen, out = set(), []
    for key, resp in cur.fetchall():
        try:
            raw = json.loads(resp)
        except Exception:
            continue
        title, mtype = raw.get("title"), raw.get("media_type")
        # raw entries store TMDB's own vocabulary ("tv"), the top-ups want the
        # app's categories.
        mtype = {"tv": "show"}.get(mtype, mtype)
        if not title or mtype not in VIDEO:
            continue
        ident = (title, mtype)
        if ident in seen:
            continue
        seen.add(ident)
        out.append({
            "title": title,
            "media_type": mtype,
            "tmdb_id": raw.get("tmdb_id"),
            "tvdb_id": raw.get("tvdb_id"),
            "imdb_id": raw.get("imdb_id"),
            "year": raw.get("year"),
            "need_sig": raw.get("significance_v") != _SIG_PROMPT_VERSION,
            "need_rec": not raw.get("reception_checked"),
            "need_wd": raw.get("wikidata_v") != _LOGIC_VERSION,
        })
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = no ceiling")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-significance", action="store_true",
                    help="no summariser work — safe beside a game or a chat")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _on_signal)

    cache = MetadataCache()
    try:
        todo = _candidates(cache)
        want = [t for t in todo
                if (t["need_sig"] and not args.skip_significance)
                or t["need_rec"] or t["need_wd"]]
        print(f"live raw entries (video): {len(todo)}")
        print(f"  missing significance : {sum(t['need_sig'] for t in todo)}"
              f"{'   (skipped)' if args.skip_significance else ''}")
        print(f"  missing reception    : {sum(t['need_rec'] for t in todo)}")
        print(f"  missing wikidata     : {sum(t['need_wd'] for t in todo)}")
        print(f"  titles to visit      : {len(want)}\n")
        if args.dry_run:
            for t in want[:15]:
                miss = [n for n, f in (("sig", t["need_sig"]), ("rec", t["need_rec"]),
                                       ("wd", t["need_wd"])) if f]
                print(f"    {t['title'][:44]:46s} {t['media_type']:6s} {','.join(miss)}")
            print("\n  dry run — nothing written")
            return 0

        if args.limit:
            want = want[:args.limit]
        got = {"sig": 0, "rec": 0, "wd": 0}
        t0 = time.monotonic()
        for i, t in enumerate(want, 1):
            if _stop:
                break
            ids = dict(tmdb_id=t["tmdb_id"], tvdb_id=t["tvdb_id"])
            if t["need_wd"]:
                try:
                    got["wd"] += bool(await topup_wikidata(
                        t["title"], t["media_type"], imdb_id=t["imdb_id"],
                        cache=cache, **ids))
                except Exception:
                    pass
            if t["need_rec"]:
                try:
                    got["rec"] += bool(await topup_reception(
                        t["title"], t["media_type"], cache=cache,
                        year=t["year"], **ids))
                except Exception:
                    pass
            if t["need_sig"] and not args.skip_significance:
                try:
                    got["sig"] += bool(await topup_significance(
                        t["title"], t["media_type"], cache=cache,
                        year=t["year"], **ids))
                except Exception:
                    pass
            if i % 25 == 0 or i == len(want):
                rate = i / max(time.monotonic() - t0, 1e-9)
                left = (len(want) - i) / rate if rate else 0
                print(f"  {i}/{len(want)}  +{got['sig']} significance "
                      f"+{got['rec']} reception +{got['wd']} on-record "
                      f"— {rate*60:.0f}/min, ~{left/60:.0f} min left")
        print(f"\ndone: +{got['sig']} significance, +{got['rec']} reception, "
              f"+{got['wd']} on-record facts")
        return 0
    finally:
        cache.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
