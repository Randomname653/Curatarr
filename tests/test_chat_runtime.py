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
    lp._reset_lanes()
    lp._last_interactive = 0.0


def _db():
    # StaticPool: one in-memory DB for every thread, since services now run
    # their session work on run_read / run_write workers.
    from sqlalchemy.pool import StaticPool
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
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

    async def slow_evict(target, url=None):
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
        assert lp.lane().active == 0 and lp.lane().event().is_set(), \
            "enrichment must resume once nobody holds the curator"
        # The next request gets the GPU instead of queueing for ever.
        async def fast_evict(target, url=None):
            return None
        lp.evict_others = fast_evict
        await asyncio.wait_for(lp.curator_start("chat retry"), timeout=1.0)
        lp.curator_done()
        if lp.lane().evict_task:
            lp.lane().evict_task.cancel()

    try:
        asyncio.run(scenario())
    finally:
        lp.evict_others = orig
        _fresh_gate()


def test_an_eviction_error_releases_the_slot_and_propagates():
    orig = lp.evict_others

    async def broken_evict(target, url=None):
        raise RuntimeError("ollama down")

    async def scenario():
        _fresh_gate()
        lp.evict_others = broken_evict
        try:
            await lp.curator_start("chat")
            raise AssertionError("the error must reach the caller")
        except RuntimeError:
            pass
        assert not lp.curator_busy() and lp.lane().active == 0
        if lp.lane().evict_task:
            lp.lane().evict_task.cancel()

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
    y = block.index("if gate_contested(_run_lane):")
    yield_part = block[y:block.index("item = cand[\"item\"]", y)]
    assert yield_part.index("_holds_gate = False") < yield_part.index("curator_done(lane=_run_lane)") \
        < yield_part.index("await curator_start") < yield_part.index("_holds_gate = True"), \
        "drop the flag before releasing, raise it only once re-acquired"
    fin = block[block.rindex("finally:"):]
    assert "if _holds_gate:" in fin
    assert fin.index("if _holds_gate:") < fin.index("curator_done(lane=_run_lane)")

    # What the flag prevents: a done() without holding frees someone else's
    # slot, letting a third caller onto the GPU next to the current holder.
    async def scenario():
        _fresh_gate()

        async def noop(target, url=None):
            return None
        orig = lp.evict_others
        lp.evict_others = noop
        try:
            await lp.curator_start("chat")         # the holder after the yield
            lp.curator_done()                      # the old finally's stray release
            assert not lp.curator_busy(), "the stray release freed the chat's slot"
        finally:
            lp.evict_others = orig
            if lp.lane().evict_task:
                lp.lane().evict_task.cancel()

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
        return asyncio.run(chat.send_message(ChatMessage(message="hello"), user))
    finally:
        chat._send_message_guarded, lane.curator_available = orig


def test_a_second_submit_gets_409_before_any_work():
    from src.services import rate_limit as rl
    calls = []

    async def guarded(message, user, _rl, thread_id):
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

    async def guarded(message, user, _rl, thread_id):
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
    # A disconnect while queued is exercised for real in section 9.


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

    async def fake_run(uid, prompt, media_category=None, quiet_s=0.0):
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


# ── 9. the chat stream: heartbeat, one-commit turns, the guard to the end ───
#
# The real _send_message_guarded → generate() pipeline, with every outside
# dependency stubbed: a MockTransport plays Ollama's NDJSON, the GPU gate
# and the context sources (taste, RAG, memories) are no-ops, and the turn
# lands in a shared in-memory SQLite.

_UID = 515151


