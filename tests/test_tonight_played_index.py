"""Tonight's "played since" check must not walk the owner's whole history:
a film is looked up by lower(title), which had no index (55-63 ms per
candidate on a 384k-row table, ~0.1 ms with ix_wh_title_lc)."""
from _harness import app_db, check

conn = app_db()
from datetime import datetime, timedelta  # noqa: E402

from sqlalchemy import func, text  # noqa: E402

from src.database.models import CachedRecommendation as R, User, WatchHistoryEntry as W  # noqa: E402

now = datetime(2026, 11, 14, 20, 0)
with conn.get_db_session() as db:
    u = User(plex_user_id="p1", plex_username="u1")
    db.add(u)
    db.flush()
    uid = u.id
    db.bulk_insert_mappings(W, [
        dict(user_id=uid, plex_user_id="p1", plex_item_id=str(i), title=f"track {i}",
             media_type="music", series_title=f"artist {i % 50}",
             viewed_at=now - timedelta(minutes=i)) for i in range(3000)]
        + [dict(user_id=uid, plex_user_id="p1", plex_item_id="f1", title="Seen Film",
                media_type="movie", viewed_at=now - timedelta(days=2))])
    for title, lane in (("Seen Film", "library"), ("Fresh Film", "library")):
        db.add(R(user_id=uid, title=title, category="movie", lane=lane, reason="r",
                 confidence=0.5, cached_at=now - timedelta(days=5), lifted_at=now))
    db.commit()

with conn.engine.connect() as c:
    idx = {r[0] for r in c.execute(text(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='watch_history'"))}
check("init_db creates ix_wh_title_lc", "ix_wh_title_lc" in idx)

from src.services.tonight import candidates  # noqa: E402

statements = []
from sqlalchemy import event  # noqa: E402

@event.listens_for(conn.engine, "before_cursor_execute")
def _grab(c, cur, stmt, *a):
    statements.append(stmt)

titles = [c["title"] for c in candidates(uid, now=now)]
check("a film played since it was stored is dropped", "Seen Film" not in titles)
check("an unplayed film stays", "Fresh Film" in titles)

q = next(s for s in statements if "lower(watch_history.title)" in s)
raw = conn.engine.raw_connection()
plan = " ".join(r[3] for r in raw.cursor().execute("EXPLAIN QUERY PLAN " + q, (uid, "x", 1, 0, 0)[:q.count("?")]))
raw.close()
check("the played lookup uses ix_wh_title_lc, not the user_id index",
      "ix_wh_title_lc" in plan and "ix_watch_history_user_id" not in plan)
check.done()
