"""The curator's voice per user (services/curator_voice): the three voices,
the guardrails every voice carries, where each surface gets it from, and
the Settings endpoint. The voices' wording was measured on replayed chats
(tests/benchmarks/tone_bench.py); this suite pins the wiring, not the taste.
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio
import random

from fastapi import HTTPException

conn = app_db()

from src.database.models import User  # noqa: E402
from src.routers import users as users_router  # noqa: E402
from src.services import chat_starters as cs  # noqa: E402
from src.services import curator_voice as cv  # noqa: E402
from src.services import proactive_messages as pm  # noqa: E402
from src.services.llm_utils import language_directive  # noqa: E402
from src.services.setup_wizard import CURATOR_SYSTEM_PROMPT  # noqa: E402

with conn.get_db_session() as db:
    db.add_all([User(id=1, plex_user_id="p1", plex_username="owner", is_admin=True),
                User(id=2, plex_user_id="p2", plex_username="member", is_admin=False)])
    db.commit()

# ── persona: the baked lines it rewrites are really there ─────────────────
for line in (cv._BAKED_IDENTITY, cv._BAKED_BRUTAL, cv._BAKED_WHY, cv._BAKED_PATTERN):
    check(f"the baked persona still has: {line[:48]}…", line in CURATOR_SYSTEM_PROMPT)

for v in cv.VOICES:
    p = cv.persona(v)
    check(f"{v}: the voice line is in the persona", cv.VOICE[v] in p)
    check(f"{v}: every guardrail is in the persona", all(g in p for g in cv.GUARDRAILS))
    check(f"{v}: no 'brutally honest', no 'why behind viewing choices', no 'mood-based viewing'",
          "brutally honest" not in p and "*why* behind viewing choices" not in p
          and "mood-based viewing" not in p)
    t = cv.chat_tone(v)
    check(f"{v}: the chat tone carries the voice and every guardrail",
          cv.VOICE[v] in t and all(g in t for g in cv.GUARDRAILS))
    check(f"{v}: the short persona names the voice and the guardrails",
          cv.VOICE[v] in cv.short_persona(v) and cv.WORK in cv.short_persona(v))
check("only the elite persona keeps 'uncompromising'",
      "uncompromising" in cv.persona("elite") and "uncompromising" not in cv.persona("neutral")
      and "uncompromising" not in cv.persona("friendly"))
check("an unknown voice reads as neutral", cv.chat_tone("shouty") == cv.chat_tone("neutral"))

# ── whose voice: a pick wins, else the role's default ─────────────────────
check("the owner (admin) keeps the old edge until they pick", cv.voice_for(1) == "elite")
check("a household member starts neutral", cv.voice_for(2) == "neutral")
check("a user the database does not know reads as neutral", cv.voice_for(99) == "neutral")
cv.set_voice(2, "Friendly")
check("a pick is stored per user, case-folded", cv.voice_for(2) == "friendly" and cv.voice_for(1) == "elite")
try:
    cv.set_voice(2, "shouty")
    check("an unknown voice is refused", False)
except ValueError:
    check("an unknown voice is refused", cv.voice_for(2) == "friendly")

# ── the Settings endpoint ─────────────────────────────────────────────────
owner = User(id=1, plex_user_id="p1", plex_username="owner", is_admin=True)
got = users_router.get_curator_voice(user=owner)
check("GET: the owner's default, marked as not picked",
      got["voice"] == "elite" and got["picked"] is False and got["voices"] == list(cv.VOICES))
got = users_router.set_curator_voice(users_router.CuratorVoiceUpdate(voice="neutral"), user=owner)
check("POST: the pick comes back and sticks",
      got == {"voice": "neutral", "picked": True, "voices": list(cv.VOICES)}
      and users_router.get_curator_voice(user=owner)["picked"] is True)
try:
    users_router.set_curator_voice(users_router.CuratorVoiceUpdate(voice="x" * 99), user=owner)
    check("POST: an unknown voice is a 400", False)
except HTTPException as e:
    check("POST: an unknown voice is a 400, its echo cut short", e.status_code == 400 and len(e.detail) < 60)

# ── proactive: the voice picks the registers and the system prompt ────────
rng = random.Random(7)
friendly = {pm._pick_register(None, rng, allowed=cv.REGISTERS["friendly"]) for _ in range(300)}
check("a friendly voice never draws the provocative register", friendly == {"curious", "warm"}, friendly)
check("no allowed list draws all five, as before",
      {pm._pick_register(None, rng) for _ in range(400)} == {"provocative", "curious", "dry", "warm", "analytic"})
check("a one-register pool still answers when the previous was that register",
      pm._pick_register("warm", rng, allowed=("warm",)) == "warm")
sp = pm._build_system_prompt("Respond in English.", "neutral")
check("the proactive system prompt carries the voice's identity and the guardrails",
      cv.IDENTITY["neutral"] in sp and cv.WORK in sp and "Respond in English." in sp)
check("without a voice it is the plain curator, as before",
      pm._build_system_prompt("Respond in English.").startswith("You are Curatarr, a personal media curator."))
marathon = pm._compose_prompt({"type": "music_marathon", "hours": 3, "artist": "Some Band"}, "")
check("the music-marathon nudge asks about the music, not for a diagnosis",
      marathon and "mood, obsession" not in marathon and "the artist or the record" in marathon)


# ── starters: the user's voice reaches the request ────────────────────────
class _Resp:
    status_code = 200

    def json(self):
        return {"message": {"content": "[]"}}


class _Client:
    sent = []

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, json=None, **k):
        _Client.sent.append(json)
        return _Resp()


orig = (cs.httpx.AsyncClient, cs.collect_facts)
cs.httpx.AsyncClient = _Client
cs.collect_facts = lambda uid, now=None: [{"kind": "stalled_series", "title": "Vinland Saga"},
                                           {"kind": "now", "weekday": "Monday", "daypart": "evening"}]
try:
    asyncio.run(cs.generate_starters(2))
finally:
    cs.httpx.AsyncClient, cs.collect_facts = orig
msgs = _Client.sent[0]["messages"] if _Client.sent else []
check("the starter request opens with the member's voice as its system prompt",
      len(msgs) == 2 and msgs[0]["role"] == "system" and msgs[0]["content"] == cv.persona("friendly"))
check("and the starter prompt carries that voice's phrase",
      len(msgs) == 2 and cv.STARTER_VOICE["friendly"] in msgs[1]["content"])

# ── German: one register of address ───────────────────────────────────────
check("the language rule asks for 'du' in German", "(du)" in language_directive("de"))

check.done()