@contextmanager
def _stream_rig(lines=(), first_delay=0.0, delay=0.0, status=200, connect_error=False,
                heartbeat=None, think_tags=None, queue_forever=False, header_delay=0.0,
                fail_first=None, fail_mid=None):
    import httpx
    from sqlalchemy.pool import StaticPool
    import src.database.connection as conn
    import src.routers.chat as chat
    import src.services.episodic_memory as em
    from src.services import rate_limit as rl

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    events = []

    @contextmanager
    def db_session():
        s = Session()
        try:
            yield s
            s.commit()
        finally:
            s.close()

    async def none(*a, **k):
        return ""

    async def empty(*a, **k):
        return []

    async def curator_start(owner="", **k):
        events.append("curator_start")
        if queue_forever:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("curator_start cancelled")
                raise
        return lp.lane()        # the lane the turn holds

    async def health(model, url=None):
        return {}

    async def body():
        for i, line in enumerate(lines):
            await asyncio.sleep(first_delay if i == 0 else delay)
            if i == 1 and fail_mid is not None:
                raise fail_mid
            yield (json.dumps(line) + "\n").encode()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            async for chunk in body():
                yield chunk

    async def handler(request):
        events.append(("request", str(request.url),
                       json.loads(request.content).get("options", {}).get("num_ctx")))
        if fail_first is not None and sum(1 for e in events if e[0] == "request") == 1:
            fail_first(request)
        # Ollama sends its headers with the first token: a load or a long
        # prompt eval happens before the response even starts.
        await asyncio.sleep(header_delay)
        if connect_error:
            raise httpx.ConnectError("refused", request=request)
        if status != 200:
            return httpx.Response(status, json={"error": "model not found"})
        return httpx.Response(200, stream=Body())

    real_persist = chat._persist_turn

    def persist(*a, **k):
        events.append(("persist", rl.CHAT_IN_FLIGHT.busy(_UID)))
        return real_persist(*a, **k)

    real_client = httpx.AsyncClient
    patches = [
        (conn, "get_db_session", db_session),
        (chat, "get_user_taste_context", none),
        (chat, "_get_rag_context", none),
        (chat, "_match_library_title", lambda m: None),
        (chat, "_persist_turn", persist),
        (chat, "_schedule_post_turn", lambda *a: events.append("post_turn")),
        (em, "retrieve_memories", empty),
        (lp, "curator_start", curator_start),
        (lp, "curator_done", lambda **k: events.append("curator_done")),
        (lp, "curator_busy", lambda: False),
        (lp, "check_curator_vram_health", health),
        (httpx, "AsyncClient",
         lambda **kw: real_client(transport=httpx.MockTransport(handler))),
    ]
    if heartbeat is not None:
        patches.append((chat, "_HEARTBEAT_S", heartbeat))
    if think_tags is not None:
        patches.append((chat._cfg, "LLM_THINK_TAGS", think_tags))
    saved = [(obj, name, getattr(obj, name)) for obj, name, _ in patches]
    for obj, name, value in patches:
        setattr(obj, name, value)

    async def start(text="hi"):
        from src.schemas.chat import ChatMessage
        rl.CHAT_IN_FLIGHT.leave(_UID)
        rl.CHAT_IN_FLIGHT.enter_or_409(_UID)
        user = types.SimpleNamespace(id=_UID)
        return await chat._send_message_guarded(
            ChatMessage(message=text), user, rl, "general")

    def rows():
        s = Session()
        try:
            return [(m.role, m.content) for m in
                    s.query(ConversationMessage).order_by(ConversationMessage.id)]
        finally:
            s.close()

    try:
        yield types.SimpleNamespace(start=start, rows=rows, events=events,
                                    busy=lambda: rl.CHAT_IN_FLIGHT.busy(_UID))
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)
        rl.CHAT_IN_FLIGHT.leave(_UID)


def _frames(rig, text="hi"):
    async def run():
        resp = await rig.start(text)
        return resp, [f async for f in resp.body_iterator]
    return asyncio.run(run())


def _tokens(frames):
    return "".join(json.loads(f[6:]).get("token", "") for f in frames if f.startswith("data: "))


_OK = [{"message": {"content": "Hello"}}, {"message": {"content": " there"}}, {"done": True}]


def test_a_finished_turn_is_one_commit_and_the_guard_is_held_through_it():
    with _stream_rig(_OK) as rig:
        resp, frames = _frames(rig)
        assert frames[0] == 'data: {"thread": "general"}\n\n', "the thread is named first"
        assert frames[-1] == 'data: {"done": true}\n\n'
        assert _tokens(frames) == "Hello there"
        assert rig.rows() == [("user", "hi"), ("assistant", "Hello there")]
        # The GPU slot goes back as soon as Ollama is done; the in-flight
        # guard only after the writer thread has the turn.
        assert rig.events.index("curator_done") < rig.events.index(("persist", True)), rig.events
        assert rig.events[-1] == "post_turn"
        assert not rig.busy(), "the guard outlived the stream"
        assert resp.headers.get("x-accel-buffering") == "no"
        assert resp.headers.get("cache-control") == "no-cache"


