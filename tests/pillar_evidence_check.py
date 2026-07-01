#!/usr/bin/env python3
"""
Pillar clerk + judge check — runs the pipeline against REAL library data.

    python tests/pillar_evidence_check.py            # clerk only (GPU-free)
    python tests/pillar_evidence_check.py --judge     # full: evidence -> verdict -> monologue (uses GPU)

Proves build_evidence() assembles a usable FACTS block (incl. Pillar III), and
with --judge runs adjudicate() + write_monologue() so you can eyeball the real
structured verdict AND the bissige user-facing prose together.
"""
import asyncio
import json
import os
import sys

# Run-from-anywhere: put the repo root (parent of tests/) on the path so `src`
# resolves regardless of cwd — sys.path[0] is the script dir, not the root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows consoles default to cp1252 and crash on non-ASCII (e.g. the ō in
# "Yasujirō Ozu") in the printed facts. Force UTF-8 like build_models.py does.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from sqlalchemy import text

from src.database.connection import get_db_session
from src.database.models import WatchHistoryEntry
from src.services.pillars import build_evidence

OWNER_ID = 1   # admin / owner; the partner is user 2
JUDGE = "--judge" in sys.argv


def _media_item(db, title_like: str, exact: bool = False) -> dict | None:
    """Build an item dict from the raw media_items table (no ORM model)."""
    op = "=" if exact else "LIKE"
    row = db.execute(
        text(f"SELECT title, tmdb_id, tvdb_id, anilist_id, year, genres, overview, "
             f"media_type FROM media_items WHERE title {op} :t LIMIT 1"),
        {"t": title_like},
    ).fetchone()
    if not row:
        return None
    return {
        "title": row[0], "tmdb_id": row[1], "tvdb_id": row[2], "anilist_id": row[3],
        "year": row[4], "genres": row[5], "overview": row[6],
        "media_type": row[7] or "movie",
    }


async def _show(db, item, category, label):
    print("=" * 72)
    print(f"### {label}: {item.get('title')!r}  (category={category})")
    ev = await build_evidence(item, user_id=OWNER_ID, category=category, db=db)
    print("FLAGS:", ev["flags"])
    print("-" * 72)
    print(ev["facts"])
    if JUDGE:
        from src.services.pillars import adjudicate, write_monologue
        print("-" * 72)
        print("... STAGE 1: verdict (constitution + schema, temp 0) ...")
        verdict = await adjudicate(ev["facts"])
        print("VERDICT:\n" + json.dumps(verdict, indent=2, ensure_ascii=False))
        print("... STAGE 2: monologue (lazy persona call) ...")
        mono = await write_monologue(ev["facts"], verdict)
        print("MONOLOGUE:\n" + (mono or "(empty)"))
    print()


async def run():
    with get_db_session() as db:
        # ── CASE A: acclaim + (if tech present) bitrate, owner-unwatched ──
        ts = _media_item(db, "%Tokyo Story%")
        if ts is None:
            ts = {"title": "Tokyo Story", "year": 1953, "genres": "Drama",
                  "media_type": "movie"}   # title-only fallback
        await _show(db, ts, ts.get("media_type") or "movie", "CASE A — acclaim")

        # ── CASE B: a title the PARTNER (user 2) actually watched (Pillar III) ──
        wh = (db.query(WatchHistoryEntry)
                .filter(WatchHistoryEntry.user_id == 2)
                .order_by(WatchHistoryEntry.viewed_at.desc()).first())
        if wh:
            wtitle = wh.series_title or wh.title
            item = _media_item(db, wtitle, exact=True) or {
                "title": wtitle, "tmdb_id": wh.tmdb_id,
                "media_type": wh.media_type, "genres": wh.genres,
            }
            await _show(db, item, item.get("media_type") or "movie",
                        "CASE B — partner-watched (Pillar III)")
        else:
            print("(no user-2 watch history found — Pillar III case skipped)")


if __name__ == "__main__":
    asyncio.run(run())
