"""The Wrap-Up's comebacks look-up (wrapup_facts._comebacks) must find a title's
last earlier play through the title indexes, not by walking every earlier play
of the member: `coalesce(series_title, title) IN (...)` cannot use an index
(1.9 s for a week on a 384k-play table, 0.3 s with the spelled-out OR)."""
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

    rows = [row(i, f"track {i}", "music", now - timedelta(days=400 + i % 300), f"artist {i % 40}")
            for i in range(4000)]
    rows += [row(9001, "Old Film", "movie", now - timedelta(days=700)),             # film: no series_title
             row(9002, "Old Film", "movie", now - timedelta(days=2)),
             row(9003, "ep 1", "show", now - timedelta(days=900), "Old Show"),      # series: keyed by series
             row(9004, "ep 9", "show", now - timedelta(days=1), "Old Show"),
             row(9005, "Recent Film", "movie", now - timedelta(days=100)),          # gap < a year
             row(9006, "Recent Film", "movie", now - timedelta(days=1)),
             # played inside the year before: no comeback, and never worth the max() look-up
             row(9007, "Regular", "movie", now - timedelta(days=40)),
             row(9008, "Regular", "movie", now - timedelta(days=2)),
             # just under / just over a year of silence before the week's first play
             row(9009, "Edge Under", "movie", now - timedelta(days=364 + 2)),
             row(9010, "Edge Under", "movie", now - timedelta(days=2)),
             row(9011, "Edge Over", "movie", now - timedelta(days=366 + 2)),
             row(9012, "Edge Over", "movie", now - timedelta(days=2)),
             # exactly a year: the latest first play of the week minus 365 days
             row(9013, "Edge Exact", "movie", now - timedelta(days=365 + 1)),
             row(9014, "Edge Exact", "movie", now - timedelta(days=1))]
    db.bulk_insert_mappings(W, rows)
    db.commit()
with conn.engine.begin() as c:
    c.execute(text("ANALYZE"))      # production has planner stats (_refresh_planner_stats)

statements = []

@event.listens_for(conn.engine, "before_cursor_execute")
def _grab(c, cur, stmt, params, *a):
    statements.append((stmt, params))

period = wf.resolve_period("week:2026-W46", now, timezone.utc)
with conn.get_db_session() as db:
    out = wf._comebacks(db, W, uid, period, wf.scope_types("all"))
got = {c["title"]: c["gap_days"] for c in out}
check("a film with no series_title comes back after 700 days", got.get("Old Film") == 698)
check("a series comes back keyed by series_title", got.get("Old Show") == 899)
check("a gap under a year is no comeback", "Recent Film" not in got)
check("a title played the year before is no comeback", "Regular" not in got)
check("364 days of silence is no comeback, 366 is", "Edge Under" not in got and got.get("Edge Over") == 366)

stmt, params = next(s for s in statements if "max(watch_history.viewed_at)" in s[0])
check("titles played within the year are not looked up at all",
      not {"Regular", "Recent Film"} & set(params))
# Exactly a year before the latest first play (now - 1 day - 365): still a
# comeback (gap >= 365), so the pre-filter must keep it for the look-up. It
# is the fourth-largest gap here, so the top three leave it out of ``got``.
check("a play exactly a year before is looked up, not filtered away (> cutoff, not >=)",
      "Edge Exact" in set(params), params)
raw = conn.engine.raw_connection()
plan = " ".join(r[3] for r in raw.cursor().execute("EXPLAIN QUERY PLAN " + stmt, params))
raw.close()
check("the earlier-play look-up uses the title indexes",
      "ix_wh_series_title" in plan and "ix_wh_title" in plan, plan)
check.done()