def test_an_ollama_error_is_shown_but_never_persisted():
    with _stream_rig(status=404) as rig:
        _, frames = _frames(rig)
        assert "could not start this reply" in _tokens(frames)
        assert frames[-1] == 'data: {"done": true}\n\n'
        assert rig.rows() == [], "an error line poisons the next turn's context"
        assert "post_turn" not in rig.events, "memory extraction must not see an error"
        assert "curator_done" in rig.events and not rig.busy()

    with _stream_rig(connect_error=True) as rig:
        _, frames = _frames(rig)
        assert "Ollama not reachable" in _tokens(frames)
        assert rig.rows() == [] and "post_turn" not in rig.events and not rig.busy()


def test_a_failed_write_warns_and_still_finishes_the_turn():
    import src.routers.chat as chat
    with _stream_rig(_OK) as rig:
        # Only the turn's write fails: the pre-stream reads open sessions of
        # their own too, so a get_db_session that always raises would fail
        # the turn before it streams.
        def locked(*a, **k):
            raise RuntimeError("database is locked")

        chat._persist_turn = locked           # restored by the rig
        _, frames = _frames(rig)
        assert _tokens(frames) == "Hello there", "the streamed reply stays intact"
        assert any("could not be saved" in f for f in frames)
        assert frames[-1] == 'data: {"done": true}\n\n', "no done frame read as a dropped connection"
        assert "post_turn" not in rig.events and not rig.busy()


def test_a_cold_load_keeps_the_stream_alive():
    # Minutes of model load / prompt eval before the first line: comment
    # frames must keep flowing or the client watchdog (45 s) and nginx (60 s)
    # cut a healthy turn.
    with _stream_rig(_OK, first_delay=0.35, heartbeat=0.05) as rig:
        _, frames = _frames(rig)
        first_token = next(i for i, f in enumerate(frames) if '"token"' in f)
        assert ": keepalive\n\n" in frames[:first_token], frames
        assert _tokens(frames) == "Hello there"


def test_waiting_for_ollamas_headers_keeps_the_stream_alive():
    # 2026-10-22: Ollama answers with headers only once the first token is
    # ready. The heartbeat ran inside the response only, so 46 s of prompt
    # eval were silent and the client's 45 s watchdog dropped the reply.
    with _stream_rig(_OK, header_delay=0.35, heartbeat=0.05) as rig:
        _, frames = _frames(rig)
        first_token = next(i for i, f in enumerate(frames) if '"token"' in f)
        assert frames[:first_token].count(": keepalive\n\n") >= 3, frames
        assert _tokens(frames) == "Hello there"


def test_a_disconnect_while_ollama_has_not_answered_releases_everything():
    with _stream_rig(_OK, header_delay=5.0, heartbeat=0.05) as rig:
        _cancel_after(rig, lambda f: f == ": keepalive\n\n")
        assert "curator_done" in rig.events, "the curator slot stayed held"
        assert rig.rows() == [] and not rig.busy()


def _two_hosts():
    """The PC (large, 16k) answers the chat role until the test says it left."""
    import src.routers.chat as chat
    from src.services.ollama_hosts import OllamaHost
    pc = OllamaHost(url="http://pc:11434", profile="large", num_ctx=16384, gpu={"num_gpu": 99})
    nas = OllamaHost(url="http://nas:30068", profile="small", num_ctx=8192, gpu={})
    current = [pc]
    saved = chat.host_for
    chat.host_for = lambda role: current[0]
    return pc, nas, current, lambda: setattr(chat, "host_for", saved)


