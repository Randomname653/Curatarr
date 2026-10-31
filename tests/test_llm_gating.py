"""LLM gating: priority, preemption, quiet windows, pre-gates and runner
options — the rules that keep the interactive user from waiting on
background work or on a model reload.

No Ollama, no network: eviction and /api/ps are stubbed.

    python tests/test_llm_gating.py
"""
import asyncio
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import src.services.llm_priority as lp                   # noqa: E402
from src.config import settings                          # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def _src(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _fresh():
    lp._reset_lanes()
    lp._last_interactive = 0.0


async def _no_evict(target, url=None):
    return None


def run(coro_fn):
    """asyncio.run with eviction stubbed and the idle-evict timer reaped."""
    async def wrapper():
        _fresh()
        orig = lp.evict_others
        lp.evict_others = _no_evict
        try:
            return await coro_fn()
        finally:
            lp.evict_others = orig
            for ln in lp.lanes():
                if ln.evict_task:
                    ln.evict_task.cancel()
    return asyncio.run(wrapper())


# ── 1. the gate serves interactive waiters first ────────────────────────────
print("priority gate")


async def _priority_order():
    order = []
    await lp.curator_start("holder")                        # background holder

    async def waiter(name, prio):
        await lp.curator_start(name, priority=prio)
        order.append(name)
        lp.curator_done()

    bg1 = asyncio.create_task(waiter("bg1", lp.BACKGROUND))
    bg2 = asyncio.create_task(waiter("bg2", lp.BACKGROUND))
    await asyncio.sleep(0)
    chat = asyncio.create_task(waiter("chat", lp.INTERACTIVE))
    await asyncio.sleep(0)
    lp.curator_done()                                       # holder leaves
    await asyncio.gather(bg1, bg2, chat)
    return order


order = run(_priority_order)
check("an interactive turn queued LAST is served FIRST", order == ["chat", "bg1", "bg2"], order)
check("...and background waiters keep their arrival order", order[1:] == ["bg1", "bg2"], order)


async def _cancelled_waiter_leaks_nothing():
    await lp.curator_start("holder")
    t = asyncio.create_task(lp.curator_start("gone", priority=lp.INTERACTIVE))
    await asyncio.sleep(0)
    t.cancel()
    await asyncio.sleep(0)
    lp.curator_done()
    free = not lp.curator_busy()
    await asyncio.wait_for(lp.curator_start("next"), timeout=1.0)
    lp.curator_done()
    return free


check("a waiter cancelled in the queue leaves the slot free",
      run(_cancelled_waiter_leaks_nothing))


# ── 2. preemption ───────────────────────────────────────────────────────────
print("preemption")
# The back-off behind a preempting turn is 5 s in the app; what the checks
# below pin is that it happens, not its length (each case used to wait it out).
check("a preempted call backs off 5 s behind the turn", lp.PREEMPT_BACKOFF_S == 5.0)
lp.PREEMPT_BACKOFF_S = 0.2


async def _preempt():
    runs = []
    started = asyncio.Event()

    async def call():
        runs.append("start")
        started.set()
        await asyncio.sleep(5)          # a long background decode
        runs.append("finished")
        return "done"

    async def call_fast():
        runs.append("rerun")
        return "done"

    calls = iter([call, call_fast])
    bg = asyncio.create_task(lp.background_curator_call(
        "proactive", lambda: next(calls)(), max_preempts=2))
    await started.wait()
    await lp.curator_start("chat", priority=lp.INTERACTIVE)
    chat_got_slot = True
    lp.curator_done()
    # the re-run waits out the 5 s back-off quiet window: shorten it
    lp._last_interactive = 0.0
    result = await asyncio.wait_for(bg, timeout=10)
    return runs, chat_got_slot, result


runs, chat_got, result = run(_preempt)
check("an interactive turn aborts the in-flight background request",
      runs[0] == "start" and "finished" not in runs, runs)
