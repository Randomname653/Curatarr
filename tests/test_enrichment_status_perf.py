"""The enrichment page's status poll stays cheap.

NAS log, November 2026: GET /api/enrichment/status took 20-48 s (3 s at the
end of October). Measured on a copy of the owner's data: four counts of
"watched titles enriched" ran 1.1 s each, because SQLite walked
ix_wh_series_title over every titled play (380k, nearly all music) for a
show or anime count; and every poll read all ~32k vector ids, which also
kept the collection loaded so the idle release never came while the page
was open. With a covering index (user, family, series, orphaned) the counts
take a millisecond; the vector counts are scanned again only after a write.

- every count the endpoint runs on watch_history uses an index, none walks
  the series-title index or the table (the real endpoint's statements,
  recorded and EXPLAINed);
- the vector counts read the store once, then again only after a write.

    python tests/test_enrichment_status_perf.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio
from datetime import datetime

from sqlalchemy import event, text

conn = app_db()
from src.database.models import User, WatchHistoryEntry as W  # noqa: E402

with conn.get_db_session() as db:
    db.add(User(id=1, plex_user_id="p1", plex_username="u1", is_admin=True))
    db.flush()
    for i in range(30):
        db.add(W(user_id=1, plex_user_id="p1", plex_item_id=f"k{i}", title=f"T{i}",
                 series_title=f"S{i % 7}", media_type=("anime", "show", "music")[i % 3],
                 viewed_at=datetime(2026, 11, 1), completed=True))
    db.commit()

# The planner decides on statistics: thirty test rows alone plan differently
# from the owner's table. These are the owner's own (sqlite_stat1, a copy of
# 2026-11-13: 384k plays, one user with half of them, ~21 plays per titled
# series) - row counts only, no data - so the test plans as the NAS did.
REAL_STATS = [
    ("watch_history", "idx_wh_user_item", "384002 192001 5"),
    ("watch_history", "ix_watch_history_user_id", "384002 192001"),
    ("watch_history", "ix_watch_history_viewed_at", "384002 2"),
    ("watch_history", "ix_wh_series_title", "384002 21"),
    ("watch_history", "ix_wh_series_title_lc", "384002 22"),
    ("watch_history", "ix_wh_title", "384002 6"),
    ("watch_history", "ix_wh_tmdb_id", "384002 11637"),
    ("watch_history", "ix_wh_user_type_viewed", "384002 192001 54858 2"),
    ("watch_history", "ix_wh_user_type_series", "384002 192001 54858 21 21"),
    ("enrichment_status", "sqlite_autoindex_enrichment_status_1", "44389 1"),
]
with conn.engine.begin() as c:
    c.execute(text("ANALYZE"))
    c.execute(text("DELETE FROM sqlite_stat1"))
    for tbl, idx, stat in REAL_STATS:
        c.execute(text("INSERT INTO sqlite_stat1 (tbl, idx, stat) VALUES (:t, :i, :s)"),
                  {"t": tbl, "i": idx, "s": stat})
conn.engine.dispose()       # new connections read the statistics

with conn.engine.connect() as c:
    cols = [r[2] for r in c.execute(text("PRAGMA index_info(ix_wh_user_type_series)"))]
check("the covering index exists: user, family, series, orphaned",
      cols == ["user_id", "media_type", "series_title", "orphaned_at"], cols)

seen: list = []


def _rec(c, cur, stmt, params, ctx, many):
    if "watch_history" in stmt and stmt.lstrip().upper().startswith("SELECT"):
        seen.append((stmt, params))

event.listen(conn.engine, "before_cursor_execute", _rec)
import src.routers.enrichment as en  # noqa: E402
fn = en.enrichment_status
while hasattr(fn, "__wrapped__"):
    fn = fn.__wrapped__
asyncio.run(fn(user=User(id=1, plex_user_id="p1", plex_username="u1", is_admin=True), quick=True))
event.remove(conn.engine, "before_cursor_execute", _rec)
check("the status poll counts on watch_history", len(seen) >= 4, len(seen))

bad = []
with conn.engine.connect() as c:
    for stmt, params in seen:
        plan = " | ".join(r[3] for r in c.exec_driver_sql("EXPLAIN QUERY PLAN " + stmt, params))
        wh = [p for p in plan.split(" | ") if "watch_history" in p]
        if any("ix_wh_series_title" in p or p.startswith("SCAN watch_history") for p in wh):
            bad.append(plan)
check("no count walks the series-title index or the whole table (the 1.1 s plan)", not bad, bad)

# ── the vector counts ────────────────────────────────────────────────────────
from src.vector_store import chromadb_wrapper as cw  # noqa: E402

scans = []


class _Store:
    def all_ids(self):
        scans.append(1)
        return ["radarr:1", "radarr:2", "sonarr:5", "lidarr:9", "spotify:x"]

cw.get_chroma_db = lambda: _Store()
en._vec_counts_cache.clear()
a = en._vector_counts_by_service()
b = en._vector_counts_by_service()
check("the vector counts per service", a == {"radarr": 2, "sonarr": 1, "lidarr": 1}, a)
check("...read the store once, the next poll answers from memory", b == a and len(scans) == 1, scans)
cw._bump_domain_gen()
en._vector_counts_by_service()
check("...and again after a write", len(scans) == 2, scans)

check.done()
