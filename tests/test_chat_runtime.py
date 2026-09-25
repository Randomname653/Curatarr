"""Chat runtime: the GPU slot, the in-flight guard, notification impressions,
verification answers and thread memory extraction.

No Ollama, no network: the eviction, the summariser and the database are
stubbed (a throwaway in-memory SQLite where rows matter).

    python tests/test_chat_runtime.py
"""
import asyncio
import json
import pathlib
import sys
import types
from contextlib import contextmanager
from datetime import datetime

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from fastapi import HTTPException                        # noqa: E402
from sqlalchemy import create_engine                     # noqa: E402
from sqlalchemy.orm import sessionmaker                  # noqa: E402

import src.services.llm_priority as lp                   # noqa: E402
from src.database.models import (                        # noqa: E402
    Base, ConversationMessage, DeletionProposal, ProactiveMessage)


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _fresh_gate():
    """Module state bound to the loop asyncio.run() is about to create."""
    lp._gpu_gate = None
    lp._event = None
    lp._active = 0
    lp._gate_owner = ""
    lp._gate_waiters = 0
    lp._curator_evict_task = None


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    @contextmanager
    def fake_db_session():
        yield session

    return session, fake_db_session


# ── 1. a cancel inside curator_start's eviction must not leak the slot ──────

def test_a_cancel_during_eviction_releases_the_gpu_slot():
    orig = lp.evict_others
    stuck = {"entered": False}

    async def slow_evict(target):
        stuck["entered"] = True
        await asyncio.Event().wait()          # a /api/ps that never answers

    async def scenario():
        _fresh_gate()
        lp.evict_others = slow_evict
        t = asyncio.create_task(lp.curator_start("chat"))
        for _ in range(20):
            await asyncio.sleep(0)
            if stuck["entered"]:
                break
        assert stuck["entered"] and lp.curator_busy()
        t.cancel()                              # the client disconnected
        try:
            await t
        except asyncio.CancelledError:
            pass
        assert not lp.curator_busy(), "the slot leaked to a dead chat stream"
        assert lp._active == 0 and lp._get_event().is_set(), \
            "enrichment must resume once nobody holds the curator"
        # The next request gets the GPU instead of queueing for ever.
        async def fast_evict(target):
            return None
        lp.evict_others = fast_evict
        await asyncio.wait_for(lp.curator_start("chat retry"), timeout=1.0)
        lp.curator_done()
        if lp._curator_evict_task:
            lp._curator_evict_task.cancel()

    try:
        asyncio.run(scenario())
    finally:
        lp.evict_others = orig
        _fresh_gate()


def test_an_eviction_error_releases_the_slot_and_propagates():
    orig = lp.evict_others

    async def broken_evict(target):
        raise RuntimeError("ollama down")

    async def scenario():
        _fresh_gate()
        lp.evict_others = broken_evict
        try:
            await lp.curator_start("chat")
            raise AssertionError("the error must reach the caller")
        except RuntimeError:
            pass
        assert not lp.curator_busy() and lp._active == 0
        if lp._curator_evict_task:
            lp._curator_evict_task.cancel()

    try:
        asyncio.run(scenario())
    finally:
        lp.evict_others = orig
        _fresh_gate()


# ── 2. the deletion funnel's yield must not release a slot it gave away ─────

def test_the_funnel_only_releases_a_slot_it_holds():
    src = _src("src/services/recommendations_engine.py")
    start = src.index('_gate_label = f"deletion scan: {category}"')
    block = src[start:src.index("return final_proposals", start)]
    y = block.index("if gate_contested():")
    yield_part = block[y:block.index("item = cand[\"item\"]", y)]
    assert yield_part.index("_holds_gate = False") < yield_part.index("curator_done()") \
        < yield_part.index("await curator_start") < yield_part.index("_holds_gate = True"), \
        "drop the flag before releasing, raise it only once re-acquired"
    fin = block[block.rindex("finally:"):]
    assert "if _holds_gate:" in fin
    assert fin.index("if _holds_gate:") < fin.index("curator_done()")

    # What the flag prevents: a done() without holding frees someone else's
    # slot, letting a third caller onto the GPU next to the current holder.
    async def scenario():
        _fresh_gate()

        async def noop(target):
            return None
        orig = lp.evict_others
        lp.evict_others = noop
        try:
            await lp.curator_start("chat")         # the holder after the yield
            lp.curator_done()                      # the old finally's stray release
            assert not lp.curator_busy(), "the stray release freed the chat's slot"
        finally:
            lp.evict_others = orig
            if lp._curator_evict_task:
                lp._curator_evict_task.cancel()

    asyncio.run(scenario())
    _fresh_gate()


