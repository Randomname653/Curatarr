"""The member's landing is a living room, not the owner's machine room
(UX audit 2026-11-08, stage 1: H2, H4, L2, W4, 1.6).

* the landing's recommendations tile reads the cache and never generates;
* the bell names a message's trigger by its label, and no label grades the
  person (curator_voice.WORK) - the internal types stay;
* the follow-up card: the newest unread "how did it land?" message, with
  one-tap answers in the member's language;
* the Wrap-Up playlist says which server refused it, and a Jellyfin member
  without a linked account is told so instead of "not found in Plex".

Every title here is invented.

    python tests/test_member_living_room.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

conn = app_db()

from fastapi import HTTPException  # noqa: E402

from src.database.models import ConversationMessage, ProactiveMessage, User  # noqa: E402
from src.routers import messages as messages_router  # noqa: E402
from src.routers import recommendations as recs_router  # noqa: E402
from src.services import proactive_messages as pm  # noqa: E402
from src.services import user_language as ul  # noqa: E402

with conn.get_db_session() as db:
    member = User(plex_user_id="lr-1", plex_username="Member", is_admin=False)
    db.add(member)
    db.commit()
    UID = member.id

# ── the recommendations tile never generates ────────────────────────────────

import src.services.recommendations_engine as engine  # noqa: E402

_called = []


async def _no_generation(**k):
    _called.append(k)
    return []

_orig_gen = engine.generate_recommendations
engine.generate_recommendations = _no_generation
try:
    user = SimpleNamespace(id=UID, is_admin=False)
    out = asyncio.run(recs_router._get_recommendations_impl(None, 1, False, "cache", None, user,
                                                            cache_only=True))
    check("cache_only on an empty cache: an empty list, no generation",
          out["recommendations"] == [] and out["from_cache"] is True and _called == [], out)
    asyncio.run(recs_router._get_recommendations_impl("movie", 1, False, "cache", None, user))
    check("without cache_only the old path still generates on an empty cache (the Recs view)",
          len(_called) == 1)
finally:
    engine.generate_recommendations = _orig_gen

# ── labels, not internal names, and none grades the person ──────────────────

labels = {t["type"]: t["label"] for t in pm.TRIGGER_TYPES}
graded = {"Attention deficit", "Procrastinator", "Guilty pleasures", "Genre rut",
          "Low completion", "Night owl"}
check("no trigger label grades the person", not graded & set(labels.values()), labels)
check("the internal types are kept (stored messages, cooldowns and switches key on them)",
      {"attention_deficit", "procrastinator", "guilty_pleasure", "genre_rut",
       "low_completion", "night_owl"} <= pm.TRIGGER_TYPE_NAMES)
check("trigger_label: the Settings label, the keep outcome by name, an unknown type readable",
      pm.trigger_label("attention_deficit") == "Started, not finished"
      and pm.trigger_label("protection_intent") == "Keep request"
      and pm.trigger_label("history_deep_dive") == labels["history_deep_dive"]
      and pm.trigger_label("some_new_kind") == "Some new kind")

now = datetime.utcnow()
with conn.get_db_session() as db:
    db.add(ProactiveMessage(user_id=UID, trigger_type="guilty_pleasure", message="A line.",
                            read=False, created_at=now - timedelta(hours=3),
                            expires_at=now + timedelta(days=7)))
    db.commit()
unread = asyncio.run(pm.get_unread_messages(UID))
check("the bell's message carries its label", unread["message"]["label"] == "Comfort picks", unread)

# ── the follow-up card ──────────────────────────────────────────────────────

check("no follow-up message: no card", pm.followup_card(UID) is None)
with conn.get_db_session() as db:
    db.add_all([
        ProactiveMessage(user_id=UID, trigger_type="last_night", message="Old morning line.",
                         read=False, created_at=now - timedelta(days=1, hours=2),
                         expires_at=now - timedelta(hours=20)),                     # expired
        ProactiveMessage(user_id=UID, trigger_type="recommendation_followup",
                         message="You tried The Lantern Room. How did it land?",
                         read=False, created_at=now - timedelta(hours=2),
                         expires_at=now + timedelta(days=7)),
        ProactiveMessage(user_id=UID, trigger_type="recommendation_followup",
                         message="Already answered.", read=True,
                         created_at=now - timedelta(hours=1), expires_at=now + timedelta(days=7)),
    ])
    db.commit()
card = pm.followup_card(UID)
check("the card is the newest unread, unexpired follow-up - not the expired morning line, "
      "not the read one, not another trigger",
      card and card["message"] == "You tried The Lantern Room. How did it land?"
      and card["label"] == "Recommendation follow-up", card)
check("an English writer gets English answers", card["replies"] == list(pm.FOLLOWUP_REPLIES["en"]))

# A morning line gets the card only when it is about exactly one title:
# "Loved it" on two series and an hour of music answers nothing.
import json  # noqa: E402

with conn.get_db_session() as db:
    db.query(ProactiveMessage).filter(ProactiveMessage.user_id == UID,
                                      ProactiveMessage.trigger_type == "recommendation_followup"
                                      ).update({"read": True})
    db.add_all([
        ProactiveMessage(user_id=UID, trigger_type="last_night", message="Two series and music.",
                         trigger_data=json.dumps({"type": "last_night", "parts": 3, "titles": 2}),
                         read=False, created_at=now - timedelta(minutes=30),
                         expires_at=now + timedelta(hours=3)),
        ProactiveMessage(user_id=UID, trigger_type="last_night", message="Stored before the count.",
                         trigger_data=json.dumps({"type": "last_night", "summary": "x"}),
                         read=False, created_at=now - timedelta(minutes=20),
                         expires_at=now + timedelta(hours=3)),
    ])
    db.commit()
check("a morning line about several things, or stored before the count, gets no card",
      pm.followup_card(UID) is None)
with conn.get_db_session() as db:
    db.add(ProactiveMessage(user_id=UID, trigger_type="last_night",
                            message="Three episodes of Starfall Archive last night.",
                            trigger_data=json.dumps({"type": "last_night", "parts": 1, "titles": 1}),
                            read=False, created_at=now - timedelta(minutes=40),
                            expires_at=now + timedelta(hours=3)))
    db.commit()
one = pm.followup_card(UID)
check("a morning line about exactly one title gets the card, even under newer ones that do not",
      one and one["message"] == "Three episodes of Starfall Archive last night.", one)
with conn.get_db_session() as db:
    db.query(ProactiveMessage).filter(ProactiveMessage.user_id == UID,
                                      ProactiveMessage.message == "You tried The Lantern Room. How did it land?"
                                      ).update({"read": False})
    db.commit()

with conn.get_db_session() as db:
    for text in ("Was soll ich heute Abend schauen? Gern etwas Ruhiges.",
                 "Ich glaube, das ist nicht so mein Ding, aber ich schaue es mir an.",
                 "Und wie fandest du die Serie, die ich gerade schaue?"):
        db.add(ConversationMessage(user_id=UID, role="user", content=text))
    db.commit()
ul._cache.clear()
check("a German writer gets the answers in German (they are sent as the member's own words)",
      pm.followup_card(UID)["replies"] == list(pm.FOLLOWUP_REPLIES["de"]))
check("every language has four answers", all(len(r) == 4 for r in pm.FOLLOWUP_REPLIES.values()))
got = asyncio.run(messages_router.get_followup(user=SimpleNamespace(id=UID)))
check("GET /api/messages/followup answers the card", got["card"] is not None
      and got["card"]["trigger_type"] in pm.FOLLOWUP_TYPES, got)

# ── the Wrap-Up playlist names the server that answered ─────────────────────

import src.services.media_server as media_server  # noqa: E402
import src.services.plex_playlists as plex_playlists  # noqa: E402
import src.services.wrapup_store as wrapup_store  # noqa: E402
from src.routers import wrapup as wrapup_router  # noqa: E402

_saved = (wrapup_store.load, plex_playlists.push_wrapup_playlist, media_server.active_kind)
wrapup_store.load = lambda uid, scope, period: {"facts": {
    "top": [{"title": "The Lantern Room", "media_type": "movie"}]}}


def _push(answer):
    async def push(user, label, videos, tracks):
        return answer
    return push


def _status(answer, kind):
    plex_playlists.push_wrapup_playlist = _push(answer)
    media_server.active_kind = lambda settings=None: kind
    try:
        asyncio.run(wrapup_router.push_playlist({"scope": "all", "period": "30d"},
                                                user=SimpleNamespace(id=UID, plex_username="Member")))
    except HTTPException as e:
        return e.status_code, e.detail
    return 200, ""


try:
    code, detail = _status({"skipped": "no_jellyfin_account"}, "jellyfin")
    check("a Jellyfin member without a linked account is told so - not 'not found in Plex'",
          code == 409 and "Jellyfin" in detail and "Plex" not in detail, detail)
    code, detail = _status({"pushed": []}, "jellyfin")
    check("nothing found: the message names Jellyfin on a Jellyfin install",
          code == 404 and detail.endswith("in Jellyfin"), detail)
    code, detail = _status({"pushed": []}, "plex")
    check("and Plex on a Plex install", code == 404 and detail.endswith("in Plex"), detail)
finally:
    wrapup_store.load, plex_playlists.push_wrapup_playlist, media_server.active_kind = _saved

check.done()
