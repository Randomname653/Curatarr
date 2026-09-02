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


def test_starters_speak_in_the_users_voice():
    """Clicking a chip sends its text as the USER'S message. The first cut
    had the model write in the curator's voice — the user then opened chats
    by narrating their own taste to themselves ("Looking back at your
    Psycho-Pass stall…") and the curator read the "you" as itself. The
    prompt must demand first person and name the failure."""
    assert "USER'S first-person voice" in cs._PROMPT
    assert "SENDS ITS TEXT AS THE USER'S OWN MESSAGE" in cs._PROMPT
    assert "CURATOR'S voice" not in cs._PROMPT


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
