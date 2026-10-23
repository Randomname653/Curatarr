"""
LLM Priority — Curator model gets priority over enrichment workers.

The curator (large model) is interactive; enrichment (small model) runs in background.
When a curator call starts:
  1. The enrichment event is cleared  → workers finish their current item then pause
  2. The summarizer model is evicted from VRAM → frees space for the curator
  3. Curator runs; enrichment waits

When the curator finishes the event is set → enrichment resumes and Ollama reloads the
summarizer on-demand for the next item.

Uses a counter + asyncio.Event so concurrent curator calls (chat + deletion pitch, etc.)
are handled correctly — enrichment only resumes once ALL curator calls are done.

Lifecycle invariants
--------------------
* In single-threaded asyncio the ``active += 1`` / ``== 1`` check is atomic
  (no ``await`` between them), so two concurrent ``curator_start`` calls
  cannot both enter the first-call branch.
* The event is cleared BEFORE awaiting model eviction. The previous
  ordering left a ~15 s window where ``_event.is_set()`` was still True
  while a curator was already on its way in — enrichment could start an
  LLM call against the about-to-be-evicted model.
* ``curator_done`` is symmetric and safe to call more than ``curator_start``
  (clamped at 0). Use the ``curator_priority()`` async context manager so
  ``curator_done`` runs even if the body raises.
"""

import asyncio
import heapq
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from src.services.http_client import tls_context
from src.services.pc_guard import ollama_client
from src.services.ollama_hosts import ollama_for

logger = logging.getLogger(__name__)

# Pass 60: priority-enrichment tier. A user-triggered single-item re-enrich
# (Library section → "Re-fetch metadata") sits BETWEEN the curator and the
# bulk enrichment run:
#   curator (chat)        → pauses everything, evicts the summarizer
#   priority enrichment   → pauses the bulk run, KEEPS the summarizer (it
#                           needs it itself) — this tier
#   bulk enrichment       → yields to both of the above
# Before this, bulk and single-item re-enrich were both just "enrichment"
# with no precedence — an explicitly-requested re-enrich queued behind an
# already-running bulk job instead of jumping ahead.
_pe_event: asyncio.Event | None = None
_pe_active = 0


def _get_pe_event() -> asyncio.Event:
    """Lazy accessor for the priority-enrichment gate (Pass 60)."""
    global _pe_event
    if _pe_event is None:
        _pe_event = asyncio.Event()
        _pe_event.set()  # set = no priority re-enrich running → bulk may proceed
    return _pe_event


# ── CURATOR CONCURRENCY GATE ──────────────────────────────────────────────────
# A single GPU can only generate one big-model (curator) response at a time
# without spilling to CPU and dragging EVERY response to a crawl. The
# priority events above make enrichment yield to the curator, but they do NOT
# stop two curator calls from overlapping — two users chatting at once, or a
# chat colliding with a recs / proactive / verification generation. This
# semaphore serializes all curator-tier calls: the second caller queues until
# the slot frees. ``MAX_CONCURRENT_CURATOR`` (default 1) can be raised if the
# host ever gets enough VRAM to run two big generations side by side.
#
# Waiters are served by PRIORITY, then arrival. A plain FIFO semaphore served
# a chat turn after every recs / proactive / starters job that happened to
# queue first — opening the chat view queued a 900-token starters refill
# right ahead of the user's first message.
INTERACTIVE = 0     # a user is watching this one (chat stream, warm-up)
BACKGROUND = 1      # everything else
# How often a queued caller on a split install re-checks where its work is
# routed (the PC's agent reports every 5 s).
REROUTE_CHECK_S = 5.0


class _PriorityGate:
    """Semaphore whose waiters are woken by (priority, arrival) instead of
    arrival alone. A freed slot is handed straight to the chosen waiter, so a
    caller arriving in between cannot jump the queue."""

    def __init__(self, slots: int):
        self._free = slots
        self._heap: list = []           # (priority, seq, future)
        self._seq = 0

    def locked(self) -> bool:
        return self._free <= 0

    def waiting(self, priority: int | None = None) -> int:
        return sum(1 for p, _, f in self._heap
                   if not f.done() and (priority is None or p == priority))

    async def acquire(self, priority: int = BACKGROUND, still_wanted=None,
                      every: float | None = None) -> bool:
        """Take a slot. ``still_wanted`` (a split install) is asked every
        ``every`` seconds while queued: False gives up the place in the queue
        and returns False - the work was routed to another host meanwhile."""
        if self._free > 0 and not self.waiting():
            self._free -= 1
            return True
        fut = asyncio.get_running_loop().create_future()
        self._seq += 1
        heapq.heappush(self._heap, (priority, self._seq, fut))
        try:
            if still_wanted is None:
                await fut
                return True
            while True:
                await asyncio.wait({fut}, timeout=every or REROUTE_CHECK_S)
                if fut.done():
                    return True
                if not still_wanted():
                    fut.cancel()        # release() skips it
                    return False
        except asyncio.CancelledError:
            # Handed the slot in the same tick we were cancelled: pass it on
            # instead of leaking it to a waiter that is gone.
            if fut.done() and not fut.cancelled():
                self.release()
            else:
                fut.cancel()            # asyncio.wait does not cancel it for us
            raise

    def release(self) -> None:
        while self._heap:
            _, _, fut = heapq.heappop(self._heap)
            if not fut.done():
                fut.set_result(None)    # direct hand-over; _free unchanged
                return
        self._free += 1


