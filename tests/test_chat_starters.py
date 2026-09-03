"""Starters that don't all sound the same — because code enforces it.

The proactive bubbles taught the lesson: same register, same shapes,
every third one interchangeable, and the same ignored bubble squatting
in the queue for days. The starter pool answers with rules that live at
the output, not in the prompt: one form each, a concrete fact each, no
repeated openings, and decay (TTL + impression cap + click retirement).
These tests pin the enforcement and the decay — the parts a model
cannot sweet-talk its way past.
"""

import asyncio
import datetime as dt
import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.services import chat_starters as cs


def _src(rel: str) -> str:
    return (_ROOT / "src" / rel).read_text(encoding="utf-8")


# ── dayparts ───────────────────────────────────────────────────────────────

def test_dayparts_cover_the_clock():
    assert cs.current_daypart(dt.datetime(2026, 9, 1, 7)) == "morning"
    assert cs.current_daypart(dt.datetime(2026, 9, 1, 13)) == "day"
    assert cs.current_daypart(dt.datetime(2026, 9, 1, 20)) == "evening"
    assert cs.current_daypart(dt.datetime(2026, 9, 1, 23)) == "night"
    assert cs.current_daypart(dt.datetime(2026, 9, 1, 3)) == "night"


# ── JSON extraction survives model formatting habits ──────────────────────

def test_json_extraction_strips_fences_and_noise():
    fenced = 'Sure!\n```json\n[{"text": "x", "form": "question"}]\n```'
    assert cs._extract_json_array(fenced)[0]["form"] == "question"
    noisy = 'here: [ {"text": "y"} ] done'
    assert cs._extract_json_array(noisy)[0]["text"] == "y"


# ── the enforcement gate ───────────────────────────────────────────────────

class _FakeResp:
    status_code = 200
    def __init__(self, payload):
        self._p = payload
    def json(self):
        return {"message": {"content": self._p}}


class _FakeClient:
    payload = "[]"
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, *a, **k):
        return _FakeResp(_FakeClient.payload)


class _FakeQuery:
    def __init__(self, rows): self._rows = rows
    def filter(self, *a, **k): return self
    def order_by(self, *a, **k): return self
    def limit(self, n): return self
    def all(self): return self._rows
    def count(self): return len(self._rows)


class _FakeSession:
    added = []
    def __init__(self, rows=None): self._rows = rows or []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def query(self, *cols): return _FakeQuery(self._rows)
    def add(self, row): _FakeSession.added.append(row)
    def commit(self): pass


def test_the_gate_drops_duplicate_forms_missing_facts_and_repeated_openings():
    import json as _json
    batch = [
        {"text": "Three weeks since you touched Vinland Saga — did it lose you at the farm arc?",
         "form": "question", "daypart": "any", "fact": "stalled_series Vinland Saga"},
        # duplicate form — must be dropped even though otherwise valid
        {"text": "Still thinking about that Mushoku Tensei finale you binged?",
         "form": "question", "daypart": "any", "fact": "current_binge"},
        # no anchoring fact — generic, dropped
        {"text": "Fancy watching something new tonight? Anything at all?",
         "form": "tonight_pick", "daypart": "evening", "fact": ""},
        # same opening words as the first — dropped
        {"text": "Three weeks since you finished anything. Explain yourself.",
         "form": "challenge", "daypart": "any", "fact": "stalled_series"},
        # valid, different form
        {"text": "Seven Mushoku Tensei episodes this week. That is a commitment, not a hobby.",
         "form": "observation", "daypart": "any", "fact": "current_binge 7 plays"},
        # invalid form name — dropped
        {"text": "An evening pick: the quiet one you keep skipping.",
         "form": "poem", "daypart": "evening", "fact": "last_watched"},
    ]
    _FakeClient.payload = _json.dumps(batch)
    _FakeSession.added = []

    orig_client, orig_session = cs.httpx.AsyncClient, cs.get_db_session
    orig_facts = cs.collect_facts
    cs.httpx.AsyncClient = _FakeClient
    cs.get_db_session = lambda: _FakeSession(rows=[])
    cs.collect_facts = lambda uid, now=None: [
        {"kind": "stalled_series", "title": "Vinland Saga"},
        {"kind": "now", "weekday": "Monday", "daypart": "evening"}]
    try:
        inserted = asyncio.run(cs.generate_starters(1))
    finally:
        cs.httpx.AsyncClient = orig_client
        cs.get_db_session = orig_session
        cs.collect_facts = orig_facts

    assert inserted == 2, [r.text for r in _FakeSession.added]
    forms = {r.form for r in _FakeSession.added}
    assert forms == {"question", "observation"}
    for r in _FakeSession.added:
        assert r.fact_used
        assert r.expires_at is not None, "every starter must carry a TTL"