# ── 3. one reply in flight, taken before anything is saved or built ─────────

def _chat_call(guarded):
    import src.routers.chat as chat
    import src.services.llm_lane as lane
    from src.schemas.chat import ChatMessage
    from src.services import rate_limit as rl
    orig = chat._send_message_guarded, lane.curator_available
    chat._send_message_guarded = guarded
    lane.curator_available = lambda: (True, "")    # the GPU is ours
    rl.reset("chat")
    try:
        user = types.SimpleNamespace(id=424242)
        return asyncio.run(chat.send_message(ChatMessage(message="hello"), user, None))
    finally:
        chat._send_message_guarded, lane.curator_available = orig


def test_a_second_submit_gets_409_before_any_work():
    from src.services import rate_limit as rl
    calls = []

    async def guarded(message, user, db, _rl, thread_id):
        calls.append(message.message)          # would save + build the context
        return "response"

    rl.CHAT_IN_FLIGHT.leave(424242)
    rl.CHAT_IN_FLIGHT.enter_or_409(424242)     # the first reply is still streaming
    try:
        _chat_call(guarded)
        raise AssertionError("expected 409")
    except HTTPException as e:
        assert e.status_code == 409
    finally:
        rl.CHAT_IN_FLIGHT.leave(424242)
    assert calls == [], "the double-submit must not persist or build anything"

    assert _chat_call(guarded) == "response" and calls == ["hello"]
    assert rl.CHAT_IN_FLIGHT.busy(424242), "the stream owns the guard now"
    rl.CHAT_IN_FLIGHT.leave(424242)


def test_a_failure_before_the_stream_releases_the_guard():
    from src.services import rate_limit as rl

    async def guarded(message, user, db, _rl, thread_id):
        raise RuntimeError("RAG exploded")

    rl.CHAT_IN_FLIGHT.leave(424242)
    try:
        _chat_call(guarded)
        raise AssertionError("expected the error")
    except RuntimeError:
        pass
    assert not rl.CHAT_IN_FLIGHT.busy(424242), "one pre-stream error locked the user out"


def test_an_unstarted_stream_releases_the_guard_on_collection():
    import gc
    import weakref
    import src.routers.chat as chat
    from src.services import rate_limit as rl

    async def gen(state):
        state["started"] = True
        yield "x"

    rl.CHAT_IN_FLIGHT.enter_or_409(424243)
    state = {"started": False}
    g = gen(state)
    weakref.finalize(g, chat._release_unstarted_chat, rl.CHAT_IN_FLIGHT, 424243, state)
    del g
    gc.collect()
    assert not rl.CHAT_IN_FLIGHT.busy(424243), "client gone before the first chunk"

    # A stream that ran owns its own release; a late collection must not
    # drop the NEXT turn's entry.
    rl.CHAT_IN_FLIGHT.enter_or_409(424243)
    chat._release_unstarted_chat(rl.CHAT_IN_FLIGHT, 424243, {"started": True})
    assert rl.CHAT_IN_FLIGHT.busy(424243)
    rl.CHAT_IN_FLIGHT.leave(424243)

    src = _src("src/routers/chat.py")
    assert "weakref.finalize(stream, _release_unstarted_chat" in src
    g0 = src.index("    async def generate() -> AsyncGenerator[str, None]:")
    pre = src[g0:src.index("# Slot acquired", g0)]
    assert 'await curator_start("chat")' in pre and "except BaseException:" in pre \
        and "CHAT_IN_FLIGHT.leave(user.id)" in pre, \
        "a disconnect while queued must release the in-flight guard too"


