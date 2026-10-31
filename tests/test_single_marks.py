"""Episodes marked as played one at a time are no viewings (2026-10-30).

Plex's "mark as played" writes no history event. The sync caught a mark only
when three or more items shared one second (test_marked_plays). Marking
episodes one after another lands them seconds apart: 53 plays in 20 runs on
the owner's server (Goblin Slayer E8, E5, E7, E1, E3, E4 within 30 s; High
School DxD E3-E8, 5 s apart, read as a six-episode binge). And an episode
started, then marked, moved to the moment of the mark and counted in full.

    python tests/test_single_marks.py
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, func, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import Base, User, WatchHistoryEntry as W  # noqa: E402
from src.services import plex_sync as ps  # noqa: E402
from src.services import wrapup_facts as wf  # noqa: E402

from _checks import check  # noqa: E402


# ── the sync: a finished item with no play event is a mark ─────────────────
T = 1761700000
items = [
    {"ratingKey": "a", "lastViewedAt": T, "_category": "anime"},            # event 40 s off: watched
    {"ratingKey": "b", "lastViewedAt": T + 5, "_category": "anime"},        # no event: marked
    {"ratingKey": "c", "lastViewedAt": T + 10, "_category": "movie"},       # no event: marked
    {"ratingKey": "d", "lastViewedAt": T, "_category": "music"},            # music: left alone
    {"ratingKey": "e", "lastViewedAt": T, "_category": "show", "_in_progress": True},
    {"ratingKey": "f", "lastViewedAt": T, "_category": "show", "_history_extra": True},
    {"ratingKey": "g", "lastViewedAt": T, "_category": "show", "_marked": True},
]
history = {"a": [(T - 40, 1)], "c": [(T - 3600, 1)]}
n = ps.mark_unwitnessed(items, history)
flag = {i["ratingKey"]: bool(i.get("_marked")) for i in items}
check("a finished video item without a play event near it is marked; one with an event is not",
      n == 2 and flag == {"a": False, "b": True, "c": True, "d": False, "e": False, "f": False, "g": True})

src = (ROOT / "src/services/plex_sync.py").read_text(encoding="utf-8")
check("it runs only on an incremental sync whose history walk covered the window",
      "single_marks = (mark_unwitnessed(all_entries, history_by_rating_key)\n"
      "                    if capture[\"complete\"] and last_sync_ts else 0)" in src.replace("\r\n", "\n"))
mark_branch = src[src.index('if entry.get("_marked") and near.get((user.id, rating_key)):'):]
mark_branch = mark_branch[:mark_branch.index("skipped += 1")]
check("a mark after a started viewing finishes that row and keeps its time and minutes",
      "_open.completed = True" in mark_branch and "viewed_at =" not in mark_branch.split("_open.completed")[1]
      and "view_offset_ms =" not in mark_branch.split("_open.completed")[1]
      and "RESUME_WINDOW_DAYS" in mark_branch)

# ── the migration repairs what was stored ───────────────────────────────────
eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
conn_mod.engine = eng
Base.metadata.create_all(bind=eng)
with eng.begin() as c:
    c.exec_driver_sql("PRAGMA user_version = 2")
S = sessionmaker(bind=eng)
db = S()
db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True))
RUN = datetime(2025, 10, 5, 22, 13, 47)


def row(key, series, at, **kw):
    db.add(W(user_id=1, plex_user_id="1", plex_item_id=key, title=key, media_type="anime",
             series_title=series, viewed_at=at, duration_ms=1_440_000, completed=True, **kw))


for i, ep in enumerate((8, 5, 7, 1, 3, 4)):                          # Goblin Slayer, marked in a run
    row(f"gs{ep}", "Goblin Slayer", RUN + timedelta(seconds=6 * i))
row("el1", "Elfen Lied", datetime(2026, 10, 6, 22, 17))               # two episodes, one evening
row("el2", "Elfen Lied", datetime(2026, 10, 6, 22, 39))
row("tt1", "Tanya", datetime(2026, 10, 9, 22, 31), player="Chromecast")   # watched, 30 s apart:
row("tt2", "Tanya", datetime(2026, 10, 9, 22, 31, 30), player="Chromecast")  # both have events
row("dup", "Psycho-Pass", datetime(2026, 6, 28, 19, 51))              # one item twice
row("dup", "Psycho-Pass", datetime(2026, 6, 28, 19, 51, 20))
db.commit()
db.close()

conn_mod._migrate_columns()
db = S()
marked = {r.plex_item_id for r in db.query(W).filter(W.source == "marked")}
check("the run of marks is marked; an evening's two episodes, plays with a player and one item "
      "played twice are not",
      marked == {f"gs{e}" for e in (8, 5, 7, 1, 3, 4)})
check("the migration is generation 3",
      db.execute(text("PRAGMA user_version")).scalar() == conn_mod.SCHEMA_VERSION == 3)
db.close()

# ── played time: a finished row keeps the minutes really watched ────────────
check("a row finished by a mark counts its own minutes; a finished viewing its length",
      wf.played_ms({"completed": True, "duration_ms": 1_440_000, "view_offset_ms": 600_000})[0] == 600_000
      and wf.played_ms({"completed": True, "duration_ms": 1_440_000, "view_offset_ms": 1_440_000})[0] == 1_440_000
      and wf.played_ms({"completed": True, "duration_ms": 1_440_000})[0] == 1_440_000)
db = S()
db.add(W(user_id=1, plex_user_id="1", plex_item_id="half", title="half", media_type="anime",
         series_title="Half", viewed_at=datetime(2026, 10, 28), duration_ms=1_440_000,
         view_offset_ms=600_000, completed=True))
db.commit()
sql_ms = db.query(func.sum(wf.played_ms_sql(W))).filter(W.plex_item_id == "half").scalar()
el_ms = db.query(func.sum(wf.played_ms_sql(W))).filter(W.series_title == "Elfen Lied").scalar()
check("...in the SQL twin too", sql_ms == 600_000 and el_ms == 2 * 1_440_000)
db.close()
check("stored wrap-ups are rebuilt on the repaired history (a new facts generation)",
      wf.FACTS_VERSION >= 12)

check.done()