def test_an_unparseable_batch_inserts_nothing():
    _FakeClient.payload = "I would rather chat about this in prose."
    _FakeSession.added = []
    orig_client, orig_session = cs.httpx.AsyncClient, cs.get_db_session
    orig_facts = cs.collect_facts
    cs.httpx.AsyncClient = _FakeClient
    cs.get_db_session = lambda: _FakeSession(rows=[])
    cs.collect_facts = lambda uid, now=None: [
        {"kind": "last_watched", "title": "X"}, {"kind": "now"}]
    try:
        assert asyncio.run(cs.generate_starters(1)) == 0
        assert _FakeSession.added == []
    finally:
        cs.httpx.AsyncClient = orig_client
        cs.get_db_session = orig_session
        cs.collect_facts = orig_facts


# ── weekday guard: day-named openers die with their day ────────────────────

def test_day_naming_detection_spares_habitual_plurals():
    assert cs._named_day("It is Wednesday evening. Let me pick.") == "wednesday"
    assert cs._named_day("Mittwoch also. Zeit fuer was Dichtes.") == "mittwoch"
    assert cs._named_day("You only ever binge on sundays, admit it.") is None
    assert cs._named_day("No day mentioned here at all.") is None


def test_end_of_local_day_lies_within_the_next_24_utc_hours():
    e = cs._end_of_local_day_utc()
    now = dt.datetime.utcnow()
    assert now < e <= now + dt.timedelta(hours=24, minutes=1)


def test_the_gate_scopes_day_named_openers_to_their_day():
    import json as _json
    today_en = cs._DAY_NAMES[dt.datetime.now().weekday()][0]
    wrong = next(names[0] for wd, names in cs._DAY_NAMES.items()
                 if wd != dt.datetime.now().weekday())
    batch = [
        {"text": f"It is {today_en.capitalize()} evening. Time for something dense.",
         "form": "tonight_pick", "daypart": "evening", "fact": "now"},
        # names a day that is NOT today — hallucinated, must be dropped
        {"text": f"A {wrong.capitalize()} classic: defend your comfort rewatch.",
         "form": "challenge", "daypart": "any", "fact": "rewatch pattern"},
        {"text": "Two stalled shows and a full watchlist. Pick a lane.",
         "form": "observation", "daypart": "any", "fact": "stalled_series"},
    ]
    _FakeClient.payload = _json.dumps(batch)
    _FakeSession.added = []
    orig_client, orig_session = cs.httpx.AsyncClient, cs.get_db_session
    orig_facts = cs.collect_facts
    cs.httpx.AsyncClient = _FakeClient
    cs.get_db_session = lambda: _FakeSession(rows=[])
    cs.collect_facts = lambda uid, now=None: [
        {"kind": "stalled_series", "title": "X"},
        {"kind": "now", "weekday": today_en.capitalize(), "daypart": "evening"}]
    try:
        inserted = asyncio.run(cs.generate_starters(1))
    finally:
        cs.httpx.AsyncClient = orig_client
        cs.get_db_session = orig_session
        cs.collect_facts = orig_facts

    texts = [r.text for r in _FakeSession.added]
    assert inserted == 2, texts
    assert not any(wrong in t.lower() for t in texts)
    by_day = {cs._named_day(r.text): r for r in _FakeSession.added}
    # day-named → local midnight, strictly sooner than the 48h default
    assert by_day[today_en].expires_at < dt.datetime.utcnow() + dt.timedelta(hours=25)
    assert by_day[None].expires_at > dt.datetime.utcnow() + dt.timedelta(hours=47)


def test_pick_retires_day_mismatched_leftovers():
    wrong = next(names[0] for wd, names in cs._DAY_NAMES.items()
                 if wd != dt.datetime.now().weekday())
    stale = cs.ChatStarter(id=1, user_id=1, form="tonight_pick", daypart="any",
                           impressions=0, text=f"It is {wrong.capitalize()} evening.")
    fresh = cs.ChatStarter(id=2, user_id=1, form="challenge", daypart="any",
                           impressions=0, text="Pick a lane tonight.")
    orig_session = cs.get_db_session
    cs.get_db_session = lambda: _FakeSession(rows=[stale, fresh])
    try:
        picked = cs.pick_starters(1, limit=3)
    finally:
        cs.get_db_session = orig_session
    assert [p["id"] for p in picked] == [2]
    assert stale.expires_at is not None, "must be hard-retired, not just skipped"


# ── decay is wired, on both pools ──────────────────────────────────────────