# ── 4. Respond must not consume a verification question ─────────────────────

def test_respond_leaves_a_verification_question_open_for_the_answer():
    js = _src("frontend/js/notifications.js")
    body = js[js.index("export async function respondToMessage"):]
    body = body[:body.index("\n}\n")]
    mark = body.index("/read`")
    guard = body.rindex("if (triggerType !== 'verification')", 0, mark)
    assert guard < mark, "Respond must not mark a verification question read"
    # …and the backend still matches only unanswered (unread) questions.
    chat = _src("src/routers/chat.py")
    assert "ProactiveMessage.read == False" in chat[chat.index("async def _check_verification_response"):
                                                   chat.index("def _revert_verification_claim")]


# ── 5. a background badge poll is not an impression ─────────────────────────

def test_only_a_shown_message_counts_an_impression():
    import src.services.proactive_messages as pm
    session, fake = _db()
    session.add(ProactiveMessage(user_id=1, trigger_type="binge_episode", message="Frieren?",
                                 read=False, created_at=datetime.utcnow(), impressions=0))
    session.commit()
    orig = pm.get_db_session
    pm.get_db_session = fake
    try:
        for _ in range(45):                       # 45 minutes of an open tab
            r = asyncio.run(pm.get_unread_messages(1))
        assert r["message"] and r["total"] == 1, "polls alone retired an unread message"
        assert session.query(ProactiveMessage).one().impressions == 0
        asyncio.run(pm.get_unread_messages(1, seen=True))
        assert session.query(ProactiveMessage).one().impressions == 1
    finally:
        pm.get_db_session = orig

    js = _src("frontend/js/notifications.js")
    assert "loadUnreadMessages(true)" in js and "'/api/messages/unread?seen=1'" in js
    assert "setInterval(loadUnreadMessages, 60_000)" in _src("frontend/js/auth.js"), \
        "the poll passes no seen flag"
    assert "seen: bool = False" in _src("src/routers/messages.py")


# ── 6. extraction anchors only on the user's own rows ───────────────────────

def _run_extract(thread_id, rows, user_id=1):
    import src.services.app_state as app_state
    import src.services.episodic_memory as em
    from src.config import settings
    session, fake = _db()
    for r in rows:
        session.add(r)
    session.add(ConversationMessage(user_id=user_id, role="user", content="I love it because of X",
                                    thread_id=thread_id, created_at=datetime.utcnow()))
    session.commit()
    prompts = []

    async def fake_run(uid, prompt, media_category=None):
        prompts.append(prompt)
        return True

    saved = (em.get_db_session, em._run_memory_extraction, app_state.get_state,
             app_state.set_state, settings.PRINCIPLES_ENABLED)
    em.get_db_session = fake
    em._run_memory_extraction = fake_run
    app_state.get_state = lambda k: None
    app_state.set_state = lambda k, v: None
    settings.PRINCIPLES_ENABLED = False
    try:
        asyncio.run(em.extract_memories_from_thread(user_id, thread_id))
    finally:
        (em.get_db_session, em._run_memory_extraction, app_state.get_state,
         app_state.set_state, settings.PRINCIPLES_ENABLED) = saved
    assert len(prompts) == 1
    return prompts[0]


def test_extraction_never_reads_another_users_message_or_proposal():
    theirs = ProactiveMessage(id=7, user_id=2, trigger_type="binge_episode",
                              message="SECRET of user two", read=False)
    p = _run_extract("proactive_message:7", [theirs])
    assert "SECRET of user two" not in p

    mine = ProactiveMessage(id=7, user_id=1, trigger_type="binge_episode",
                            message="Your own opener", read=False)
    assert "Your own opener" in _run_extract("proactive_message:7", [mine])

    dp = DeletionProposal(id=9, user_id=2, media_id="m", title="Their Private Film",
                          service="radarr", category="movie")
    assert "Their Private Film" not in _run_extract("deletion_proposal:9", [dp])