def test_the_pc_leaving_before_the_first_word_retries_on_the_main_host():
    from src.services.pc_guard import PcLost
    pc, nas, current, restore = _two_hosts()

    def leave(request):
        current[0] = nas                      # the routing now answers the NAS
        raise PcLost("the gaming PC stopped taking work", request=request)
    try:
        with _stream_rig(_OK, fail_first=leave) as rig:
            _, frames = _frames(rig)
            reqs = [e for e in rig.events if isinstance(e, tuple) and e[0] == "request"]
            assert [r[1] for r in reqs] == ["http://pc:11434/api/chat", "http://nas:30068/api/chat"], reqs
            assert reqs[1][2] == 8192, "the retry carries the NAS's window"
            assert any("answering on this server" in f for f in frames), frames
            assert _tokens(frames) == "Hello there"
            assert rig.rows()[-1] == ("assistant", "Hello there"), "the retried reply is saved"
    finally:
        restore()


def test_the_pc_leaving_mid_reply_ends_the_turn_unsaved():
    from src.services.pc_guard import PcLost
    pc, nas, current, restore = _two_hosts()
    try:
        with _stream_rig(_OK, fail_mid=PcLost("gone")) as rig:
            _, frames = _frames(rig)
            reqs = [e for e in rig.events if isinstance(e, tuple) and e[0] == "request"]
            assert len(reqs) == 1, "no retry once words went out"
            assert any("went away in the middle of this reply" in f for f in frames), frames
            assert rig.rows() == [], "half a reply is never saved"
            assert frames[-1] == 'data: {"done": true}\n\n'
    finally:
        restore()


def test_a_think_block_keeps_the_stream_alive():
    # Lines keep arriving (so the read never times out) but the think filter
    # swallows them all: the heartbeat has to count what the CLIENT got.
    think = ([{"message": {"content": "<think>"}}]
             + [{"message": {"content": "hmm "}} for _ in range(12)]
             + [{"message": {"content": "</think>Answer"}}, {"done": True}])
    with _stream_rig(think, delay=0.03, heartbeat=0.1, think_tags=True) as rig:
        _, frames = _frames(rig)
        first_token = next(i for i, f in enumerate(frames) if '"token"' in f)
        assert ": keepalive\n\n" in frames[:first_token], frames
        assert _tokens(frames).strip() == "Answer"


def _cancel_after(rig, predicate):
    """Consume the stream in a task, cancel it once ``predicate(frame)``
    holds — what Starlette does when the client goes away."""
    async def run():
        resp = await rig.start()
        seen = asyncio.Event()

        async def consume():
            async for f in resp.body_iterator:
                if predicate(f):
                    seen.set()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(seen.wait(), 5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        for _ in range(3):
            await asyncio.sleep(0)          # let the cancelled acquire unwind
    asyncio.run(run())


def test_a_disconnect_mid_reply_persists_nothing_and_releases_everything():
    lines = [{"message": {"content": "Hel"}}] + [{"message": {"content": "lo"}}] * 50 + [{"done": True}]
    with _stream_rig(lines, delay=0.02) as rig:
        _cancel_after(rig, lambda f: '"token"' in f)
        assert rig.rows() == [], "half a reply (or its lone user turn) was kept"
        assert "curator_done" in rig.events and "post_turn" not in rig.events
        assert not rig.busy()


def test_a_disconnect_while_queued_releases_slot_and_guard():
    with _stream_rig(_OK, heartbeat=0.05, queue_forever=True) as rig:
        _cancel_after(rig, lambda f: f == ": keepalive\n\n")
        assert "curator_start cancelled" in rig.events, "the queued acquire was left waiting"
        assert "curator_done" not in rig.events, "released a slot it never held"
        assert rig.rows() == [] and not rig.busy()


def test_one_failing_post_turn_step_does_not_skip_the_others():
    import src.routers.chat as chat
    import src.services.bg_tasks as bg
    import src.services.episodic_memory as em
    names = []

    def track(coro, name=None):
        coro.close()
        names.append(name)

    def boom(*a, **k):
        raise RuntimeError("extraction scheduler down")

    orig = bg.track_task, em.schedule_thread_extraction
    bg.track_task, em.schedule_thread_extraction = track, boom
    try:
        chat._schedule_post_turn(_UID, "general", "keep it", "ok", None)
    finally:
        bg.track_task, em.schedule_thread_extraction = orig
    assert names == ["check_protection_intent_bg", "check_verification_response"], names

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