# ── LANES: one queue per Ollama host (2026-10-23) ─────────────────────────────
# A split install has two cards, the NAS's and the gaming PC's. All of the
# state below used to be one global, so a chat on the PC's card paused
# enrichment on the NAS's and a deletion pitch on the PC queued behind a taste
# summary on the NAS. Now every host has its own lane: the curator slot, the
# "curator running" event enrichment waits on, the idle evictor, preemption.
# Work queues only behind work on the same card. An install that is not split
# has exactly one lane, and everything behaves as it did.
#
# A caller says what kind of work it is (``role``, see ollama_hosts.ROLES);
# the lane is the host that role routes to NOW. While a split install's
# caller waits in a queue, the route is re-checked every few seconds: a PC
# that starts a game hands its waiters over to the NAS's queue instead of
# keeping them waiting on a card they will not get.

class _Lane:
    """One Ollama host's queue and VRAM bookkeeping."""

    def __init__(self, url: str):
        from src.config import settings
        n = max(1, int(getattr(settings, "MAX_CONCURRENT_CURATOR", 1) or 1))
        self.url = url
        self.gate = _PriorityGate(n)
        self._event: asyncio.Event | None = None
        self.active = 0
        # Who holds the slot (debug label, e.g. "deletion scan: anime") and
        # how many callers are queued behind it. The waiter count lets a
        # long-running batch (the judge funnel) YIELD between items when
        # someone is waiting — an interactive chat gets the GPU within one
        # verdict instead of after the whole category.
        self.owner = ""
        self.waiters = 0
        # Set when a holder ran (a big model is plausibly resident), cleared
        # once make_room_for_summarizer or the idle evictor cleared the card.
        self.warm = False
        # Priorities of the current holders, in acquire order: curator_done()
        # tells from it whether an interactive turn just ended.
        self.holder_prios: list = []
        # In-flight background requests an interactive turn on THIS card may
        # abort (request Task -> owner label).
        self.preemptible: dict = {}
        # The delayed curator eviction, rescheduled on each activity change.
        self.evict_task: asyncio.Task | None = None
        # Summariser calls blocked in wait_for_curator(): the idle evictor
        # picks its short delay while the pipeline starves for the card.
        self.summarizer_pending = 0

    def event(self) -> asyncio.Event:
        """Set = no curator running here → enrichment may proceed. Lazy so it
        binds to whichever loop first uses it."""
        if self._event is None:
            self._event = asyncio.Event()
            self._event.set()
        return self._event


_lanes: dict = {}


def _key(url: str) -> str:
    return (url or "").strip().rstrip("/")


def lane(url: str | None = None) -> _Lane:
    """The lane of the host at ``url``; this install's own Ollama by default."""
    if url is None:
        from src.services.ollama_hosts import main_host
        url = main_host().url
    k = _key(url)
    ln = _lanes.get(k)
    if ln is None:
        ln = _lanes[k] = _Lane(k)
    return ln


def lane_for(role: str) -> _Lane:
    """The lane ``role`` runs on now. A PC-only role while the PC cannot take
    work answers the PC's lane: the call itself raises PcUnavailable there,
    the queue does not decide that."""
    from src.services.ollama_hosts import PcUnavailable, host_for, pc_host
    try:
        return lane(host_for(role).url)
    except PcUnavailable:
        return lane(pc_host().url)


def lanes() -> list:
    return list(_lanes.values())


def _reset_lanes() -> None:
    """Tests: forget every lane (a fresh event loop needs fresh futures)."""
    _lanes.clear()
    _preempted.clear()


def _default_role(priority: int) -> str:
    return "chat" if priority == INTERACTIVE else "curator"


def _held_lane(ln: "_Lane | None") -> _Lane:
    """The lane a bare ``curator_done()`` releases: the one given, else the
    only lane with a holder (an install that is not split has one lane)."""
    if ln is not None:
        return ln
    held = [x for x in _lanes.values() if x.active > 0]
    if len(held) == 1:
        return held[0]
    if held:
        logger.warning("[gate] curator_done() without a lane while %d hosts hold one — "
                       "releasing %s", len(held), held[-1].url)
        return held[-1]
    return lane()


def curator_busy(role: str = "chat") -> bool:
    """True when every curator slot on ``role``'s host is taken — the next
    ``curator_start`` there will block. The chat stream uses it to tell a
    waiting user they're queued instead of leaving a silent dead window."""
    return lane_for(role).gate.locked()


def gate_owner(role: str = "chat") -> str:
    """Label of the holder on ``role``'s host ('' when free) — for busy messages."""
    ln = lane_for(role)
    return ln.owner if ln.gate.locked() else ""


def gate_contested(ln: "_Lane | None" = None) -> bool:
    """True when a caller is queued behind the holder of ``ln`` (any host
    when not given)."""
    if ln is not None:
        return ln.waiters > 0
    return any(x.waiters > 0 for x in _lanes.values())


def interactive_waiting(ln: "_Lane | None" = None) -> bool:
    """True when an interactive turn is queued on ``ln`` (any host when not given)."""
    return any(x.gate.waiting(INTERACTIVE) > 0
               for x in ([ln] if ln is not None else _lanes.values()))


def interactive_active(ln: "_Lane | None" = None) -> bool:
    """True while an interactive turn holds a curator slot on ``ln`` (any host)."""
    return any(INTERACTIVE in x.holder_prios
               for x in ([ln] if ln is not None else _lanes.values()))