# ── 7. extraction: malformed items, partial writes, flush category ──────────

class _Resp:
    status_code = 200

    def __init__(self, content):
        self._c = content

    def json(self):
        return {"message": {"content": self._c}}


class _Client:
    content = "[]"

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _Resp(_Client.content)


def _extract_with(llm_items, write):
    import src.services.episodic_memory as em
    saved = (em.httpx.AsyncClient, em.write_memory, em.resolve_memory_conflicts)
    _Client.content = json.dumps(llm_items)
    em.httpx.AsyncClient = _Client
    em.write_memory = write

    async def no_conflicts(**k):
        return None
    em.resolve_memory_conflicts = no_conflicts
    try:
        return asyncio.run(em._run_memory_extraction(1, "prompt", media_category="music"))
    finally:
        em.httpx.AsyncClient, em.write_memory, em.resolve_memory_conflicts = saved


def test_a_malformed_item_is_skipped_not_fatal():
    written = []

    async def write(**k):
        written.append(k["content"])
        return len(written)

    ok = _extract_with(["just a string", {"content": "The user loves slow-burn sci-fi.",
                                          "type": "preference", "title": ""}], write)
    assert ok is True, "a junk item failed the window, so the next flush rewrites the rest"
    assert written == ["The user loves slow-burn sci-fi."]


def test_a_failure_after_a_write_consumes_the_window():
    written = []

    async def write(**k):
        if written:
            raise RuntimeError("db locked")
        written.append(k["content"])
        return 1

    ok = _extract_with([{"content": "The user values practical effects.", "title": ""},
                        {"content": "The user dislikes laugh tracks in sitcoms.", "title": ""}], write)
    assert ok is True, "a retry would store the first memory a second time"
    assert written == ["The user values practical effects."]

    async def always_fails(**k):
        raise RuntimeError("db locked")
    assert _extract_with([{"content": "The user values practical effects.", "title": ""}],
                         always_fails) is False, "nothing stored — the window is retried"


def test_flush_keeps_the_scheduled_media_category():
    import src.services.episodic_memory as em
    seen = []

    async def fake_extract(uid, tid, media_category=None):
        seen.append((uid, tid, media_category))

    async def scenario():
        em.schedule_thread_extraction(1, "general", media_category="music")
        await em.flush_thread_extraction(1, "general")
        em.schedule_thread_extraction(1, "t2", media_category="anime")
        await em.flush_all_pending_extractions()

    orig = em.extract_memories_from_thread
    em.extract_memories_from_thread = fake_extract
    try:
        asyncio.run(scenario())
    finally:
        em.extract_memories_from_thread = orig
    assert seen == [(1, "general", "music"), (1, "t2", "anime")], seen
    assert not em._pending_thread_categories


# ── 8. an empty library index is cached too ─────────────────────────────────

def test_an_empty_library_index_is_not_rebuilt_every_turn():
    import src.cache.metadata_cache as mcmod
    import src.routers.chat as chat
    builds = []

    class FakeCache:
        def __init__(self):
            builds.append(1)

        def get_cache(self, key):
            return None

        def close(self):
            pass

    orig = mcmod.MetadataCache
    mcmod.MetadataCache = FakeCache
    chat._LIB_TITLE_INDEX.update(ts=0.0, items=[])
    try:
        for _ in range(5):
            assert chat._library_title_index() == []
        assert len(builds) == 1, f"rebuilt {len(builds)}x for five chat turns"
        chat._LIB_TITLE_INDEX["ts"] -= chat._LIB_TITLE_EMPTY_TTL_S + 1
        chat._library_title_index()
        assert len(builds) == 2, "an empty index is retried after its short TTL"
    finally:
        mcmod.MetadataCache = orig
        chat._LIB_TITLE_INDEX.update(ts=0.0, items=[])


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{'all passed' if not fails else f'{fails} failed'}")
    sys.exit(1 if fails else 0)
