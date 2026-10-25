"""Plays that are no viewings, and plays stored as unfinished (2026-10-25).

Seen in the Wrap-Up on the real server:
- "Mark as played" in Plex gives every item it touches one lastViewedAt
  second and writes no history event. A whole season "watched" in a second
  read as a viewing; Killing Bites, played in March and marked in October,
  became the period's title with 12 "replays". 268 of 1259 video rows on the
  real server were such marks (Kim Possible: 86 episodes in one second).
- Music plays imported from Plex's history carry no length and were stored
  unfinished, so every artist read 0 h.

    python tests/test_marked_plays.py
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import Base, User, WatchHistoryEntry as W  # noqa: E402
from src.services import plex_sync as ps  # noqa: E402
from src.services import wrapup_facts as wf  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── the sync recognises a mark ──────────────────────────────────────────────
items = [{"ratingKey": str(k), "lastViewedAt": 1761259785} for k in range(10, 22)]
items += [{"ratingKey": "30", "lastViewedAt": 1761259000}, {"ratingKey": "31", "lastViewedAt": 1761259001},
          {"ratingKey": "40", "lastViewedAt": 1761250000, "_in_progress": True},
          {"ratingKey": "41", "lastViewedAt": 1761250000}]
n = ps.mark_bulk_marked(items)
check("twelve episodes in one second are a mark; plays seconds apart, and two items, are not",
      n == 12 and all(i.get("_marked") for i in items[:12])
      and not any(i.get("_marked") for i in items[12:]))
src = (ROOT / "src/services/plex_sync.py").read_text(encoding="utf-8")
check("a mark of an item with a stored play writes nothing; a first mark is stored as 'marked'",
      'if entry.get("_marked") and near.get((user.id, rating_key)):' in src
      and 'source="marked" if entry.get("_marked") else None,' in src)
check("...is not news for the watched-a-recommendation follow-up",
      'if not entry.get("_backfill") and not entry.get("_marked"):' in src)
check("...and a history play never borrows the mark from the listing", "_marked" in ps._LISTING_ONLY_KEYS)

# ── the migration repairs what was stored ───────────────────────────────────
eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
conn_mod.engine = eng
Base.metadata.create_all(bind=eng)
with eng.begin() as c:
    c.exec_driver_sql("PRAGMA user_version = 1")
S = sessionmaker(bind=eng)
db = S()
db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True))
MARK = datetime(2026, 10, 23, 22, 49, 45)
for ep in range(1, 13):
    db.add(W(user_id=1, plex_user_id="1", plex_item_id=f"kb{ep}", title=f"Ep {ep}", media_type="anime",
             series_title="Killing Bites", episode=ep, viewed_at=datetime(2026, 3, 7, 21) + timedelta(minutes=24 * ep),
             duration_ms=1_449_000, completed=True))
    db.add(W(user_id=1, plex_user_id="1", plex_item_id=f"kb{ep}", title=f"Ep {ep}", media_type="anime",
             series_title="Killing Bites", episode=ep, viewed_at=MARK, duration_ms=1_449_000, completed=True))
for k, at in (("e1", datetime(2026, 10, 20, 20)), ("e2", datetime(2026, 10, 20, 20, 24))):
    db.add(W(user_id=1, plex_user_id="1", plex_item_id=k, title=k, media_type="anime", series_title="Elfen Lied",
             viewed_at=at, duration_ms=1_440_000, completed=True))
db.add(W(user_id=1, plex_user_id="900000001", plex_item_id="t1", title="Schattenfraß", media_type="music",
         series_title="ASP", viewed_at=datetime(2026, 10, 3, 16), completed=False))
db.add(W(user_id=1, plex_user_id="1", plex_item_id="t2", title="Skipped", media_type="music",
         series_title="ASP", viewed_at=datetime(2026, 10, 3, 17), completed=False, view_offset_ms=20_000,
         duration_ms=240_000))
db.commit()
db.close()

conn_mod._migrate_columns()
db = S()
marked = db.query(W).filter(W.source == "marked").all()
check("the twelve rows of one second are marked; the March plays and Elfen Lied are not",
      len(marked) == 12 and all(r.viewed_at == MARK for r in marked)
      and db.query(W).filter(W.series_title == "Elfen Lied", W.source.is_(None)).count() == 2)
check("a music play from history without a length is a play now; a real skip stays unfinished",
      db.query(W).filter_by(plex_item_id="t1").one().completed is True
      and db.query(W).filter_by(plex_item_id="t2").one().completed is False)
check("the migration runs once (schema generation 2)",
      db.execute(text("PRAGMA user_version")).scalar() == conn_mod.SCHEMA_VERSION >= 2)
db.close()

# ── the wrap-up counts viewings, not marks ──────────────────────────────────
db = S()
ms = wf.played_ms_sql(W)
from sqlalchemy import func  # noqa: E402

oct_hours = (db.query(func.coalesce(W.series_title, W.title), func.sum(ms))
             .filter(W.user_id == 1, W.viewed_at >= datetime(2026, 10, 1), wf.a_play(W))
             .group_by(func.coalesce(W.series_title, W.title)).all())
check("October without the mark: Elfen Lied, not Killing Bites; ASP's play has a length",
      {t for t, _ in oct_hours} == {"Elfen Lied", "ASP"}
      and all(h > 0 for t, h in oct_hours if t == "ASP"))
db.close()
check("facts built on the repaired history are a new generation", wf.FACTS_VERSION >= 7)
facts_src = (ROOT / "src/services/wrapup_facts.py").read_text(encoding="utf-8")
check("every in-period query of the facts leaves marks out", facts_src.count("a_play(W)") >= 6)
orphan = (ROOT / "src/services/orphan_repair.py").read_text(encoding="utf-8")
check("the orphan import stores a history play without a length as played",
      "completed = not duration_ms or (" in orphan)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
