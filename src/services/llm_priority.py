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
* In single-threaded asyncio the ``_active += 1`` / ``== 1`` check is atomic
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
import logging
from contextlib import asynccontextmanager

import httpx

logger = logging.getLogger(__name__)

# Lazy-bound to the running loop. asyncio.Event in 3.10+ no longer takes a
# loop param at construction, but binding lazily keeps us robust against
# import-time-vs-runtime-loop confusion.
_event: asyncio.Event | None = None
_active = 0

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

# Counter of summarizer calls currently blocked in wait_for_curator(). When
# the curator finishes we use this to choose between the long idle-evict
# delay (no one waiting → user might come back) and the short busy delay
# (background pipeline is starving for the summarizer).
_summarizer_pending = 0

# Background task that fires `_evict_model(curator)` after an idle period.
# Cancelled and rescheduled on each curator activity transition.
_curator_evict_task: asyncio.Task | None = None


def _get_event() -> asyncio.Event:
    """Lazy accessor so the Event binds to whichever loop first uses it."""
    global _event
    if _event is None:
        _event = asyncio.Event()
        _event.set()    # set = no curator running → enrichment may proceed
    return _event


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
_gpu_gate: asyncio.Semaphore | None = None


def _get_gate() -> asyncio.Semaphore:
    """Lazy global curator slot, bound to the running loop on first use."""
    global _gpu_gate
    if _gpu_gate is None:
        from src.config import settings
        n = max(1, int(getattr(settings, "MAX_CONCURRENT_CURATOR", 1) or 1))
        _gpu_gate = asyncio.Semaphore(n)
    return _gpu_gate


def curator_busy() -> bool:
    """True when every curator slot is taken — the next ``curator_start`` will
    block. Callers (e.g. the chat stream) use this to tell a waiting user
    they're queued instead of leaving a silent dead window."""
    return _get_gate().locked()


# Who currently holds the gate (debug label, e.g. "deletion scan: anime") and
# how many callers are queued behind it. The waiter count lets a long-running
# batch (the judge funnel) YIELD between items when someone is waiting — an
# interactive chat gets the GPU within one verdict instead of after the whole
# category.
_gate_owner: str = ""
_gate_waiters: int = 0


def gate_owner() -> str:
    """Label of the current gate holder ('' when free) — for busy messages."""
    return _gate_owner if curator_busy() else ""


def gate_contested() -> bool:
    """True when at least one caller is queued on the curator gate."""
    return _gate_waiters > 0


# ── MODEL INTROSPECTION ───────────────────────────────────────────────────────

async def loaded_models() -> list[dict]:
    """
    Return the list of models currently resident in Ollama VRAM.

    Each entry from Ollama /api/ps looks like::

        {"name": "qwen3.6:27b", "size_vram": 22123456789, "size": 22123456789, ...}

    Returns an empty list on error or when nothing is loaded.
    """
    from src.config import settings
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{settings.effective_ollama}/api/ps")
        if r.status_code == 200:
            return r.json().get("models", [])
    except Exception:
        pass
    return []


async def _evict_model(model_name: str, timeout: float = 8.0) -> bool:
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
    from src.config import settings
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"{settings.effective_ollama}/api/generate",
                json={"model": model_name, "keep_alive": 0},
            )
    except Exception as e:
        logger.debug("Eviction request failed for %s: %s", model_name, e)
        return False

    # Poll /api/ps until the model is gone. Match by base name (without
    # the ":tag" suffix) because Ollama may report "x:latest" or "x".
    base_name = model_name.split(":")[0]
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        loaded = await loaded_models()
        still_loaded = any(base_name in m.get("name", "") for m in loaded)
        if not still_loaded:
            logger.info("Confirmed evicted from VRAM: %s", model_name)
            return True
        await asyncio.sleep(0.3)
    logger.warning(
        "Eviction timeout for %s after %.0fs — VRAM may still be busy; "
        "curator may load partially on CPU",
        model_name, timeout,
    )
    return False