def test_selection_decays_and_respects_the_daypart():
    src = _src("services/chat_starters.py")
    assert "impressions < MAX_IMPRESSIONS" in src
    assert "ChatStarter.expires_at > now" in src
    assert 'in_((dp, "any"))' in src
    assert "row.impressions = (row.impressions or 0) + 1" in src


def test_proactive_messages_gained_the_same_mortality():
    pm = _src("services/proactive_messages.py")
    assert "expires_at=now + timedelta(days=7)" in pm
    # surfacing counts, and ignored-past-cap or expired messages retire
    assert "m.impressions = (m.impressions or 0) + 1" in pm
    assert "ProactiveMessage.impressions >= 40" in pm


def test_the_custodian_owns_generation_and_the_gpu_gate():
    from src.services.data_custodian import _registry
    entry = next(t for t in _registry() if t.job_id == "chat_starters")
    assert entry.needs_llm, "starters need the model — must skip during games"
    assert entry.takes_task, "must report progress into the Activity card"
    assert entry.cadence_h == 12.0
    # and the request path never generates inline
    chat = _src("routers/chat.py")
    assert "asyncio.create_task(_refill(user.id))" in chat
    assert "is_game_running()" in chat


def test_starters_are_curator_openers_not_user_prompts():
    """The design settled on the third try: the chip makes the CURATOR say
    the line and the user replies (like proactive messages) — because
    curator-voiced text sent as the user's message swapped the roles on
    stage, and rewriting the voice to first person wasted the register.
    The prompt writes openers in the curator's voice with an optional
    title anchor; the reply turn re-injects the opener via the `starter`
    discuss context (server-owned row, ownership check, no persisted fake
    assistant turn)."""
    assert "OPENERS" in cs._PROMPT
    assert "Clicking one makes you SAY it" in cs._PROMPT
    assert "anchor_title" in cs._PROMPT

    chat = _src("routers/chat.py")
    assert 'return f"starter:{ctx.starter_id}"' in chat
    branch = chat[chat.index("Curator-opened conversation (starter chip)"):]
    branch = branch[:branch.index("Watched-title discussion")]
    # ownership check, opener re-injection, and the role clarification
    assert "ChatStarter.user_id == user_id" in branch
    assert "YOU (the curator) OPENED this conversation" in branch
    assert 'refers to the USER, not to you' in branch
    # an anchored opener grounds the discussion like a title click does
    assert "ensure_verified_data(active_title" in branch


def test_anchors_survive_the_gate_and_bad_ones_are_dropped():
    import json as _json
    batch = [
        {"text": "Thirty-three days since Psycho-Pass. Did the Sibyl System lose you?",
         "form": "question", "daypart": "any", "fact": "stalled_series",
         "anchor_title": "Psycho-Pass", "anchor_media_type": "anime"},
        {"text": "Sleep Token, seventy-three times in a fortnight. We should talk.",
         "form": "observation", "daypart": "any", "fact": "music_rotation",
         "anchor_title": "Sleep Token", "anchor_media_type": "playlist"},  # bad type -> nulled
    ]
    _FakeClient.payload = _json.dumps(batch)
    _FakeSession.added = []
    orig_client, orig_session = cs.httpx.AsyncClient, cs.get_db_session
    orig_facts = cs.collect_facts
    cs.httpx.AsyncClient = _FakeClient
    cs.get_db_session = lambda: _FakeSession(rows=[])
    cs.collect_facts = lambda uid, now=None: [
        {"kind": "stalled_series", "title": "Psycho-Pass"}, {"kind": "now"}]
    try:
        assert asyncio.run(cs.generate_starters(1)) == 2
    finally:
        cs.httpx.AsyncClient = orig_client
        cs.get_db_session = orig_session
        cs.collect_facts = orig_facts
    by_title = {r.anchor_title: r for r in _FakeSession.added}
    assert by_title["Psycho-Pass"].anchor_media_type == "anime"
    assert by_title["Sleep Token"].anchor_media_type is None


def test_streamed_status_lines_carry_no_emoji():
    """The frontend's emoji sweep cannot reach strings the BACKEND streams
    into the thinking panel — pre_stream_status lines are user-visible UI."""
    import re
    chat = _src("routers/chat.py")
    emoji = re.compile(r"[\U0001F000-\U0001FAFF✀-➿☀-⛿"
                       r"⬀-⯿←-⇿✔✅]")
    lines = chat.split("\n")
    offenders = []
    for i, line in enumerate(lines):
        window = "\n".join(lines[max(0, i - 2):i + 1])
        if "pre_stream_status.append" in window and emoji.search(line):
            offenders.append(line.strip()[:80])
    assert not offenders, offenders