check("...gets the slot straight away", chat_got)
check("...and the background call re-runs afterwards and returns",
      runs[-1] == "rerun" and result == "done", (runs, result))


async def _not_preemptible_by_background():
    hit = []

    async def call():
        await asyncio.sleep(0.05)
        hit.append("ok")
        return 1

    bg = asyncio.create_task(lp.background_curator_call("a", call))
    await asyncio.sleep(0)
    await lp.curator_start("b")                 # BACKGROUND: waits, no abort
    lp.curator_done()
    return await bg, hit


res, hit = run(_not_preemptible_by_background)
check("a background arrival never preempts another background call", res == 1 and hit == ["ok"])


async def _preempt_budget():
    async def call():
        await asyncio.sleep(5)

    async def chatter(bg):
        while not bg.done():
            if lp.lane().preemptible:
                await lp.curator_start("chat", priority=lp.INTERACTIVE)
                lp.curator_done()
                lp._last_interactive = 0.0
            await asyncio.sleep(0.01)

    bg = asyncio.create_task(lp.background_curator_call("x", call, max_preempts=1))
    await chatter(bg)
    try:
        bg.result()
    except lp.Preempted:
        return True
    return False


check("past its preemption budget a background call raises Preempted", run(_preempt_budget))


# ── 3. the quiet window ─────────────────────────────────────────────────────
print("quiet window")


async def _quiet():
    lp.touch_interactive()
    t0 = asyncio.get_running_loop().time()
    await lp.wait_quiet(0.6)
    waited = asyncio.get_running_loop().time() - t0

    # an interactive HOLDER keeps background work waiting past the window
    await lp.curator_start("chat", priority=lp.INTERACTIVE)
    lp._last_interactive = 0.0
    w = asyncio.create_task(lp.wait_quiet(0.2))
    await asyncio.sleep(1.2)
    held = not w.done()
    lp.curator_done()            # ends the turn → the window restarts
    await asyncio.wait_for(w, timeout=3)
    return waited, held


waited, held = run(_quiet)
check("wait_quiet waits out the window after a sign of the user", waited >= 0.55, waited)
check("...and never returns while an interactive turn holds the slot", held)
check("curator_done() releases bare and still knows the turn was interactive",
      lp.lane().holder_prios == [] and lp.seconds_since_interactive() < 5)


async def _make_room():
    evicted = []

    async def fake_evict(model, url=None):
        evicted.append(model)

    orig = lp.evict_if_resident
    lp.evict_if_resident = fake_evict
    try:
        await lp.make_room_for_summarizer(0)          # nothing ran: no probe
        before = list(evicted)
        await lp.curator_start("chat", priority=lp.INTERACTIVE)
        lp.curator_done()
        lp._last_interactive = 0.0
        await lp.make_room_for_summarizer(0)
        return before, evicted, lp.lane().warm
    finally:
        lp.evict_if_resident = orig


before, evicted, warm = run(_make_room)
check("make_room skips the /api/ps probe when no curator ever ran", before == [])
check("...and evicts the idle curator before the 8B loads after a turn",
      evicted == [settings.CURATOR_MODEL or settings.BASE_CURATOR_MODEL], evicted)
check("...once", warm is False)


# ── 4. lexical pre-gates ────────────────────────────────────────────────────
print("pre-gates")
import src.services.episodic_memory as em      # noqa: E402

for msg, anchor, want in [
    ("thanks!", "Friends", False),
    ("lol that's fair", None, False),
    ("what year did it come out?", "Friends", False),
    ("I liked the first season", None, False),
    ("keep it", "Friends", True),
    ("we're keeping this show", None, True),
    ("don't delete Breaking Bad", None, True),
    ("nicht löschen bitte", None, True),
    ("das behalten wir", None, True),
    ("yes", "Friends", True),
    ("yes", None, False),
    ("it stays.", "Friends", True),
    # German verb-second order and emphatic forms (2026-10-21)
    ("Lösch den Film nicht!", None, True),
    ("auf keinen Fall löschen", None, True),
    ("Finger weg von Dark", None, True),
    ("bitte nicht entfernen", None, True),
    ("hands off that one", None, True),
    ("was läuft heute?", None, False),
]:
    check(f"protection pre-gate: {msg!r} (anchor={anchor!r}) -> {want}",
          em.protection_worth_checking(msg, anchor) is want)

