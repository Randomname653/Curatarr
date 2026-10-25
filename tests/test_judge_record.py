"""The judge's record and the plays of removed media: kept, not deleted.

* Verdicts lived only in a 30-day cache, so by December the spring's were
  gone. Every verdict is now kept in ``judge_verdicts``, one row per change.
* Lifting a protection deleted its row, and re-judging overwrote it. The old
  state now lands in ``protection_history`` first.
* The orphan cleanup deleted the plays of media gone from Plex. It now marks
  them: hidden from every ORM read, kept for the readers that ask.

    python tests/test_judge_record.py
"""
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import aliased, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.routers.recommendations as recs  # noqa: E402
import src.services.judge_record as jr  # noqa: E402
import src.services.recommendations_engine as eng  # noqa: E402
from src.database import orphaned  # noqa: E402
from src.database.models import (Base, JudgeVerdict, ProtectedMedia,  # noqa: E402
                                 ProtectionHistory, User, WatchHistoryEntry as W)

ROOT = Path(__file__).resolve().parents[1]
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def fake_db_session():
    s = Session()
    try:
        yield s
        s.commit()
    finally:
        s.close()


jr.get_db_session = fake_db_session
eng.get_db_session = fake_db_session

with fake_db_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True))

# ── 1. verdicts ──────────────────────────────────────────────────────────────
print("\n-- a verdict is kept, one row per change --")
item = {"title": "Old Film", "tmdb_id": 42, "size_mb": 8000}
cut = {"verdict": "CUT", "protecting_pillar": "NONE", "pillar_0_ego": "Far from the taste.",
       "pillar_3_household": "Nobody else watched it."}
t0 = datetime(2026, 3, 1)
r1 = jr.record_verdict(1, item, "movie", cut, score=71.5, model="judge-m", verdict_key="k1", now=t0)
r2 = jr.record_verdict(1, item, "movie", cut, score=71.5, model="judge-m", verdict_key="k1",
                       now=t0 + timedelta(days=1))


def verdicts():
    with fake_db_session() as db:
        return db.query(JudgeVerdict).order_by(JudgeVerdict.id).all()


rows = verdicts()
check("first verdict is a new row, the same one again only counts", (r1, r2) == ("new", "same"))
check("...one row, two scans, last_at moved",
      len(rows) == 1 and rows[0].scans == 2 and rows[0].last_at == t0 + timedelta(days=1)
      and rows[0].first_at == t0)
check("...with the item key, score, size and the findings",
      rows[0].item_key == "42" and rows[0].score == 71.5 and rows[0].size_mb == 8000
      and json.loads(rows[0].pillars_json)["pillar_0_ego"] == "Far from the taste.")

floored = {**cut, "verdict": "STAGNANT", "_guard": "household_floor", "_guard_detail": "anna"}
r3 = jr.record_verdict(1, item, "movie", floored, model_verdict="CUT", verdict_key="k1",
                       now=t0 + timedelta(days=2))
rows = verdicts()
check("a guard's change is a new row, with what the model said",
      r3 == "new" and len(rows) == 2 and rows[1].verdict == "STAGNANT"
      and rows[1].model_verdict == "CUT" and rows[1].guard == "household_floor"
      and json.loads(rows[1].pillars_json)["guard_detail"] == "anna")
jr.record_verdict(1, item, "movie", floored, model_verdict="CUT", verdict_key="k2",
                  now=t0 + timedelta(days=3))
check("new inputs (another verdict key) are a new row", len(verdicts()) == 3)
check("a failed verdict is not kept",
      jr.record_verdict(1, item, "movie", {"verdict": "EVALUATE", "_error": "timeout"}) is None
      and len(verdicts()) == 3)
check("a title without TMDB id is keyed by its title",
      jr.record_verdict(1, {"title": "No Id"}, "show", cut) == "new"
      and verdicts()[-1].item_key == "No Id")

# ── 2. protections ───────────────────────────────────────────────────────────
print("\n-- a protection's old state is kept --")


def history():
    with fake_db_session() as db:
        return db.query(ProtectionHistory).order_by(ProtectionHistory.id).all()