# ── INTERACTIVITY CLOCK ───────────────────────────────────────────────────────
# Monotonic time of the last sign of a user: a chat turn starting or ending,
# a typing / focus ping (POST /api/chat/warm). Background LLM work that would
# load a model or hold the GPU waits until this is a quiet window old — the
# post-turn jobs used to fire the instant a reply ended and cost the user's
# very next message a 20 GB reload.
_last_interactive = 0.0


def touch_interactive() -> None:
    global _last_interactive
    _last_interactive = time.monotonic()


def seconds_since_interactive() -> float:
    return time.monotonic() - _last_interactive


async def wait_quiet(quiet_s: float = 45.0, max_wait: float = 900.0) -> None:
    """Return once the user has been idle for ``quiet_s`` seconds with no
    interactive turn held or queued. Gives up after ``max_wait`` so a user
    chatting for an hour cannot starve background learning for ever (the
    work then still queues BEHIND interactive turns on the priority gate)."""
    if quiet_s <= 0:
        return
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        idle = seconds_since_interactive()
        if idle >= quiet_s and not interactive_active() and not interactive_waiting():
            return
        step = quiet_s - idle if idle < quiet_s else 1.0
        await asyncio.sleep(max(0.5, min(step, deadline - time.monotonic())))
    logger.info("[gate] quiet window of %.0fs never came within %.0fs — proceeding",
                quiet_s, max_wait)


# ── PREEMPTION ────────────────────────────────────────────────────────────────
# In-flight background curator requests that an interactive turn may abort:
# cancelling the request task closes its HTTP connection, which makes Ollama
# stop decoding. background_curator_call() then re-queues it.
_preempted: set = set()


def _preempt_background(ln: "_Lane") -> None:
    for t, owner in list(ln.preemptible.items()):
        if not t.done():
            _preempted.add(t)
            t.cancel()
            logger.info("[gate] preempting %r for an interactive turn", owner)


class Preempted(Exception):
    """A background curator call was preempted more often than allowed."""


# ── MODEL INTROSPECTION ───────────────────────────────────────────────────────

_ps_failing = False   # warn once per /api/ps outage, not every 0.3 s poll


async def loaded_models(url: str | None = None) -> Optional[list[dict]]:
    """
    Return the list of models currently resident in Ollama VRAM - on the
    host at ``url``, by default the one the next curator call runs on.

    Each entry from Ollama /api/ps looks like::

        {"name": "qwen3.6:27b", "size_vram": 22123456789, "size": 22123456789, ...}

    Returns [] when nothing is loaded and None when /api/ps could not be
    read. Errors used to return [] too ("nothing resident"), so
    _evict_model logged "Confirmed evicted" for a model it never saw leave
    and evict_others evicted nothing — reopening the VRAM race.
    """
    global _ps_failing
    err = None
    try:
        async with httpx.AsyncClient(verify=tls_context(), timeout=5) as client:
            # Default: the ACTIVE host - the PC while it takes work, else
            # this install's Ollama - where the next curator call will run.
            r = await client.get(f"{url or ollama_for('curator')}/api/ps")
        if r.status_code == 200:
            _ps_failing = False
            return r.json().get("models", [])
        err = f"HTTP {r.status_code}"
    except Exception as e:
        err = str(e) or type(e).__name__
    if not _ps_failing:
        _ps_failing = True
        logger.warning("Ollama /api/ps unreadable (%s) — VRAM residency unknown", err)
    else:
        logger.debug("Ollama /api/ps still unreadable: %s", err)
    return None


async def _evict_model(model_name: str, timeout: float = 8.0, url: str | None = None) -> bool:
    """
    Ask Ollama to unload *model_name* from VRAM AND wait until /api/ps
    confirms the model is actually gone.

    Background: ``keep_alive=0`` is Ollama's eviction signal but it does NOT
    block — it just tells Ollama "you may unload now". Ollama then frees the
    VRAM at its own pace (typically 1-3 s for a 14 GB summarizer). The
    previous fire-and-forget version returned before the VRAM was actually
    free, opening a race where the curator load fight the still-resident
    summarizer for VRAM and Ollama silently fell back to 100% CPU for
    *both* models.

    Now we poll /api/ps until the model is no longer listed (or until
    ``timeout`` seconds have elapsed). Returns True if eviction confirmed,
    False if we timed out (callers may proceed anyway — better to risk a
    CPU-fallback warning than to block the user forever).
    """
    url = url or ollama_for('curator')
    try:
        async with httpx.AsyncClient(verify=tls_context(), timeout=15) as client:
            await client.post(
                f"{url}/api/generate",
                json={"model": model_name, "keep_alive": 0},
            )
    except httpx.HTTPError as e:
        logger.debug("Eviction request failed for %s: %s", model_name, e)
        return False
    except Exception as e:
        logger.warning("Eviction request crashed for %s: %r", model_name, e,
                       exc_info=True)
        return False

    # Poll /api/ps until the model is gone. Match by base name (without
    # the ":tag" suffix) because Ollama may report "x:latest" or "x".
    base_name = model_name.split(":")[0]
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        loaded = await loaded_models(url)
        # None = /api/ps unreadable: unknown is not "gone" — keep polling.
        if loaded is not None and not any(
                base_name in m.get("name", "") for m in loaded):
            logger.info("Confirmed evicted from VRAM: %s", model_name)
            return True
        await asyncio.sleep(0.3)
    logger.warning(
        "Eviction of %s NOT confirmed after %.0fs — VRAM may still be busy; "
        "curator may load partially on CPU",
        model_name, timeout,
    )
    return False


