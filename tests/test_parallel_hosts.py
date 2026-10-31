"""Both cards at once: one queue per Ollama host (2026-10-23).

On a split install the NAS's card runs enrichment while the gaming PC's runs
chat, background curator work and the deletion run. llm_priority keeps a
lane per host, so work waits only for work on the same card:

- a chat on the PC no longer pauses enrichment on the NAS;
- a deletion run on the PC and a curator job pinned to the NAS hold their
  slots at the same time;
- a caller queued for the PC while the PC starts a game moves to the NAS's
  queue instead of waiting for a card it will not get;
- the judge stays queued on the PC (deletion runs wait, never fall back).

Fakes only: no Ollama, no agent.

    python tests/test_parallel_hosts.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services import llm_priority as lp  # noqa: E402
from src.services import ollama_hosts as oh  # noqa: E402

from _checks import check  # noqa: E402


NAS = oh.OllamaHost(url="http://nas:30068", profile="small", num_ctx=16384)
PC = oh.OllamaHost(url="http://pc:11434", profile="large", num_ctx=16384)
pc_state = {"usable": True}
saved = (oh.main_host, oh.pc_host, oh.pc_usable, lp.evict_others, lp.REROUTE_CHECK_S)
oh.main_host = lambda: NAS
oh.pc_host = lambda: PC
oh.pc_usable = lambda: pc_state["usable"]


async def _no_evict(target, url=None):
    return None


lp.evict_others = _no_evict
lp.REROUTE_CHECK_S = 0.05


def run(coro_fn):
    async def wrapper():
        lp._reset_lanes()
        lp._last_interactive = 0.0
        pc_state["usable"] = True
        try:
            return await coro_fn()
        finally:
            for ln in lp.lanes():
                if ln.evict_task:
                    ln.evict_task.cancel()
    return asyncio.run(wrapper())


try:
    # ── routing of the lanes ────────────────────────────────────────────────
    async def routing():
        out = {r: lp.lane_for(r).url for r in ("chat", "curator", "judge", "enrich", "embed")}
        pc_state["usable"] = False
        out["off"] = {r: lp.lane_for(r).url for r in ("chat", "judge", "enrich")}
        return out
    r = run(routing)
    check("PC free: chat, curator and the judge share the PC's lane",
          r["chat"] == r["curator"] == r["judge"] == PC.url)
    check("...enrichment and embeddings stay on the NAS's lane",
          r["enrich"] == r["embed"] == NAS.url)
    check("PC gone: chat moves to the NAS's lane, the judge stays queued on the PC's",
          r["off"]["chat"] == NAS.url and r["off"]["judge"] == PC.url)

    # ── a chat on the PC does not pause enrichment on the NAS ───────────────
    async def chat_vs_enrich():
        ln = await lp.curator_start("chat", priority=lp.INTERACTIVE)
        t0 = asyncio.get_running_loop().time()
        await lp.wait_for_curator(timeout=1.0)
        await lp.wait_for_enrichment_slot(timeout=1.0)
        waited = asyncio.get_running_loop().time() - t0
        nas_open = lp.lane(NAS.url).event().is_set()
        lp.curator_done(lane=ln)
        return ln.url, waited, nas_open
    url, waited, nas_open = run(chat_vs_enrich)
    check("a chat holds the PC's slot", url == PC.url)
    check("...and enrichment on the NAS does not wait for it", waited < 0.2 and nas_open)

    # ── a chat on the NAS (PC gone) does pause enrichment there, as before ──
    async def nas_chat():
        pc_state["usable"] = False
        ln = await lp.curator_start("chat", priority=lp.INTERACTIVE)
        waiter = asyncio.create_task(lp.wait_for_curator(timeout=5.0))
        await asyncio.sleep(0.1)
        blocked = not waiter.done()
        lp.curator_done(lane=ln)
        await asyncio.wait_for(waiter, 1.0)
        return ln.url, blocked
    url, blocked = run(nas_chat)
    check("same card: enrichment yields to the chat until it is done",
          url == NAS.url and blocked)

    # ── two holders on two cards at once ────────────────────────────────────
    async def two_cards():
        judge = await lp.curator_start("deletion scan: movies", role="judge")
        other = await asyncio.wait_for(
            lp.curator_start("taste summary", url=NAS.url), timeout=0.5)
        both = judge.active == 1 and other.active == 1
        lp.curator_done(lane=other)
        lp.curator_done(lane=judge)
        return judge.url, other.url, both
    j, o, both = run(two_cards)
    check("a deletion run on the PC and a curator job on the NAS hold slots at once",
          j == PC.url and o == NAS.url and both)

    # ── same card: still one at a time ──────────────────────────────────────
    async def same_card():
        first = await lp.curator_start("deletion scan", role="judge")
        second = asyncio.create_task(lp.curator_start("chat", priority=lp.INTERACTIVE))
        await asyncio.sleep(0.15)
        queued = not second.done() and lp.gate_contested(first) and lp.interactive_waiting(first)
        lp.curator_done(lane=first)
        ln = await asyncio.wait_for(second, 1.0)
        lp.curator_done(lane=ln)
        return queued
    check("the same card still serves one generation at a time; the run sees the chat queued",
          run(same_card))

    # ── re-routing while queued ─────────────────────────────────────────────
    async def reroute():
        holder = await lp.curator_start("deletion scan", role="judge")      # PC busy
        waiter = asyncio.create_task(lp.curator_start("proactive message"))  # queued on the PC
        await asyncio.sleep(0.1)
        queued_on_pc = not waiter.done() and lp.lane(PC.url).waiters == 1
        pc_state["usable"] = False                                           # a game starts
        ln = await asyncio.wait_for(waiter, 1.0)
        left = lp.lane(PC.url).waiters == 0 and lp.lane(PC.url).gate.waiting() == 0
        lp.curator_done(lane=ln)
        lp.curator_done(lane=holder)
        free = not lp.lane(PC.url).gate.locked() and not lp.lane(NAS.url).gate.locked()
        return queued_on_pc, ln.url, left, free
    queued_on_pc, url, left, free = run(reroute)
    check("queued for the PC while it is taken", queued_on_pc)
    check("the PC starts a game: the waiter moves to the NAS and runs there", url == NAS.url)
    check("...its place in the PC's queue is gone, and no slot leaks", left and free)

    async def judge_stays():
        holder = await lp.curator_start("chat", priority=lp.INTERACTIVE)    # PC taken
        waiter = asyncio.create_task(lp.curator_start("deletion scan", role="judge"))
        await asyncio.sleep(0.1)
        pc_state["usable"] = False
        await asyncio.sleep(0.2)
        still = not waiter.done() and lp.lane(PC.url).waiters == 1
        lp.curator_done(lane=holder)
        ln = await asyncio.wait_for(waiter, 1.0)
        lp.curator_done(lane=ln)
        return still, ln.url
    still, url = run(judge_stays)
    check("the judge never falls back: it stays queued on the PC", still and url == PC.url)

    # ── a slot handed over in the same tick the route changes ───────────────
    async def handed_then_moved():
        holder = await lp.curator_start("chat", priority=lp.INTERACTIVE)
        waiter = asyncio.create_task(lp.curator_start("starters"))
        await asyncio.sleep(0.1)
        pc_state["usable"] = False
        lp.curator_done(lane=holder)        # hands the PC's slot to the waiter...
        ln = await asyncio.wait_for(waiter, 1.0)
        pc_free = not lp.lane(PC.url).gate.locked()
        lp.curator_done(lane=ln)
        return ln.url, pc_free
    url, pc_free = run(handed_then_moved)
    check("...which notices the PC is gone, gives the slot back and runs on the NAS",
          url == NAS.url and pc_free)

    # ── preemption stays on its own card ────────────────────────────────────
    async def preempt_card():
        started = asyncio.Event()

        async def long_call():
            started.set()
            await asyncio.sleep(1.0)
            return "done"
        bg = asyncio.create_task(lp.background_curator_call("taste summary", long_call))
        await started.wait()
        pc_state["usable"] = False          # the chat goes to the NAS
        ln = await lp.curator_start("chat", priority=lp.INTERACTIVE)
        await asyncio.sleep(0.05)
        untouched = not bg.done()
        lp.curator_done(lane=ln)
        bg.cancel()
        try:
            await bg
        except asyncio.CancelledError:
            pass
        return ln.url, untouched
    url, untouched = run(preempt_card)
    check("a chat on the NAS does not preempt background work on the PC's card",
          url == NAS.url and untouched)

    # ── not split: one lane, as before ──────────────────────────────────────
    async def single():
        oh.pc_host = lambda: None
        try:
            urls = {lp.lane_for(r).url for r in ("chat", "curator", "judge", "enrich")}
            ln = await lp.curator_start("chat", priority=lp.INTERACTIVE)
            blocked = not lp.lane_for("enrich").event().is_set()
            lp.curator_done()               # bare: the only held lane
            return urls, blocked, ln.active
        finally:
            oh.pc_host = lambda: PC
    urls, blocked, active = run(single)
    check("not split: every role shares the one lane, a chat pauses enrichment, "
          "a bare curator_done() releases it", urls == {NAS.url} and blocked and active == 0)
finally:
    oh.main_host, oh.pc_host, oh.pc_usable, lp.evict_others, lp.REROUTE_CHECK_S = saved

# ── wiring ──────────────────────────────────────────────────────────────────
eng = (ROOT / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
pil = (ROOT / "src/services/pillars.py").read_text(encoding="utf-8")
chat = (ROOT / "src/routers/chat.py").read_text(encoding="utf-8")
mu = (ROOT / "src/services/model_updates.py").read_text(encoding="utf-8")
check("the deletion run and its pillar calls queue as the judge",
      eng.count('role="judge"') == 4 and pil.count('role="judge"') == 2
      and "curator_done()\n" not in eng)
check("the chat turn releases the lane it holds", "curator_done(lane=_chat_lane)" in chat
      and "check_curator_vram_health(curator_model, url=_chat_lane.url)" in chat)
check("model updates queue on the host they measure", mu.count("url=endpoint") >= 7)

check.done()
