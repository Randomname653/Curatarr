"""The Wrap-Up's device facts say the KIND of device - the TV, the
computer, the phone - never its own name (owner's read on real data: the
story said twice "you lived on the <machine name>"). A device's name is
given freely and often carries a person's, a household's or a machine's
name. One kind only is no device story. Every name here is invented.

    python tests/test_wrapup_devices.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

from datetime import datetime, timedelta

conn = app_db()

from src.database.models import User, WatchHistoryEntry  # noqa: E402
from src.services import wrapup_facts as wf  # noqa: E402
from src.services.wrapup_claims import claim  # noqa: E402
from src.services.wrapup_insights import mine  # noqa: E402

H = 3_600_000
check("a platform is a kind of device",
      [wf.device_kind(p) for p in ("tvOS", "Windows", "iOS 17.1 (iPhone14,2)", "Chrome", "Roku", "BeOS", None)]
      == ["TV", "computer", "phone", "browser", "TV", None, None])


def member(name, plays):
    with conn.get_db_session() as db:
        u = User(plex_user_id=name, plex_username=name, timezone="UTC")
        db.add(u)
        db.commit()
        for i, (player, platform, at) in enumerate(plays):
            db.add(WatchHistoryEntry(user_id=u.id, plex_user_id=name, plex_item_id=f"{name}{i}", title=f"Part {i}",
                                     series_title="Starfall Archive", media_type="anime", season=1, episode=i + 1,
                                     duration_ms=H, view_offset_ms=H, completed=True, player=player,
                                     platform=platform, viewed_at=at))
        db.commit()
        return u.id


day = datetime(2026, 10, 5)
two = member("dev-2", [("Fennick-PC", "Windows", day + timedelta(days=i, hours=23, minutes=30)) for i in range(6)]
             + [("Orla's Living Room", "tvOS", day + timedelta(days=i, hours=20)) for i in range(4)])
f = wf.build_wrapup_facts(two, "all", "month:2026-10", now=datetime(2026, 11, 2))
dv = f.get("devices") or {}
check("hours by kind of device, and the kind of the night - no device name",
      [x["player"] for x in dv.get("top", [])] == ["computer", "TV"] and dv.get("night", {}).get("player") == "computer",
      dv)
texts = " ".join(claim(c, lang) for c in mine(f, limit=40) for lang in ("en", "de"))
check("the story's device card says the kind, in both languages, never a name",
      "After 23:00 it was the computer" in texts and "Nach 23 Uhr lief es auf dem Computer" in texts
      and not any(n in texts for n in ("Fennick", "Orla", "Living Room")), texts)
one = member("dev-1", [("Fennick-PC", "Windows", day + timedelta(days=i, hours=23)) for i in range(5)]
             + [("Office", "Windows", day + timedelta(days=i, hours=20)) for i in range(5)])
check("two machines of one kind are one kind: no device story",
      not wf.build_wrapup_facts(one, "all", "month:2026-10", now=datetime(2026, 11, 2)).get("devices"))

check.done()