async def check_curator_vram_health(curator_model: str, url: str | None = None) -> dict:
    """
    Inspect /api/ps and report whether the curator is fully on GPU or has
    spilled to CPU. Threshold semantics:

    - cpu_pct <= 13:  ok          (small spillover for context buffers is normal)
    - cpu_pct < 50:   moderate    (response will be slower, but tolerable)
    - cpu_pct >= 50:  severe      (response will be 5-10× slower; suggest restart)

    If the model isn't listed at all, returns severity="not_loaded" — that's
    fine for early polling before Ollama has loaded it; just retry shortly.

    Returns a dict shape consumed by the chat router to decide whether to
    emit an SSE warning frame to the frontend.
    """
    loaded = await loaded_models(url) or []   # unreadable → reported as not_loaded
    base_name = (curator_model or "").split(":")[0]
    if not base_name:
        return {"loaded": False, "ratio": 0.0, "cpu_pct": 0,
                "severity": "not_loaded", "message": None}

    for m in loaded:
        if base_name in m.get("name", ""):
            total = m.get("size", 0) or 1
            vram = m.get("size_vram", 0) or 0
            ratio = vram / total if total else 0.0
            cpu_pct = max(0, min(100, round((1 - ratio) * 100)))

            if cpu_pct <= 13:
                return {"loaded": True, "ratio": ratio, "cpu_pct": cpu_pct,
                        "severity": "ok", "message": None}
            if cpu_pct < 50:
                return {
                    "loaded": True, "ratio": ratio, "cpu_pct": cpu_pct,
                    "severity": "moderate",
                    "message": (
                        f"⚠️ {cpu_pct}% of the curator model is on CPU — "
                        f"responses will be slower than usual."
                    ),
                }
            return {
                "loaded": True, "ratio": ratio, "cpu_pct": cpu_pct,
                "severity": "severe",
                "message": (
                    f"⚠️ {cpu_pct}% of the curator model is on CPU. "
                    f"Responses will be much slower. Tip: restart Ollama "
                    f"or reduce the context window."
                ),
            }

    return {"loaded": False, "ratio": 0.0, "cpu_pct": 0,
            "severity": "not_loaded", "message": None}


# ── CURATOR LIFECYCLE ─────────────────────────────────────────────────────────

async def curator_start(owner: str = "curator request", *,
                        exclusive_model: str = None,
                        priority: int = BACKGROUND,
                        role: str | None = None,
                        url: str | None = None) -> "_Lane":
    """
    Signal that a curator (chat / recommendations) call is starting.
    ``owner`` is a debug label shown to queued callers ("deletion scan: anime").

    ``exclusive_model`` names the big model this holder is about to run —
    default is the curator bake. On the *first* active call every OTHER
    known big model that is actually resident gets evicted (two-bake
    split: a deletion run passes the pitcher here, so the resident
    curator is evicted before the pitcher loads — and vice versa when a
    chat interjects mid-run). The gate itself stays model-agnostic: a
    slot is a slot, whichever bake the holder runs.

    Concurrent calls (e.g. two chat streams open simultaneously) increment
    the counter but only trigger eviction once.

    The order matters: we clear the event BEFORE awaiting model eviction.
    Awaiting first would leave a window where wait_for_curator() returns
    immediately because ``_event.is_set()`` is still True, and an
    enrichment worker could fire an LLM call against the model that's
    about to be evicted out from under it.

    ``priority`` orders the queue: INTERACTIVE (a user is watching) is served
    before every BACKGROUND waiter, and an interactive arrival preempts any
    in-flight ``background_curator_call`` holding the slot.

    ``role`` (ollama_hosts.ROLES) picks the host's lane: "chat" for an
    interactive turn, "curator" for background work by default, "judge" for
    the deletion run. ``url`` pins one host instead (model updates measure
    a named host). Returns the lane held - pass it to ``curator_done``.
    """
    role = role or _default_role(priority)
    if priority == INTERACTIVE:
        touch_interactive()
    # Serialize big-model generations per card: block here until a curator
    # slot on this host is free (MAX_CONCURRENT_CURATOR, default 1). A second
    # chatter / recs / proactive call waits instead of fighting for the GPU.
    # Acquired BEFORE touching ``active`` so a cancelled acquire (user
    # disconnects while queued) leaves no state to unwind, and so the gate is
    # released exactly once per successful acquire in curator_done().
    ln = await _acquire_lane(role, priority, url)
    ln.owner = owner
    ln.warm = True
    ln.holder_prios.append(priority)
    ln.active += 1
    is_first = (ln.active == 1)

    # From here on we hold a slot. The eviction below awaits network calls,
    # and a cancel landing there (client disconnect during the chat's
    # pre-stream, a scan being stopped) propagates out of curator_start
    # BEFORE the caller has entered the try whose finally calls
    # curator_done() — the slot would leak and every later curator call
    # would queue forever behind a dead holder. Undo our own acquire on any
    # failure so callers only ever own a slot once curator_start returns.
    try:
        # Pass 14.7: cancel any pending idle-eviction. The user is back; we
        # want the curator to stay in VRAM, not get evicted mid-conversation.
        if ln.evict_task and not ln.evict_task.done():
            ln.evict_task.cancel()
            logger.debug("Cancelled pending curator eviction (new chat activity)")

        if is_first:
            # Clear the gate immediately — before any await — so any
            # enrichment worker hitting wait_for_curator() right now blocks.
            ln.event().clear()
        logger.debug("curator_start  active=%d on %s", ln.active, ln.url)

        if is_first:
            from src.config import settings
            target = (exclusive_model or settings.CURATOR_MODEL
                      or settings.BASE_CURATOR_MODEL)
            await evict_others(target, url=ln.url)
    except BaseException:
        curator_done(priority=priority, lane=ln)
        raise
    return ln