async def check_curator_vram_health(curator_model: str) -> dict:
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
    loaded = await loaded_models()
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
                        exclusive_model: str = None) -> None:
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
    """
    global _active, _curator_evict_task, _gate_owner, _gate_waiters
    # Serialize big-model generations across the whole server: block here
    # until a curator slot is free (MAX_CONCURRENT_CURATOR, default 1). A
    # second chatter / recs / proactive call waits instead of fighting for
    # the GPU. Acquired BEFORE touching _active so a cancelled acquire (user
    # disconnects while queued) leaves no state to unwind, and so the gate is
    # released exactly once per successful acquire in curator_done().
    _gate_waiters += 1
    try:
        await _get_gate().acquire()
    finally:
        _gate_waiters -= 1
    _gate_owner = owner
    _active += 1
    is_first = (_active == 1)

    # Pass 14.7: cancel any pending idle-eviction. The user is back; we want
    # the curator to stay in VRAM, not get evicted mid-conversation.
    if _curator_evict_task and not _curator_evict_task.done():
        _curator_evict_task.cancel()
        logger.debug("Cancelled pending curator eviction (new chat activity)")

    if is_first:
        # Clear the gate immediately — before any await — so any enrichment
        # worker hitting wait_for_curator() right now blocks correctly.
        _get_event().clear()
    logger.debug("curator_start  active=%d", _active)

    if is_first:
        from src.config import settings
        target = (exclusive_model or settings.CURATOR_MODEL
                  or settings.BASE_CURATOR_MODEL)
        await evict_others(target)


async def evict_others(target_model: str) -> None:
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
    loaded = await loaded_models()
    evicted = False
    for model in candidates:
        base_name = model.split(":")[0]
        if any(base_name in m.get("name", "") for m in loaded):
            logger.info("Gate holder needs %s — evicting resident %s",
                        target_model, model)
            await _evict_model(model)
            evicted = True
    if not evicted:
        logger.info("Gate holder needs %s — no other big model resident",
                    target_model)


async def evict_if_resident(model_name: str) -> None:
    """Guarded single-model evict (run-end cleanup). No-ops when the
    model is not in VRAM — see evict_others for why the guard matters."""
    if not model_name:
        return
    base_name = model_name.split(":")[0]
    loaded = await loaded_models()
    if any(base_name in m.get("name", "") for m in loaded):
        await _evict_model(model_name)


async def resolve_pitcher_model() -> str:
    """The model a deletion run should use: PITCHER_MODEL when configured
    AND installed, else the curator bake.

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
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{settings.effective_ollama}/api/tags")
        if r.status_code == 200:
            names = [m.get("name", "") or m.get("model", "")
                     for m in r.json().get("models", [])]
            for n in names:
                if pitcher == n or pitcher in n or n in pitcher:
                    return pitcher
    except Exception as e:
        logger.debug("pitcher install probe failed: %s", e)
    logger.warning("PITCHER_MODEL=%s not installed — deletion run falls "
                   "back to %s (run `python build_models.py`)",
                   pitcher, fallback)
    return fallback


async def _scheduled_curator_evict() -> None:
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

    delay = CURATOR_IDLE_EVICT_BUSY if _summarizer_pending > 0 else CURATOR_IDLE_EVICT_SECONDS
    if _summarizer_pending > 0:
        logger.info(
            "Curator idle: %d summarizer call(s) waiting — eviction in %ds",
            _summarizer_pending, delay,
        )
    else:
        logger.debug("Curator idle: scheduling eviction in %ds", delay)

    try:
        await asyncio.sleep(delay)
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
    loaded = await loaded_models()
    targets = [curator, (settings.PITCHER_MODEL or "").strip()]
    for model in [t for t in targets if t]:
        base_name = model.split(":")[0]
        if any(base_name in m.get("name", "") for m in loaded):
            logger.info("%s idle for %ds — evicting from VRAM", model, delay)
            await _evict_model(model)
        else:
            logger.debug("%s already absent — nothing to evict", model)


