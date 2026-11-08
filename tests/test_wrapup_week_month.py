"""Wrap-Up periods week: and month: on the member's own clock (stage 3.1 of
docs/UX_RETENTION_DESIGN.md): an ISO week and a calendar month run midnight
to midnight in the member's zone, daylight saving included; the window
before a month is the calendar month before it. The tests set the zone and
the moment, never the machine's clock.

    python tests/test_wrapup_week_month.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

conn = app_db()

from src.database.models import User, WatchHistoryEntry  # noqa: E402
from src.services import wrapup_facts as wf  # noqa: E402

BERLIN, UTC = ZoneInfo("Europe/Berlin"), ZoneInfo("UTC")
NOW = datetime(2026, 11, 20, 12, 0)

w = wf.resolve_period("week:2026-W45", NOW, BERLIN)
check("an ISO week starts Monday 00:00 on the member's clock (Berlin, winter: 23:00 UTC the "
      "Sunday before) and is a closed period once over",
      w.start == datetime(2026, 11, 1, 23, 0) and w.end == datetime(2026, 11, 8, 23, 0)
      and w.closed and w.label == "Week 45, 2026", w)
check("...and the week before it is the seven days before",
      w.previous == (datetime(2026, 10, 25, 23, 0), datetime(2026, 11, 1, 23, 0)), w.previous)
dst = wf.resolve_period("week:2026-W13", NOW, BERLIN)
check("a week across the change to summer time is 167 hours long",
      dst.end - dst.start == timedelta(hours=167), dst)
check("the same week on a UTC clock starts at UTC midnight",
      wf.resolve_period("week:2026-W45", NOW, UTC).start == datetime(2026, 11, 2, 0, 0))

m = wf.resolve_period("month:2026-10", NOW, BERLIN)
check("a month runs from the 1st to the 1st on the member's clock, summer to winter time",
      m.start == datetime(2026, 9, 30, 22, 0) and m.end == datetime(2026, 10, 31, 23, 0)
      and m.label == "October 2026" and m.closed, m)
check("the window before a month is the calendar month before (30 days of September), "
      "not the same length earlier",
      m.previous == (datetime(2026, 8, 31, 22, 0), datetime(2026, 9, 30, 22, 0)), m.previous)
jan = wf.resolve_period("month:2026-01", NOW, BERLIN)
check("January's month before is December of the year before",
      jan.previous[0] == datetime(2025, 11, 30, 23, 0))
cur = wf.resolve_period("month:2026-11", NOW, BERLIN)
check("the current month is open and ends now", not cur.closed and cur.end == NOW)
for bad in ("week:2026-W54", "month:2026-13", "week:2027-W01", "month:2027-01", "week:26-W1"):
    try:
        wf.resolve_period(bad, NOW, BERLIN)
        check(f"{bad} is refused", False)
    except ValueError:
        check(f"{bad} is refused", True)
check("the keys of a wall-clock date",
      wf.week_key(datetime(2026, 11, 8, 23, 30)) == "week:2026-W45"
      and wf.week_key(datetime(2026, 11, 9, 0, 30)) == "week:2026-W46"
      and wf.month_key(datetime(2026, 11, 1)) == "month:2026-11")

# ── the facts of a week count the member's week, not UTC's ──────────────────

with conn.get_db_session() as db:
    u = User(plex_user_id="wm-1", plex_username="Member", timezone="Europe/Berlin")
    db.add(u)
    db.commit()
    UID = u.id
    # Monday 00:30 in Berlin is Sunday 23:30 UTC: it belongs to week 46, not 45
    for i, at in enumerate([datetime(2026, 11, 8, 23, 30)] + [datetime(2026, 11, 10, 20, 0) + timedelta(days=d)
                                                             for d in range(5)]):
        db.add(WatchHistoryEntry(user_id=UID, plex_user_id="wm-1", plex_item_id=f"k{i}",
                                 title=f"Episode {i + 1}", series_title="Starfall Archive",
                                 media_type="anime", season=1, episode=i + 1, duration_ms=3_600_000,
                                 view_offset_ms=3_600_000, completed=True, viewed_at=at))
    db.commit()

f46 = wf.build_wrapup_facts(UID, "all", "week:2026-W46", now=NOW)
f45 = wf.build_wrapup_facts(UID, "all", "week:2026-W45", now=NOW)
check("a play at 00:30 Monday on the member's clock counts for that week",
      f46["tempo"]["plays"] == 6 and f45.get("empty") is True, (f46.get("tempo", {}).get("plays"), f45))
check("the week's facts carry their key and label", f46["period"]["key"] == "week:2026-W46"
      and f46["period"]["label"] == "Week 46, 2026" and f46["facts_version"] == wf.FACTS_VERSION)

# ── a week opened on Wednesday is not frozen at Wednesday ───────────────────

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from src.database.models import Wrapup  # noqa: E402
from src.routers import wrapup as wr  # noqa: E402
from src.services import wrapup_store  # noqa: E402

with conn.get_db_session() as db:
    mid = User(plex_user_id="wm-2", plex_username="Midweek", timezone="Europe/Berlin")
    db.add(mid)
    db.commit()
    MID = mid.id
    for d in range(7):                       # week 10 of 2026: Monday 2 March .. Sunday 8 March
        db.add(WatchHistoryEntry(user_id=MID, plex_user_id="wm-2", plex_item_id=f"m{d}", title=f"Episode {d + 1}",
                                 series_title="Starfall Archive", media_type="anime", season=1, episode=d + 1,
                                 duration_ms=3_600_000, view_offset_ms=3_600_000, completed=True,
                                 viewed_at=datetime(2026, 3, 2, 19, 0) + timedelta(days=d)))
    db.commit()
WEDNESDAY = datetime(2026, 3, 4, 20, 0)
wrapup_store.save_facts(MID, "all", "week:2026-W10",
                        wf.build_wrapup_facts(MID, "all", "week:2026-W10", now=WEDNESDAY))
with conn.get_db_session() as db:
    db.query(Wrapup).filter(Wrapup.user_id == MID).update({"facts_at": WEDNESDAY})
    db.commit()

built = []
_build, _posters = wf.build_wrapup_facts, wr.attach_posters


def _counting(*a, **k):
    built.append(a[2])
    return _build(*a, **k)


async def _no_posters(facts):
    facts.setdefault("posters", {})

wf.build_wrapup_facts, wr.attach_posters = _counting, _no_posters
try:
    member = SimpleNamespace(id=MID, is_admin=False, plex_username="Midweek")
    first, _ = asyncio.run(wr.current_facts(member, "all", "week:2026-W10"))
    again, _ = asyncio.run(wr.current_facts(member, "all", "week:2026-W10"))
finally:
    wf.build_wrapup_facts, wr.attach_posters = _build, _posters
check("a week whose facts were stored on its Wednesday is counted again once it is over: all seven days",
      first["tempo"]["plays"] == 7 and built[:1] == ["week:2026-W10"], (first.get("tempo"), built))
check("...and those facts, computed after the end, are the week's for good", again["tempo"]["plays"] == 7
      and len(built) == 1, built)

check.done()