async def _acquire_lane(role: str, priority: int, url: str | None = None) -> "_Lane":
    """A slot on the lane ``role`` routes to. On a split install the route is
    re-checked while queued and once more after the slot is handed over: work
    routed elsewhere meanwhile (the PC started a game, or came back) leaves
    this queue for the right one."""
    from src.services.ollama_hosts import split
    while True:
        ln = lane(url) if url else lane_for(role)
        if priority == INTERACTIVE and ln.gate.locked():
            _preempt_background(ln)
        still = (lambda: lane_for(role) is ln) if split() and not url else None
        ln.waiters += 1
        try:
            got = await ln.gate.acquire(priority, still_wanted=still)
        finally:
            ln.waiters -= 1
        if got and (still is None or still()):
            return ln
        if got:
            ln.gate.release()
        logger.info("[gate] %s work re-routed from %s while queued", role, ln.url)


async def evict_others(target_model: str, url: str | None = None) -> None:
    """Evict every known big model EXCEPT *target_model* — but only the
    ones actually resident in VRAM.

    The residency guard is load-bearing, not an optimization: POSTing
    ``keep_alive: 0`` at a model that is NOT loaded makes Ollama
    load-then-unload it (documented in process_monitor) — the "cleanup"
    of an absent 17 GB pitcher would cost a 17 GB load. One /api/ps
    probe covers all candidates.
    """
    from src.config import settings
    candidates = {
        settings.CURATOR_MODEL or settings.BASE_CURATOR_MODEL,
        settings.SUMMARIZER_MODEL or settings.BASE_SUMMARIZER_MODEL,
        (settings.PITCHER_MODEL or "").strip(),
    }
    candidates.discard("")
    candidates.discard(None)
    candidates.discard(target_model)
    if not candidates:
        return
    loaded = await loaded_models(url)
    if loaded is None:
        # Residency unknown: evicting blind would LOAD absent models (see
        # above), so evict nothing — but say so instead of "none resident".
        logger.warning("Gate holder needs %s — VRAM residency unknown "
                       "(/api/ps unreadable); nothing evicted", target_model)
        return
    evicted = False
    for model in candidates:
        base_name = model.split(":")[0]
        if any(base_name in m.get("name", "") for m in loaded):
            logger.info("Gate holder needs %s — evicting resident %s",
                        target_model, model)
            await _evict_model(model, url=url)
            evicted = True
    if not evicted:
        logger.info("Gate holder needs %s — no other big model resident",
                    target_model)


async def evict_if_resident(model_name: str, url: str | None = None) -> None:
    """Guarded single-model evict (run-end cleanup). No-ops when the
    model is not in VRAM — see evict_others for why the guard matters."""
    if not model_name:
        return
    base_name = model_name.split(":")[0]
    loaded = await loaded_models(url)
    if loaded is None:
        logger.warning("evict %s skipped — VRAM residency unknown", model_name)
        return
    if any(base_name in m.get("name", "") for m in loaded):
        await _evict_model(model_name, url=url)


async def resolve_pitcher_model(url: str = None) -> str:
    """The model a deletion run should use: PITCHER_MODEL when configured
    AND installed - on ``url``, the host the run judges on - else the
    curator bake.

    The install probe (one /api/tags GET per run) is mandatory, not
    defensive polish: pillars.adjudicate swallows ALL exceptions into a
    safe EVALUATE fallback, so a misspelled pitcher name would silently
    produce zero proposals per category with only debug noise. A visible
    fallback to the known-good curator is strictly better.
    """
    from src.config import settings
    pitcher = (settings.PITCHER_MODEL or "").strip()
    fallback = settings.CURATOR_MODEL or settings.BASE_CURATOR_MODEL
    if not pitcher:
        return fallback
    try:
        async with ollama_client(timeout=5) as client:
            r = await client.get(f"{url or ollama_for('judge')}/api/tags")
        if r.status_code == 200:
            names = [m.get("name", "") or m.get("model", "")
                     for m in r.json().get("models", [])]
            for n in names:
                if pitcher == n or pitcher in n or n in pitcher:
                    return pitcher
    except httpx.HTTPError as e:
        logger.debug("pitcher install probe failed: %s", e)
    except Exception as e:
        logger.warning("pitcher install probe crashed: %r", e, exc_info=True)
    logger.warning("PITCHER_MODEL=%s not installed — deletion run falls "
                   "back to %s (run `python build_models.py`)",
                   pitcher, fallback)
    return fallback


