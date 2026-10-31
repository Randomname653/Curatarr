"""The user's own clock, DST included.

Local time came from this machine's CURRENT offset applied to every row:
plays from the other half of the year were an hour off across a DST change,
a container on UTC put everyone's evening in UTC, and a member in another
zone was wrong all year. The browser now reports its IANA zone, and each
row is converted with the offset in force at its own time.

    python tests/test_user_time.py
"""
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import src.services.user_time as ut  # noqa: E402
import src.services.wrapup_facts as wf  # noqa: E402
from src.database.models import Base, User, WatchHistoryEntry  # noqa: E402

PASS = FAIL = 0
ROOT = Path(__file__).resolve().parents[1]


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


BERLIN, NY = ZoneInfo("Europe/Berlin"), ZoneInfo("America/New_York")

# ── zones and conversion ─────────────────────────────────────────────────────
check("a known IANA zone is accepted", ut.valid_zone("Europe/Berlin") == "Europe/Berlin")
check("a typo, a path or nothing is refused",
      ut.valid_zone("Europe/Berlinn") is None and ut.valid_zone("../etc/passwd") is None
      and ut.valid_zone("") is None and ut.valid_zone(None) is None)
check("winter: 22:30 UTC is 23:30 in Berlin",
      ut.to_local(datetime(2026, 1, 15, 22, 30), BERLIN) == datetime(2026, 1, 15, 23, 30))
check("summer: 22:30 UTC is 00:30 the next day — not 23:30 with today's offset",
      ut.to_local(datetime(2026, 7, 15, 22, 30), BERLIN) == datetime(2026, 7, 16, 0, 30))
ch = ut.offset_changes(BERLIN, datetime(2026, 1, 1), datetime(2027, 1, 1))
check("Berlin's 2026 offset changes, to the minute",
      ch == [(datetime(2026, 1, 1), timedelta(hours=1)),
             (datetime(2026, 3, 29, 1, 0), timedelta(hours=2)),
             (datetime(2026, 10, 25, 1, 0), timedelta(hours=1))])
check("a zone without DST has no changes",
      len(ut.offset_changes(ZoneInfo("Asia/Tokyo"), datetime(2026, 1, 1), datetime(2027, 1, 1))) == 1)

# ── the same conversion inside a query ──────────────────────────────────────
from _checks import memory_engine  # noqa: E402
engine = memory_engine()
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, autoflush=False)


@contextmanager
def fake_session():
    db = Session()
    try:
        yield db
        db.commit()
    finally:
        db.close()


moments = [datetime(2026, 1, 15, 22, 30), datetime(2026, 3, 29, 0, 59), datetime(2026, 3, 29, 1, 0),
           datetime(2026, 7, 15, 22, 30), datetime(2026, 10, 25, 0, 59), datetime(2026, 10, 25, 1, 0),
           datetime(2026, 12, 31, 23, 30)]
with fake_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True, timezone="America/New_York"))
    db.add(User(id=2, plex_user_id="2", plex_username="member", timezone=None))
    for i, at in enumerate(moments):
        db.add(WatchHistoryEntry(user_id=2, plex_user_id="2", plex_item_id=f"m{i}", title=f"M{i}",
                                 media_type="movie", viewed_at=at, duration_ms=60_000, completed=True))
with fake_session() as db:
    W = WatchHistoryEntry
    expr = ut.local_sql(W.viewed_at, BERLIN, datetime(2026, 1, 1), datetime(2027, 1, 1))
    got = [datetime.fromisoformat(v) for (v,) in db.query(expr).filter(W.user_id == 2).order_by(W.viewed_at)]
check("in SQL every row gets the offset of its own time, both change moments included",
      got == [ut.to_local(m, BERLIN) for m in moments])

# ── whose zone ───────────────────────────────────────────────────────────────
ut.server_zone.cache_clear()
_real_server = ut.server_zone
ut.server_zone = lambda: BERLIN
with fake_session() as db:
    check("the stored zone is the user's clock", ut.user_zone(db, 1) == NY)
    check("without one, the machine's zone stands in", ut.user_zone(db, 2) == BERLIN)
check("an unknown stored name falls back to the machine's zone", ut.zone_of("Mars/Olympus") == BERLIN)
from src.services.viewing_sessions import _local  # noqa: E402
check("sittings' dayparts use the offset of the play's own date",
      _local(datetime(2026, 7, 15, 22, 30)) == datetime(2026, 7, 16, 0, 30)
      and _local(datetime(2026, 1, 15, 22, 30)) == datetime(2026, 1, 15, 23, 30))
ut.server_zone = _real_server

# ── the Wrap-Up runs on the user's clock ─────────────────────────────────────
wf.get_db_session = fake_session
wf._verified_lookup = lambda items: {}
with fake_session() as db:
    for d in range(6):          # winter evenings in New York: 03:30 UTC is 22:30 EST
        db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id=f"w{d}", title="Heat",
                                 media_type="movie", viewed_at=datetime(2026, 1, 10 + d, 3, 30),
                                 duration_ms=170 * 60_000, completed=True))
f = wf.build_wrapup_facts(1, "all", "year:2026", now=datetime(2026, 2, 1), is_admin=True)
check("the owner in New York peaks at 22:00, not at 03:00 UTC",
      f["tempo"]["peak_hour"] == 22 and f["tempo"]["dayparts"].get("night", 0) == 0)
check("...and the plays fall on their New York days",
      f["tempo"]["biggest_day"]["date"].startswith("2026-01-"))
check("facts built on the new clock are a new generation", wf.FACTS_VERSION >= 3)

# ── the browser tells it ─────────────────────────────────────────────────────
from src.routers.users import TimezoneUpdate, set_timezone  # noqa: E402

db = Session()
me = db.get(User, 2)
r = set_timezone(TimezoneUpdate(timezone="Europe/Berlin"), user=me, db=db)
try:
    set_timezone(TimezoneUpdate(timezone="Europe/Berlinn"), user=me, db=db)
    code = None
except HTTPException as e:
    code = e.status_code
db.close()
with fake_session() as db:
    stored = db.get(User, 2).timezone
check("the reported zone is stored, a typo refused with 400",
      r == {"timezone": "Europe/Berlin"} and stored == "Europe/Berlin" and code == 400)
auth_js = (ROOT / "frontend/js/auth.js").read_text(encoding="utf-8")
check("the app reports the browser's zone when it differs from the stored one",
      "Intl.DateTimeFormat().resolvedOptions().timeZone" in auth_js
      and "'/api/users/me/timezone'" in auth_js and "syncTimezone(u);" in auth_js)
auth_py = (ROOT / "src/routers/auth.py").read_text(encoding="utf-8")
check("the session status carries the stored zone", '"timezone": user.timezone' in auth_py)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
