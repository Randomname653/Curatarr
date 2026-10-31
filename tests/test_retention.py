"""Retention, the settle-only escape and the lyrics vacuum (2026-10 audit).

1. retention_pass removes retired rows past their horizon, never a row a
   discussion thread points at. The thread match is SQL string
   concatenation (||): with SQLite's arithmetic +, 'deletion_proposal:'
   + '42' is 42, nothing matches, and discussed rows would be deleted.
2. A settle-only task (the weekly VACUUM) ran only in the first tick after a
   start — never again on a box that is not restarted. Past twice its
   cadence it runs on any tick.
3. The lyrics / Plex-music index is vacuumed with the other SQLite files.

    python tests/test_retention.py
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

import _harness  # noqa: E402,F401  (first: temp stores and .env, never the live data/)
import src.database.connection as conn_mod
import src.services.retention as ret
from src.database.models import (AppState, Base, ChatStarter, ConversationMessage,
                                 DeletionProposal, ProactiveMessage)

from _checks import check  # noqa: E402


from _checks import memory_engine, with_users  # noqa: E402
engine = memory_engine()
Base.metadata.create_all(engine)
with_users(engine, 1)
Session = sessionmaker(bind=engine)


@contextmanager
def fake_db_session():
    s = Session()
    try:
        yield s
        s.commit()
    finally:
        s.close()


conn_mod.get_db_session = fake_db_session

# ── 1. the trap itself, and that the helper does not fall into it ───────────
raw = sqlite3.connect(":memory:")
check("SQLite's + is arithmetic: 'deletion_proposal:' + '42' = 42",
      raw.execute("SELECT 'deletion_proposal:' + '42'").fetchone()[0] == 42)
expr = ret._thread_of("deletion_proposal:", DeletionProposal.id)
sql = str(select(expr).compile(engine))
check("the thread match compiles to ||, not +", "||" in sql and " + " not in sql)
with engine.connect() as c:
    check("... and evaluates to the thread id",
          c.execute(select(ret._thread_of("deletion_proposal:", 42))).scalar()
          == "deletion_proposal:42")

# ── 2. retention_pass ────────────────────────────────────────────────────────
NOW = datetime(2026, 10, 20, 12, 0)
old, recent = NOW - timedelta(days=120), NOW - timedelta(days=10)
with fake_db_session() as db:
    def prop(pid, status, resolved):
        db.add(DeletionProposal(id=pid, user_id=1, title=f"P{pid}", media_id=str(pid),
                                service="radarr", status=status, resolved_at=resolved))
    prop(1, "superseded", old)           # goes
    prop(2, "superseded", old)           # discussed: stays
    prop(3, "superseded", recent)        # too young: stays
    prop(4, "deleted", old)              # tombstone: stays
    prop(5, "rejected", old)             # the owner's keep: stays
    prop(6, "superseded", None)          # legacy row with no date: stays
    prop(42, "superseded", old)          # discussed, two-digit id: stays
    db.add(ConversationMessage(user_id=1, role="user", content="why?",
                               thread_id="deletion_proposal:2"))
    db.add(ConversationMessage(user_id=1, role="user", content="and this?",
                               thread_id="deletion_proposal:42"))

    def bubble(mid, read, created):
        db.add(ProactiveMessage(id=mid, user_id=1, trigger_type="t", message="m",
                                read=read, created_at=created))
    bubble(1, True, old)                 # goes
    bubble(2, True, old)                 # discussed: stays
    bubble(3, False, old)                # unread: stays
    bubble(4, True, recent)              # too young: stays
    db.add(ConversationMessage(user_id=1, role="user", content="tell me more",
                               thread_id="proactive_message:2"))

    db.add(ChatStarter(id=1, user_id=1, text="a", used_at=NOW - timedelta(days=40)))
    db.add(ChatStarter(id=2, user_id=1, text="b", expires_at=NOW - timedelta(days=40)))
    db.add(ChatStarter(id=3, user_id=1, text="c", expires_at=NOW + timedelta(days=2)))
    db.add(ChatStarter(id=4, user_id=1, text="d", used_at=NOW - timedelta(days=3)))

    for day in ("2026-09-01", "2026-10-12", "2026-10-13", "2026-10-20"):
        db.add(AppState(key=f"omdb_calls:{day}", value="5"))
    db.add(AppState(key="job_fail:plex_sync", value="{}"))

removed = ret.retention_pass(now=NOW)
with fake_db_session() as db:
    props = {p.id for p in db.query(DeletionProposal)}
    bubbles = {m.id for m in db.query(ProactiveMessage)}
    starters = {s.id for s in db.query(ChatStarter)}
    keys = {a.key for a in db.query(AppState)}
check("an old undiscussed superseded proposal is removed", 1 not in props)
check("a superseded proposal with a discussion thread is kept", {2, 42} <= props)
check("recent, deleted, rejected and undated rows are kept", {3, 4, 5, 6} <= props)
check("an old read bubble is removed; a discussed, unread or recent one kept",
      bubbles == {2, 3, 4})
check("starters used or expired over 30 days ago are removed", starters == {3, 4})
check("OMDb day counters older than a week go; the rest of app_state stays",
      keys == {"omdb_calls:2026-10-13", "omdb_calls:2026-10-20", "job_fail:plex_sync"})
check("the pass reports what it removed",
      removed == {"proposals": 1, "proactive": 1, "starters": 2, "app_state": 2})
check("a second pass removes nothing", sum(ret.retention_pass(now=NOW).values()) == 0)
check("deletion_proposals is AUTOINCREMENT, so a freed id is never reused",
      DeletionProposal.__table__.dialect_options["sqlite"]["autoincrement"])

# ── 3. settle-only escape ────────────────────────────────────────────────────
import src.services.data_custodian as dc  # noqa: E402
import src.services.scheduler as sched  # noqa: E402

vac = next(t for t in dc._registry() if t.job_id == "db_vacuum")
check("the vacuum is still settle-only", vac.settle_only)
asked = []
_real_overdue = sched._job_overdue


def overdue_after(hours):
    def fake(job_id, interval_hours, grace_factor=1.1):
        asked.append(interval_hours)
        return interval_hours < hours
    return fake


dc._first_run_done[dc.queue_of(vac)] = False
check("first tick after a start: no wait", not dc._settle_waits(vac, dc.queue_of(vac)))
dc._first_run_done[dc.queue_of(vac)] = True
sched._job_overdue = overdue_after(1.5 * vac.cadence_h)     # 1.5 weeks since the last run
check("later ticks wait while the vacuum is under twice its cadence overdue",
      dc._settle_waits(vac, dc.queue_of(vac)))
sched._job_overdue = overdue_after(10 * vac.cadence_h)      # months without a restart
check("... and run it once it is past twice its cadence",
      not dc._settle_waits(vac, dc.queue_of(vac))
      and asked[-1] == vac.cadence_h * dc._SETTLE_ESCAPE)
plain = next(t for t in dc._registry() if not t.settle_only)
check("ordinary tasks never wait", not dc._settle_waits(plain, dc.queue_of(plain)))
sched._job_overdue = _real_overdue
src_dc = (Path(__file__).resolve().parents[1] / "src/services/data_custodian.py").read_text(encoding="utf-8")
check("every settle gate goes through _settle_waits",
      "t.settle_only and _first_run_done" not in src_dc.replace(
          "if not (t.settle_only and _first_run_done[queue]):", ""))

# ── 4. the lyrics index is vacuumed too ─────────────────────────────────────
from src.config import settings  # noqa: E402
from src.services.lyrics import LYRICS_DB_PATH  # noqa: E402
import src.services.app_state as app_state  # noqa: E402

tmp = tempfile.mkdtemp(prefix="curatarr_ret_")
cache_file = os.path.join(tmp, "enrichment.db")
lyrics_file = os.path.join(tmp, LYRICS_DB_PATH.name)
for path in (cache_file, lyrics_file):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE junk (b BLOB)")
    c.executemany("INSERT INTO junk VALUES (?)", [(b"x" * 4000,) for _ in range(300)])
    c.commit()
    c.execute("DELETE FROM junk")
    c.commit()
    c.close()
_orig = settings.DATABASE_URL, settings.ENRICHMENT_CACHE
settings.DATABASE_URL = "sqlite:///" + os.path.join(tmp, "absent.db")
settings.ENRICHMENT_CACHE = cache_file
from _checks import real_state  # noqa: E402
real_state().clear()            # the real app_state table, empty
vacuumed = []
_real_vac = sched._vacuum_sqlite_file
sched._vacuum_sqlite_file = lambda p: (vacuumed.append(p), _real_vac(p))
try:
    asyncio.run(sched.job_db_vacuum())
finally:
    sched._vacuum_sqlite_file = _real_vac
    settings.DATABASE_URL, settings.ENRICHMENT_CACHE = _orig
check("the lyrics index next to the cache file is vacuumed",
      lyrics_file in vacuumed and cache_file in vacuumed)
check("... the one next to the CURRENT cache path, not the import-time one",
      str(LYRICS_DB_PATH) not in vacuumed or str(LYRICS_DB_PATH) == lyrics_file)

t = next((t for t in dc._registry() if t.job_id == "retention"), None)
check("retention is a daily background wrapper task",
      t is not None and t.cadence_h == 24.0 and t.takes_task and not t.needs_llm)

check.done()
