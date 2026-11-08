"""User-voiced chips on the chat landing (UX audit 2026-11-08, H1 / C3).

Beside the curator's openers the landing offers chips in the MEMBER's own
voice: no model, filled from the opener facts (chat_starters.collect_facts),
in the language they write to the curator in (services/user_language). The
owner's clean-up chip is the owner's only - a member cannot open Deletions.

Every title here is invented; the clock is a fixed zone and moment.

    python tests/test_user_chips.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

conn = app_db()

from src.database.models import ConversationMessage, User, WatchHistoryEntry  # noqa: E402
from src.services import chat_starters as cs  # noqa: E402
from src.services import user_language as ul  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BERLIN = ZoneInfo("Europe/Berlin")


def clock(weekday: str, daypart: str) -> dict:
    return {"kind": "now", "weekday": weekday, "daypart": daypart}


def texts(chips):
    return [c["text"] for c in chips]


# ── the pure builder ────────────────────────────────────────────────────────

c = cs.user_chips([clock("Tuesday", "evening")], "en", is_admin=False)
check("a brand-new member (only the clock) still gets chips: tonight, the mood, the taste",
      texts(c) == ["Something under 30 minutes for tonight", "I'm in the mood for ",
                   "What does my watch history say about my taste?"], texts(c))
check("the mood chip is a sentence to finish: the input gets a trailing space, the chip an ellipsis",
      c[1]["label"] == "I'm in the mood for …" and c[0]["label"] == c[0]["text"])
check("Friday and Saturday evenings offer a film instead",
      texts(cs.user_chips([clock("Friday", "evening")], "en", False))[0] == "A film for tonight - surprise me")
check("night and day have their own clock chip",
      texts(cs.user_chips([clock("Monday", "night")], "en", False))[0] == "Something short before bed"
      and texts(cs.user_chips([clock("Monday", "morning")], "en", False))[0] == "Something to put on later today")

facts = [{"kind": "last_watched", "title": "The Lantern Room", "media_type": "movie"},
         {"kind": "active_series", "title": "Starfall Archive", "media_type": "anime"},
         {"kind": "music_rotation", "artist": "The Hollow Pines", "plays_14d": 30},
         clock("Wednesday", "evening")]
c = texts(cs.user_chips(facts, "en", False))
check("an active series names the series; a music rotation fills the third chip",
      c == ["Something under 30 minutes for tonight", "Something in the spirit of Starfall Archive",
            "More music like The Hollow Pines"], c)
c = texts(cs.user_chips(facts[:1] + [clock("Wednesday", "evening")], "en", False))
check("without a series, the last film watched", "More like The Lantern Room" in c, c)

de = texts(cs.user_chips(facts, "de", False))
check("in the member's language: German chips for a German writer",
      de == ["Etwas unter 30 Minuten für heute Abend", "Etwas in der Art von Starfall Archive",
             "Mehr Musik wie The Hollow Pines"], de)
check("a language without chips reads the English ones",
      texts(cs.user_chips([clock("Tuesday", "evening")], "fr", False))
      == texts(cs.user_chips([clock("Tuesday", "evening")], "en", False)))

member = texts(cs.user_chips([clock("Tuesday", "evening")], "en", is_admin=False))
owner = texts(cs.user_chips([clock("Tuesday", "evening")], "en", is_admin=True))
check("the clean-up chip is the owner's only",
      "What can I clean up in my library?" in owner
      and not any("clean up" in t for t in member), (owner, member))
check("at most three chips", all(len(cs.user_chips(facts, lang, True)) == 3 for lang in ("en", "de")))
check("the German chips cover every English one", set(cs._CHIPS["de"]) == set(cs._CHIPS["en"]))

# ── from a real member: their facts, their zone, their language ────────────

with conn.get_db_session() as db:
    u = User(plex_user_id="chips-1", plex_username="Member", is_admin=False, timezone="Europe/Berlin")
    db.add(u)
    db.commit()
    uid = u.id
    now = datetime(2026, 11, 10, 18, 0)               # Tuesday 19:00 in Berlin
    for i in range(3):
        db.add(WatchHistoryEntry(user_id=uid, plex_user_id="chips-1", plex_item_id=f"k{i}",
                                 title=f"Episode {i + 1}", series_title="Starfall Archive",
                                 media_type="anime", season=1, episode=i + 1,
                                 viewed_at=now - timedelta(days=2, hours=i), completed=True))
    for text in ("Was soll ich heute Abend schauen? Ich habe Lust auf etwas Ruhiges.",
                 "Und was hältst du von der Serie, die ich gerade schaue?",
                 "Ich glaube, das ist nicht so mein Ding, aber ich gebe ihm eine Chance."):
        db.add(ConversationMessage(user_id=uid, role="user", content=text))
    db.commit()

ul._cache.clear()
got = texts(cs.chips_for(uid, is_admin=False, now=now))
check("chips_for: the member's language, their Berlin evening, their active series",
      got[:2] == ["Etwas unter 30 Minuten für heute Abend", "Etwas in der Art von Starfall Archive"]
      and not any("aufräumen" in t for t in got), got)

# ── the endpoint: openers and chips side by side ───────────────────────────

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from src.routers import chat  # noqa: E402
import src.services.process_monitor as pmon  # noqa: E402


async def _no_refill(uid, n=5):
    return 0

_saved = (cs.generate_starters, pmon.is_game_running)
cs.generate_starters, pmon.is_game_running = _no_refill, (lambda: False)
try:
    as_member = asyncio.run(chat.get_starters(user=SimpleNamespace(id=uid, is_admin=False)))
    with conn.get_db_session() as db:
        db.query(User).filter(User.id == uid).update({"is_admin": True})
        db.commit()
    as_owner = asyncio.run(chat.get_starters(user=SimpleNamespace(id=uid, is_admin=True)))
finally:
    cs.generate_starters, pmon.is_game_running = _saved
check("GET /starters returns the user-voiced chips beside the curator's openers",
      as_member["starters"] == [] and len(as_member["chips"]) == 3
      and all(set(c) == {"text", "label"} for c in as_member["chips"]), as_member)
check("the owner flag comes from the signed-in user: the clean-up chip for the owner only",
      not any("aufräumen" in c["text"] for c in as_member["chips"])
      and any("aufräumen" in c["text"] for c in as_owner["chips"]), (as_member, as_owner))

check.done()