async def shutdown_evict() -> None:
    """Called from the app's lifespan teardown.

    The idle-evict timer is cancelled by new chat activity but nothing ever
    cancelled it on shutdown — stop the app inside its 10-60s window and the
    loop tears down under a sleeping task ("Task was destroyed but it is
    pending!"), and the eviction it was going to do never happens, so the
    curator squats in VRAM until Ollama's own keep_alive expires. Closing
    the app is the strongest "user is done" signal there is: cancel the
    timer and evict immediately, best-effort.
    """
    from src.config import settings
    # Every card this process ran a curator on, and the one the next curator
    # call would use (as before the lanes: a model left from an earlier run).
    urls = [_key(ollama_for('curator'))]
    for ln in lanes():
        if ln.evict_task and not ln.evict_task.done():
            ln.evict_task.cancel()
        ln.evict_task = None
        if ln.warm and ln.url not in urls:
            urls.append(ln.url)
    for url in urls:
        for model in (settings.CURATOR_MODEL or settings.BASE_CURATOR_MODEL,
                      (settings.PITCHER_MODEL or "").strip()):
            if not model:
                continue
            try:
                await asyncio.wait_for(_evict_model(model, url=url), timeout=5)
            except Exception as e:  # noqa: BLE001 - shutdown teardown; Ollama unloads on its own TTL
                logger.debug("[shutdown] evict of %s on %s skipped: %s", model, url, e)


async def _scheduled_curator_evict(ln: "_Lane") -> None:
    """Background task: wait for an idle window, then evict the curator.

    Cancelled by ``curator_start`` if the user comes back within the window.
    The delay is chosen at firing time:

      - ``CURATOR_IDLE_EVICT_BUSY`` (10 s) when at least one summarizer call
        is queued (the background pipeline needs the curator OUT to make
        progress).
      - ``CURATOR_IDLE_EVICT_SECONDS`` (60 s) otherwise (give the user a
        comfortable window to type a follow-up before paying the reload tax).
    """
    from src.services.llm_utils import CURATOR_IDLE_EVICT_SECONDS, CURATOR_IDLE_EVICT_BUSY

    delay = CURATOR_IDLE_EVICT_BUSY if ln.summarizer_pending > 0 else CURATOR_IDLE_EVICT_SECONDS
    if ln.summarizer_pending > 0:
        logger.info(
            "Curator idle: %d summarizer call(s) waiting — eviction in %ds",
            ln.summarizer_pending, delay,
        )
    else:
        logger.debug("Curator idle: scheduling eviction in %ds", delay)

    try:
        await asyncio.sleep(delay)
        # Past the window, evict only when something needs the card (a
        # summariser call queued on wait_for_curator) or once the user has
        # been away CURATOR_IDLE_KEEP_S. Background summariser work that
        # starts with the gate already open makes its own room
        # (make_room_for_summarizer) — so a reader who takes 90 s over a
        # long reply no longer pays a 20 GB reload for their follow-up.
        from src.services.llm_utils import CURATOR_IDLE_KEEP_S
        while (ln.summarizer_pending == 0
               and seconds_since_interactive() < CURATOR_IDLE_KEEP_S):
            await asyncio.sleep(15)
    except asyncio.CancelledError:
        logger.debug("Curator eviction cancelled — new chat activity")
        raise

    # Idle window elapsed. If the curator is still in VRAM, evict it.
    # The pitcher is included as leak hardening: the deletion run evicts
    # it eagerly at run end, but if that evict failed (network blip) the
    # pitcher would otherwise squat until its 10m keep_alive expires.
    from src.config import settings
    curator = settings.CURATOR_MODEL or settings.BASE_CURATOR_MODEL
    if not curator:
        return
    loaded = await loaded_models(ln.url)
    if loaded is None:
        logger.warning("Idle eviction skipped — VRAM residency unknown")
        return
    targets = [curator, (settings.PITCHER_MODEL or "").strip()]
    for model in [t for t in targets if t]:
        base_name = model.split(":")[0]
        if any(base_name in m.get("name", "") for m in loaded):
            logger.info("%s idle for %ds — evicting from VRAM", model, delay)
            await _evict_model(model, url=ln.url)
        else:
            logger.debug("%s already absent — nothing to evict", model)


def _pop_holder(ln: "_Lane", priority: int | None) -> int | None:
    """Drop one holder record. Callers release bare (``curator_done()``), so
    with no priority given the single holder is it — and with several
    (MAX_CONCURRENT_CURATOR > 1) a background record goes first, which can
    only make wait_quiet more cautious, never less."""
    prios = ln.holder_prios
    if not prios:
        return None
    if priority in prios:
        prios.remove(priority)
        return priority
    if len(prios) > 1 and BACKGROUND in prios:
        prios.remove(BACKGROUND)
        return BACKGROUND
    return prios.pop()


def curator_done(priority: int | None = None, lane: "_Lane | None" = None) -> None:
    """
    Signal that a curator call has finished.
    Resumes enrichment once all in-flight curator calls are done, and
    schedules a delayed curator eviction (Pass 14.7).

    Safe to call more times than ``curator_start`` (the counter is clamped
    at zero) — defensive in case future call sites end up with a mismatched
    pair under exception handling.
    """
    ln = _held_lane(lane)
    if ln.active <= 0:
        # More done() than start() (defensive, e.g. mismatched exception
        # handling). Nothing was acquired → don't over-release the gate.
        logger.debug("curator_done called with no active curator — ignoring")
        return
    ln.active -= 1
    ln.owner = ""
    if _pop_holder(ln, priority) == INTERACTIVE:
        touch_interactive()     # the quiet window runs from the END of a turn
    ln.gate.release()   # free the slot for the next queued curator call
    if ln.active == 0:
        ln.event().set()
        logger.info("All curator calls done on %s — enrichment there may resume", ln.url)
        # Schedule curator eviction. Cancel any pending one first (defensive
        # against rapid done→start→done sequences from concurrent streams).
        if ln.evict_task and not ln.evict_task.done():
            ln.evict_task.cancel()
        try:
            ln.evict_task = asyncio.create_task(_scheduled_curator_evict(ln))
        except RuntimeError:
            # No running loop (e.g. called from a sync test harness).
            # Skip scheduling — eviction will happen on Ollama's keep_alive
            # timer instead.
            ln.evict_task = None
    logger.debug("curator_done   active=%d on %s", ln.active, ln.url)