for msg, want in [
    ("what else is on tonight?", False),
    ("ok thanks", False),
    ("I love the soundtrack because it's moody", True),
    ("honestly kind of boring after episode 3", True),
    ("das war langweilig", True),
    ("Der Film gefällt mir richtig", True),
    ("einfach genial", True),
    ("ein Meisterwerk", True),
    ("not really my thing", True),
    ("was läuft heute?", False),
]:
    check(f"memory pre-gate: {msg!r} -> {want}", bool(em._PREF_CUES.search(msg)) is want)

chat_src = _src("src/routers/chat.py")
post = chat_src[chat_src.index("def _schedule_post_turn("):chat_src.index("async def send_message(")]
check("the post-turn scheduler gates the protection task before creating it",
      post.index("protection_gate(") < post.index("_check_protection_intent_bg("))
check("the protection pre-gate is log-only for now (owner, 2026-10-21)",
      em.PROTECTION_PREGATE_ENFORCED is False
      and em.protection_gate("thanks!", "Friends", log=False) is True
      and em.protection_worth_checking("thanks!", "Friends") is False)


# ── 5. one runner, one keep_alive for every curator call ────────────────────
print("runner options")
import src.services.llm_utils as lu             # noqa: E402

p = lu.curator_payload([{"role": "user", "content": "x"}], temperature=0.0,
                       num_predict=10, fmt={"type": "object"}, seed=7)
check("curator_payload pins the shared window, residency and placement",
      p["options"]["num_ctx"] == lu.CURATOR_NUM_CTX and p["keep_alive"] == lu.CURATOR_KEEP_ALIVE
      and p["options"]["seed"] == 7 and p["format"] == {"type": "object"}
      and "num_thread" not in p["options"])
check("options_for keeps the summariser on its own bake window",
      lu.options_for(settings.SUMMARIZER_MODEL)["options"]["num_ctx"] == lu.SUMMARIZER_NUM_CTX
      and lu.options_for(settings.CURATOR_MODEL)["options"]["num_ctx"] == lu.CURATOR_NUM_CTX)

for rel in ("src/services/pillars.py", "src/services/curator_principles.py"):
    s = _src(rel)
    check(f"{rel}: no hand-built curator keep_alive left", '"keep_alive": "10m"' not in s)

cp = _src("src/services/curator_principles.py")
check("principle calls go through the gate, not past it",
      "background_curator_call(" in cp and "await wait_for_curator()" not in cp)
ps = _src("src/services/plex_sync.py")
check("the taste summary's curator attempts go through the gate",
      "background_curator_call(\"taste summary\"" in ps and "options_for(model" in ps)

# ── 6. no summariser on the chat pre-stream ─────────────────────────────────
print("chat pre-stream")
check("every chat verified-data lookup skips the summariser steps",
      chat_src.count("await _verified_for_chat(") == 4
      and "allow_summarizer=False" in chat_src)
ent = chat_src[chat_src.index("async def _extract_entity_llm("):
               chat_src.index("async def _detect_media_in_query(")]
check("entity extraction runs on the curator, never queueing behind the gate",
      "curator_payload(" in ent and "if curator_busy():" in ent
      and "SUMMARIZER" not in ent)
check("the chat turn takes the gate as INTERACTIVE",
      'curator_start("chat", priority=INTERACTIVE)' in chat_src)
check("the warm-up loads with the shared curator payload",
      "curator_payload([], temperature=0.7, num_predict=1, host=host)" in chat_src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
