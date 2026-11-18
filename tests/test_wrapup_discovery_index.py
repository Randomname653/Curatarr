"""The Wrap-Up's discovery look-up (wrapup_facts._discovery) asks "was this title
played before the period?" with one probe per title that stops at the first hit,
through the title indexes. `coalesce(series_title, title) IN (...)` plus DISTINCT
walked every earlier play of a regular artist (0.6 s a week on a 384k-play table,
0.04 s probing)."""
from _harness import app_db, check

conn = app_db()
from datetime import datetime, timedelta, timezone  # noqa: E402

from sqlalchemy import event, text  # noqa: E402

import src.services.wrapup_facts as wf  # noqa: E402
from src.database.models import User, WatchHistoryEntry as W  # noqa: E402

now = datetime(2026, 11, 15, 12, 0)
with conn.get_db_session() as db:
    u = User(plex_user_id="p1", plex_username="u1")
    db.add(u)
    db.flush()
    uid = u.id

    def row(i, title, mt, at, series=None):
        return dict(user_id=uid, plex_user_id="p1", plex_item_id=f"i{i}", title=title,
                    media_type=mt, series_title=series, viewed_at=at, completed=True)

    rows = [row(i, f"track {i}", "music", now - timedelta(days=30 + i % 300), f"artist {i % 40}")
            for i in range(4000)]
    rows += [row(9001, "Old Film", "movie", now - timedelta(days=50)),
             row(9002, "Old Film", "movie", now - timedelta(days=2)),
             row(9003, "ep 1", "show", now - timedelta(days=60), "Old Show"),
             row(9004, "ep 9", "show", now - timedelta(days=1), "Old Show"),
             row(9005, "New Film", "movie", now - timedelta(days=1)),
             row(9006, "Other", "movie", now - timedelta(days=50), "Old Film")]  # series_title shadows title
    db.bulk_insert_mappings(W, rows)
    db.commit()
with conn.engine.begin() as c:
    c.execute(text("ANALYZE"))

statements = []

@event.listens_for(conn.engine, "before_cursor_execute")
def _grab(c, cur, stmt, params, *a):
    statements.append((stmt, params))

period = wf.resolve_period("week:2026-W46", now, timezone.utc)
titles = [{"title": t, "hours": 1.0} for t in ("Old Film", "Old Show", "New Film", "artist 3")]
with conn.get_db_session() as db:
    out = wf._discovery(db, W, uid, period, wf.scope_types("all"), titles)
check("only the title never played before is new", out["new_titles"] == 1 and out["best_new"] == "New Film", out)
check("returning titles counted (film, series, artist)", out["returning_titles"] == 3, out)

probe = next((s for s in statements if "watch_history.viewed_at <" in s[0] and "LIMIT" in s[0]), None)
check("one first-hit probe per title, not a DISTINCT walk", probe is not None)
if probe is None:
    check.done()
raw = conn.engine.raw_connection()
plan = " ".join(r[3] for r in raw.cursor().execute("EXPLAIN QUERY PLAN " + probe[0], probe[1]))
raw.close()
check("the look-up is an index search on both title columns, no scan",
      "ix_wh_title" in plan and "SEARCH" in plan and "SCAN" not in plan, plan)
check.done()