@asynccontextmanager
async def curator_priority(owner: str = "curator request", *,
                           exclusive_model: str = None,
                           priority: int = BACKGROUND,
                           role: str | None = None,
                           url: str | None = None):
    """Async context manager — guarantees ``curator_done`` even on exceptions.

    Preferred over manually pairing ``curator_start`` / ``curator_done``::

        async with curator_priority():
            await ollama_call(...)
            ...
    """
    ln = await curator_start(owner, exclusive_model=exclusive_model, priority=priority,
                             role=role, url=url)
    try:
        yield ln
    finally:
        curator_done(priority=priority, lane=ln)


async def background_curator_call(owner: str, call, *, exclusive_model: str = None,
                                  quiet_s: float = 0.0, max_preempts: int = 3,
                                  role: str = "curator"):
    """Run ``call()`` — a coroutine FACTORY doing curator-model requests — on
    the curator slot at BACKGROUND priority, and return its result.

    * ``quiet_s`` first waits for the user to have been idle that long
      (``wait_quiet``), so post-turn work never takes the GPU in the gap
      between a reply and the user's follow-up.
    * An interactive turn arriving while ``call()`` runs preempts it: the
      request task is cancelled (closing the connection stops Ollama's
      decode), the slot goes to the turn, and ``call()`` is re-run from the
      start once the turn is over. After ``max_preempts`` restarts it raises
      ``Preempted``.

    Every curator request that is NOT a user's chat turn belongs in here —
    a bare post to the curator model past the gate is a GPU holder nobody can
    see: a chat that got the slot then queued INSIDE Ollama behind it.
    """
    for _ in range(max_preempts + 1):
        await wait_quiet(quiet_s)
        ln = await curator_start(owner, exclusive_model=exclusive_model, priority=BACKGROUND,
                                 role=role)
        if interactive_waiting(ln):
            # A turn queued while we were still acquiring / evicting — before
            # the request was registered as preemptible. Hand the slot over
            # now instead of making it wait out a whole generation.
            curator_done(priority=BACKGROUND, lane=ln)
            quiet_s = max(quiet_s, 5.0)
            continue
        task = asyncio.ensure_future(call())
        ln.preemptible[task] = owner
        try:
            # asyncio.wait, not await: OUR cancel propagates, the request's
            # own (preemption) cancel is inspected below instead.
            await asyncio.wait({task})
        finally:
            ln.preemptible.pop(task, None)
            if not task.done():
                task.cancel()
            curator_done(priority=BACKGROUND, lane=ln)
        if task in _preempted:
            _preempted.discard(task)
            # Back off behind the turn that preempted us, not just the gate.
            quiet_s = max(quiet_s, 5.0)
            continue
        return task.result()
    raise Preempted(owner)


async def curator_resident(url: str | None = None) -> bool:
    """True when the curator bake is loaded in Ollama (one /api/ps GET)."""
    from src.config import settings
    model = settings.CURATOR_MODEL or settings.BASE_CURATOR_MODEL
    if not model:
        return False
    base_name = model.split(":")[0]
    loaded = await loaded_models(url)
    return bool(loaded) and any(base_name in m.get("name", "") for m in loaded)


async def make_room_for_summarizer(quiet_s: float = 45.0) -> None:
    """Background SUMMARISER work calls this before its Ollama request.

    On a 24 GB card the 8B summariser and the 20 GB curator do not fit side
    by side: a summariser load while the curator sits idle makes Ollama unload
    the curator, and the user's next message pays the reload. ``wait_for_
    curator`` only waits while a curator call is RUNNING — between turns the
    gate is open, so post-turn jobs walked straight in. This waits for a quiet
    window first, then for any running curator call, then evicts the idle
    curator (confirmed via /api/ps) so the load never races it into CPU
    offload either.

    The card is the one the summariser runs on (the "enrich" role's host): on
    a split install a chat on the PC's card no longer holds up the NAS's."""
    from src.config import settings
    await wait_quiet(quiet_s)
    await wait_for_curator()
    ln = lane_for("enrich")
    # Only after this process put a curator into VRAM is there anything to
    # clear: skips the /api/ps probe on every bulk-enrichment item otherwise.
    if ln.active or not ln.warm:
        return      # (a curator call that got in meanwhile evicts US, not vice versa)
    await evict_if_resident(settings.CURATOR_MODEL or settings.BASE_CURATOR_MODEL, url=ln.url)
    if not ln.active:
        ln.warm = False


# ── ENRICHMENT CHECKPOINT ─────────────────────────────────────────────────────