def curator_done() -> None:
    """
    Signal that a curator call has finished.
    Resumes enrichment once all in-flight curator calls are done, and
    schedules a delayed curator eviction (Pass 14.7).

    Safe to call more times than ``curator_start`` (the counter is clamped
    at zero) — defensive in case future call sites end up with a mismatched
    pair under exception handling.
    """
    global _active, _curator_evict_task, _gate_owner
    if _active <= 0:
        # More done() than start() (defensive, e.g. mismatched exception
        # handling). Nothing was acquired → don't over-release the gate.
        logger.debug("curator_done called with no active curator — ignoring")
        return
    _active -= 1
    _gate_owner = ""
    _get_gate().release()   # free the slot for the next queued curator call
    if _active == 0:
        _get_event().set()
        logger.info("All curator calls done — enrichment may resume")
        # Schedule curator eviction. Cancel any pending one first (defensive
        # against rapid done→start→done sequences from concurrent streams).
        if _curator_evict_task and not _curator_evict_task.done():
            _curator_evict_task.cancel()
        try:
            _curator_evict_task = asyncio.create_task(_scheduled_curator_evict())
        except RuntimeError:
            # No running loop (e.g. called from a sync test harness).
            # Skip scheduling — eviction will happen on Ollama's keep_alive
            # timer instead.
            _curator_evict_task = None
    logger.debug("curator_done   active=%d", _active)


@asynccontextmanager
async def curator_priority(owner: str = "curator request", *,
                           exclusive_model: str = None):
    """Async context manager — guarantees ``curator_done`` even on exceptions.

    Preferred over manually pairing ``curator_start`` / ``curator_done``::

        async with curator_priority():
            await ollama_call(...)
            ...
    """
    await curator_start(owner, exclusive_model=exclusive_model)
    try:
        yield
    finally:
        curator_done()


# ── ENRICHMENT CHECKPOINT ─────────────────────────────────────────────────────

async def wait_for_curator(timeout: float = 600.0) -> None:
    """
    Enrichment workers call this before each LLM call.

    - Returns immediately when no curator is active.
    - When a curator is active: logs once, then waits up to *timeout* seconds.
      After the timeout it proceeds anyway so a stuck chat session can never
      block enrichment forever.

    Side effect: increments ``_summarizer_pending`` while we wait, decrements
    on return (success or timeout). The post-curator eviction scheduler
    reads this counter to decide between the long and short idle-evict
    windows.
    """
    global _summarizer_pending
    event = _get_event()
    if event.is_set():
        return
    _summarizer_pending += 1
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
        _summarizer_pending = max(0, _summarizer_pending - 1)


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


async def wait_for_enrichment_slot(timeout: float = 600.0) -> None:
    """Bulk enrichment workers call this before each LLM call (replaces the
    bare ``wait_for_curator`` checkpoint in the bulk consumer).

    Bulk enrichment is the lowest priority tier — it waits while EITHER a
    curator call OR a user-triggered priority re-enrich is active. Returns
    immediately when both gates are open. After ``timeout`` it proceeds
    anyway so a stuck curator / re-enrich can never block the bulk run
    forever.

    Increments ``_summarizer_pending`` while waiting, same as
    ``wait_for_curator`` — the post-curator eviction scheduler reads it.
    """
    global _summarizer_pending
    curator_event = _get_event()
    pe_event = _get_pe_event()
    if curator_event.is_set() and pe_event.is_set():
        return

    _summarizer_pending += 1
    logger.info("Bulk enrichment yielding — curator or priority re-enrich active …")
    try:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        # Loop: wait on whichever gate is currently closed, then re-check
        # both — the other one could have closed while we waited.
        while not (curator_event.is_set() and pe_event.is_set()):
            remaining = deadline - loop.time()
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
                logger.warning(
                    "Bulk enrichment slot wait timed out after %gs — resuming anyway",
                    timeout,
                )
                return
        logger.info("Bulk enrichment resuming — higher-priority work done")
    finally:
        _summarizer_pending = max(0, _summarizer_pending - 1)
