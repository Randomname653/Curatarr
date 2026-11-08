"""The Wrap-Up's moments on the chat landing (stages 3.3 / 3.4 of
docs/UX_RETENTION_DESIGN.md): "Your week" from Sunday 17:00 to the end of
Monday on the member's clock, deterministic, in their language, only for a
week that reached the Wrap-Up's threshold. Fixed zone and moments; every
title invented.

    python tests/test_wrapup_moments.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

conn = app_db()

from src.database.models import User, WatchHistoryEntry  # noqa: E402
from src.services import wrapup_cards as wc  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")
H = 3_600_000

check("the week's moment: Sunday from 17:00 (this week), all Monday (the week before), never else",
      wc.week_due(datetime(2026, 11, 8, 17, 0)) == datetime(2026, 11, 8, 17, 0)
      and wc.week_due(datetime(2026, 11, 9, 23, 0)) == datetime(2026, 11, 8, 23, 0)
      and wc.week_due(datetime(2026, 11, 8, 16, 59)) is None
      and wc.week_due(datetime(2026, 11, 10, 9, 0)) is None)

with conn.get_db_session() as db:
    u = User(plex_user_id="mo-1", plex_username="Member", timezone="Europe/Berlin")
    quiet = User(plex_user_id="mo-2", plex_username="Quiet", timezone="Europe/Berlin")
    db.add_all([u, quiet])
    db.commit()
    UID, QUIET = u.id, quiet.id
    for i in range(6):                      # week 45: Monday 2 Nov .. Saturday 7 Nov, 20:00 UTC
        db.add(WatchHistoryEntry(user_id=UID, plex_user_id="mo-1", plex_item_id=f"k{i}", title=f"Episode {i + 1}",
                                 series_title="Starfall Archive", media_type="anime", season=1, episode=i + 1,
                                 duration_ms=H, view_offset_ms=H, completed=True, genres="Fantasy",
                                 viewed_at=datetime(2026, 11, 2, 20, 0) + timedelta(days=i)))
    # what the member watched before: no fantasy yet
    db.add(WatchHistoryEntry(user_id=UID, plex_user_id="mo-1", plex_item_id="old", title="The Lantern Room",
                             media_type="movie", duration_ms=H, view_offset_ms=H, completed=True,
                             genres="Mystery", viewed_at=datetime(2026, 9, 1, 20, 0)))
    db.add(WatchHistoryEntry(user_id=QUIET, plex_user_id="mo-2", plex_item_id="q1", title="Gull Point",
                             media_type="movie", duration_ms=H, view_offset_ms=H, completed=True,
                             viewed_at=datetime(2026, 11, 3, 20, 0)))
    db.commit()

monday = datetime(2026, 11, 9, 8, 0)                     # Monday 09:00 in Berlin
card = wc.week_card(UID, now=monday, zone=BERLIN, lang="en")
check("on Monday: the week just ended, its hours, days and what most of it was",
      card == {"kind": "week", "key": "week:2026-W45", "label": "Your week",
               "lines": ["6 h · 6 days with something on", "Most of it: **Starfall Archive** (6 h)",
                         "New for you: Fantasy"]}, card)
de = wc.week_card(UID, now=monday, zone=BERLIN, lang="de")
check("in German for a German writer",
      de["label"] == "Deine Woche" and de["lines"][0] == "6 Std · an 6 Tagen lief etwas"
      and de["lines"][1] == "Am meisten: **Starfall Archive** (6 Std)", de)
check("on a Tuesday there is no week card",
      wc.week_card(UID, now=datetime(2026, 11, 10, 18, 0), zone=BERLIN, lang="en") is None)
check("a week under the Wrap-Up's threshold gets no card - no small number, no 'only'",
      wc.week_card(QUIET, now=monday, zone=BERLIN, lang="en") is None)

from src.routers import home  # noqa: E402

_real = wc.week_card
wc.week_card = lambda uid: _real(uid, now=monday, zone=BERLIN, lang="en")
try:
    out = asyncio.run(home.moments(user=SimpleNamespace(id=UID)))
finally:
    wc.week_card = _real
check("GET /api/home/moments carries the week card", out == {"cards": [card]}, out)

check.done()