async def wait_for_curator(timeout: float = 600.0) -> None:
    """
    Enrichment workers call this before each LLM call.

    - Returns immediately when no curator is active.
    - When a curator is active: logs once, then waits up to *timeout* seconds.
      After the timeout it proceeds anyway so a stuck chat session can never
      block enrichment forever.

    Side effect: increments the lane's ``summarizer_pending`` while we wait,
    decrements on return (success or timeout). The post-curator eviction
    scheduler reads this counter to decide between the long and short
    idle-evict windows. Only a curator on the summariser's own card counts.
    """
    ln = lane_for("enrich")
    event = ln.event()
    if event.is_set():
        return
    ln.summarizer_pending += 1
    logger.info("Enrichment yielding to curator — will resume when chat finishes …")
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        logger.info("Curator released — enrichment resuming")
    except asyncio.TimeoutError:
        logger.warning(
            "curator priority wait timed out after %gs — resuming enrichment anyway",
            timeout,
        )
    finally:
        ln.summarizer_pending = max(0, ln.summarizer_pending - 1)


# ── PRIORITY ENRICHMENT (Pass 60) ─────────────────────────────────────────────

def priority_enrichment_start() -> None:
    """Signal that a user-triggered single-item re-enrich is starting.

    On the first active call, clears the priority-enrichment gate so the
    bulk enrichment consumer pauses after its current item. Unlike
    ``curator_start`` this does NOT evict the summarizer — a single-item
    re-enrich runs ON the summarizer, evicting it would be self-defeating.

    Counter-based so concurrent re-enriches (user clicks two items) are
    handled correctly — bulk only resumes once all of them are done.
    Atomic ``+= 1`` / ``== 1`` check (no await between) — same invariant
    as ``curator_start``.
    """
    global _pe_active
    _pe_active += 1
    if _pe_active == 1:
        _get_pe_event().clear()
    logger.debug("priority_enrichment_start  active=%d", _pe_active)


def priority_enrichment_done() -> None:
    """Signal that a single-item re-enrich finished. Bulk resumes once all
    in-flight priority re-enriches are done. Clamped at 0 — safe to call
    more than ``priority_enrichment_start`` under exception handling.
    """
    global _pe_active
    _pe_active = max(0, _pe_active - 1)
    if _pe_active == 0:
        _get_pe_event().set()
        logger.info("All priority re-enriches done — bulk enrichment may resume")
    logger.debug("priority_enrichment_done   active=%d", _pe_active)


@asynccontextmanager
async def priority_enrichment():
    """Async context manager — guarantees ``priority_enrichment_done`` even
    on exceptions. Wrap a user-triggered single-item re-enrich with it::

        async with priority_enrichment():
            await enrich_media_item(...)
    """
    priority_enrichment_start()
    try:
        yield
    finally:
        priority_enrichment_done()


# A long, legitimate curator job (the model-update test runs 20+ minutes)
# outlasts the 600 s safety timeout below, and bulk enrichment then loaded
# the summariser next to it — measured 2026-10-15: the new weights were
# timed with the summariser resident. Such a job extends this hold while it
# makes progress; a hold that is not renewed lapses, so a stuck job still
# cannot block enrichment for longer than its last extension.
_bulk_hold_until = 0.0


def extend_bulk_hold(seconds: float) -> None:
    """Keep bulk enrichment waiting for ``seconds`` more (from now)."""
    global _bulk_hold_until
    import time as _t
    _bulk_hold_until = max(_bulk_hold_until, _t.monotonic() + seconds)


def release_bulk_hold() -> None:
    global _bulk_hold_until
    _bulk_hold_until = 0.0


def _bulk_held() -> float:
    import time as _t
    return max(0.0, _bulk_hold_until - _t.monotonic())


async def wait_for_enrichment_slot(timeout: float = 600.0) -> None:
    """Bulk enrichment workers call this before each LLM call (replaces the
    bare ``wait_for_curator`` checkpoint in the bulk consumer).

    Bulk enrichment is the lowest priority tier — it waits while EITHER a
    curator call OR a user-triggered priority re-enrich is active. Returns
    immediately when both gates are open. After ``timeout`` it proceeds
    anyway so a stuck curator / re-enrich can never block the bulk run
    forever.

    Increments ``summarizer_pending`` while waiting, same as
    ``wait_for_curator`` — the post-curator eviction scheduler reads it.
    """
    ln = lane_for("enrich")
    curator_event = ln.event()
    pe_event = _get_pe_event()
    if curator_event.is_set() and pe_event.is_set():
        return

    ln.summarizer_pending += 1
    logger.info("Bulk enrichment yielding — curator or priority re-enrich active …")
    try:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        # Loop: wait on whichever gate is currently closed, then re-check
        # both — the other one could have closed while we waited.
        while not (curator_event.is_set() and pe_event.is_set()):
            remaining = max(deadline - loop.time(), _bulk_held())
            if remaining <= 0:
                logger.warning(
                    "Bulk enrichment slot wait timed out after %gs — resuming anyway",
                    timeout,
                )
                return
            closed = curator_event if not curator_event.is_set() else pe_event
            try:
                await asyncio.wait_for(closed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                if _bulk_held() > 0:
                    continue        # the holder renewed its hold meanwhile
                logger.warning(
                    "Bulk enrichment slot wait timed out after %gs — resuming anyway",
                    timeout,
                )
                return
        logger.info("Bulk enrichment resuming — higher-priority work done")
    finally:
        ln.summarizer_pending = max(0, ln.summarizer_pending - 1)