keep = {"verdict": "KEEP_WITH_FLAG", "protecting_pillar": "CUSTODIAN",
        "pillar_2_custodian": "A documented classic of its kind."}
eng._persist_judge_protection(1, item, "movie", "KEEP_WITH_FLAG", keep)
eng._persist_judge_protection(1, item, "movie", "KEEP_WITH_FLAG", keep)
check("re-judging to the same verdict archives nothing", history() == [])
eng._persist_judge_protection(1, item, "movie", "HARD_KEEP", {**keep, "verdict": "HARD_KEEP"})
h = history()
check("a changed judge verdict archives the old one",
      len(h) == 1 and h[0].kind == "changed" and h[0].verdict == "KEEP_WITH_FLAG"
      and h[0].new_verdict == "HARD_KEEP" and h[0].by_user_id is None)

with fake_db_session() as db:
    pid = db.query(ProtectedMedia.id).scalar()
    db.query(ProtectedMedia).update({"verdict": "KEEP_WITH_FLAG"})
owner = type("U", (), {"id": 1})()
db = Session()
recs.downscale_done(pid, user=owner, db=db)
db.close()
h = history()
check("marking a downscale done archives the KEEP_WITH_FLAG it was",
      h[-1].kind == "downscaled" and h[-1].verdict == "KEEP_WITH_FLAG"
      and h[-1].new_verdict == "HARD_KEEP" and h[-1].by_user_id == 1)
db = Session()
res = recs.delete_judge_protection(pid, user=owner, db=db)
db.close()
h = history()
with fake_db_session() as s:
    live = s.query(ProtectedMedia).count()
check("lifting deletes the live row and archives what it was",
      res["status"] == "deleted" and live == 0 and h[-1].kind == "lifted"
      and h[-1].by_user_id == 1 and h[-1].identifier == "42" and h[-1].source == "judge"
      and h[-1].granted_at is not None and h[-1].protection_id == pid
      and h[-1].verdict == "HARD_KEEP")

# ── 3. the judge loop keeps every verdict ────────────────────────────────────
print("\n-- the judge loop keeps every verdict --")
src_eng = (ROOT / "src/services/recommendations_engine.py").read_text()
loop = src_eng[src_eng.index("_model_v = v"):src_eng.index('if v in ("CUT", "STAGNANT"):')]
check("after the household floor, before the verdict is acted on",
      "HOUSEHOLD FLOOR" in loop and "judge_record.keep_verdict(" in loop
      and "model_verdict=_model_v" in loop and "verdict_key=_vkey" in loop)

# ── 4. plays of removed media ────────────────────────────────────────────────
print("\n-- the plays of removed media are hidden, not deleted --")
now = datetime(2026, 10, 1)
with fake_db_session() as s:
    for i, gone in enumerate((None, now, None)):
        s.add(W(user_id=1, plex_user_id="1", plex_item_id=str(100 + i), title=f"T{i}",
                media_type="movie", viewed_at=now, orphaned_at=gone))
s = Session()
check("hidden from entity, column, count and select reads",
      s.query(W).count() == 2 and len(s.query(W.title).all()) == 2
      and s.query(func.count(W.id)).scalar() == 2 and len(s.execute(select(W.title)).all()) == 2
      and len(s.query(aliased(W).title).all()) == 2)
check("...and from a lookup by id", s.get(W, 2) is None)
check("one statement can ask for them",
      s.query(func.count(W.id)).execution_options(include_orphaned=True).scalar() == 3)
s.close()
s = orphaned.include(Session())
check("a session that asks reads them all", s.query(W).count() == 3)
s.close()
s = Session()
check("bulk updates are not filtered", s.query(W).update({"title": "x"}) == 3)
s.rollback()
s.close()
for path, n in (("src/services/wrapup_facts.py", 3), ("src/services/taste_snapshots.py", 2),
                ("src/routers/wrapup.py", 1), ("src/services/plex_sync.py", 1)):
    check(f"{path} reads the hidden plays ({n})",
          (ROOT / path).read_text().count("orphaned.include(db)") == n)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
