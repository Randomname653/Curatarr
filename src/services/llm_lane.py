"""
Curatarr — where does an LLM call run right now?

The owner's 4090 is shared with an image-generation job for a while, and he
does not want to lose Curatarr in the meantime. Measured on his box on
2026-09-15 while that job held 17.5 of 24.5 GB at 90 % utilisation:

  curator, 19.9 GB      Ollama never got the model server up: the request
                        died after 304 s with "timed out waiting for
                        llama-server to start". Not slow — not started.
                        That is also what the summariser timeouts of the
                        evening before were.
  summariser, 5.3 GB    on the CPU with six threads, the app's own
                        SUMMARIZE_PROMPT filled from a real cache entry
                        (2,542 tokens in, 440 out): 145 s, and the GPU
                        never moved a megabyte.
  threads               six are as fast as twelve (17.4 against 17.6 tok/s
                        on a 3B): generation is memory-bandwidth bound, so
                        the other half of the Ryzen stays with his job.
  memory                the same run took 9.7 GB of system RAM — weights,
                        KV cache and mapped file pages, not the 5.3 GB of
                        the file. The card's tenant wants memory too (his
                        image job held 29 of 64 GB, leaving 7.8 GB at the
                        trough), so the lane checks for room before it
                        promises anything: below LLM_CPU_MIN_FREE_MB it
                        stays closed rather than push the box into swap.

Hence the split this module decides. Under GPU pressure summariser-class
work moves to the CPU and keeps the library current, curator-class work
waits and says so out loud. Nothing is attempted that cannot start, which
is the whole difference to the old behaviour: that one stopped the work
which would have run and attempted the one which could not.

Placement is returned as Ollama request options, so the call sites keep
their shape — see llm_utils.ollama_options (summariser) and
llm_utils.curator_options (curator, always the GPU).
"""
from __future__ import annotations

import logging
from typing import Tuple

from src.config import settings

logger = logging.getLogger(__name__)

GPU = "gpu"
CPU = "cpu"
NONE = "none"

SUMMARIZER = "summarizer"
CURATOR = "curator"

# Why the curator never gets a CPU lane: 19.9 GB of weights at the
# summariser's measured 5.9 tok/s would be tens of minutes for one reply,
# and the model has to be read from disk into RAM first. Waiting is the
# honest answer, so `lane(CURATOR)` says NONE instead of "cpu".
_CPU_CAPABLE = (SUMMARIZER,)


def _pressed() -> bool:
    """True when something that is not Ollama has been holding the GPU long
    enough to count (process_monitor.gpu_pressure, 30 s cache, 45 s hold)."""
    try:
        from src.services.process_monitor import gpu_pressure
        return bool(gpu_pressure())
    except Exception as e:                                   # pragma: no cover
        logger.debug("[lane] gpu_pressure unavailable: %s", e)
        return False


def _game() -> bool:
    """True when a known game process is running. A game owns the whole box,
    not just the card: the CPU lane stays off so its threads are not the
    thing that drops frames."""
    try:
        from src.services.process_monitor import game_process_running
        return bool(game_process_running())
    except Exception as e:                                   # pragma: no cover
        logger.debug("[lane] game detection unavailable: %s", e)
        return False


_ram_cache = {"at": 0.0, "free_mb": None}
_RAM_CACHE_S = 30


def ram_free_mb(*, now: "float | None" = None, reader=None) -> "int | None":
    """Free system memory in MB, cached for 30 s. None when psutil cannot
    answer — the lane then trusts the operator rather than refusing to work.
    ``reader`` is injectable for tests."""
    import time
    now = time.time() if now is None else now
    if reader is None and now - _ram_cache["at"] < _RAM_CACHE_S:
        return _ram_cache["free_mb"]
    try:
        if reader is None:
            import psutil
            free = int(psutil.virtual_memory().available / (1024 * 1024))
        else:
            free = reader()
            free = None if free is None else int(free)
    except Exception as e:                                   # pragma: no cover
        logger.debug("[lane] memory read failed: %s", e)
        free = None
    if reader is None:
        _ram_cache.update(at=now, free_mb=free)
    return free


def min_free_mb() -> int:
    try:
        return max(0, int(getattr(settings, "LLM_CPU_MIN_FREE_MB", 12000)))
    except (TypeError, ValueError):
        return 12000


def ram_ok(**kw) -> tuple:
    """(is there room for the lane, free MB or None). Unknown counts as room:
    a host without psutil should not lose its background work over a reading
    we could not take."""
    free = ram_free_mb(**kw)
    if free is None:
        return True, None
    return free >= min_free_mb(), free


def _cpu_threads() -> int:
    """The thread budget, clamped to [1, 64]. Unset or unreadable falls back
    to six — the measured sweet spot, not a guess."""
    try:
        n = int(getattr(settings, "LLM_CPU_THREADS", 6))
    except (TypeError, ValueError):
        return 6
    return max(1, min(n, 64))


def cpu_lane_enabled() -> bool:
    return bool(getattr(settings, "LLM_CPU_LANE", True))


def lane(role: str = SUMMARIZER, *, pressed: "bool | None" = None,
         game: "bool | None" = None, ram: "bool | None" = None) -> str:
    """Where a call of this role can run: GPU, CPU, or NONE (do not call).
    ``pressed``, ``game`` and ``ram`` are injectable for tests."""
    if pressed is None:
        pressed = _pressed()
    if not pressed:
        return GPU
    if game is None:
        game = _game()
    if game:
        return NONE
    if role in _CPU_CAPABLE and cpu_lane_enabled():
        if ram is None:
            ram = ram_ok()[0]
        return CPU if ram else NONE
    return NONE


def placement(role: str = SUMMARIZER, **kw) -> dict:
    """Ollama options that put the call where ``lane`` says it belongs.
    NONE returns the GPU placement: a caller that ignored ``available`` is
    better off failing fast against a busy GPU than silently running a
    19.9 GB model on the CPU for half an hour."""
    if lane(role, **kw) == CPU:
        return {"num_gpu": 0, "num_thread": _cpu_threads()}
    return {"num_gpu": 99}


def available(role: str = SUMMARIZER, **kw) -> Tuple[bool, str]:
    """(can this role run now, why not). The reason is user-facing text and
    names memory as well when that is what closed the lane."""
    if lane(role, **kw) != NONE:
        return True, ""
    parts = [p for p in (gpu_reason(), ram_reason()) if p]
    return False, "; ".join(parts)


def curator_available() -> Tuple[bool, str]:
    return available(CURATOR)


def ram_reason() -> str:
    """Why the lane is closed for memory, empty when it is not."""
    ok, free = ram_ok()
    if ok or free is None:
        return ""
    return f"{free / 1024:.1f} GB RAM free, {min_free_mb() / 1024:.0f} GB needed"


def gpu_reason() -> str:
    """What the GPU read said the last time pressure was measured, e.g.
    "17467/24564 MB, 90 %". Empty when nothing is holding it."""
    try:
        from src.services.process_monitor import gpu_pressure_reason
        return gpu_pressure_reason()
    except Exception:                                        # pragma: no cover
        return ""


# The message the chat shows instead of attempting a generation that cannot
# start. Plain text, no emoji — it is rendered as a curator reply.
_BUSY_HEAD = ("The GPU is busy with another program right now, and there is no room "
              "to load my model.")
_BUSY_TAIL_LANE = ("Nothing is lost: enrichment, lyrics profiles and memory keep running on the "
                   "processor in the background, just slower. For a conversation I need the "
                   "graphics card back — end the job that is holding it, or come back once it "
                   "has finished.")
_BUSY_TAIL_WAIT = ("The background work is waiting too, so nothing is lost, it just stands still "
                   "until there is room again. End the job that is holding the machine, or come "
                   "back once it has finished.")


def busy_message(reason: str = "", *, lane_open: "bool | None" = None) -> str:
    """The notice the chat shows. It promises background progress only when
    the lane is actually open — with the memory too tight for it, saying
    "everything keeps running" would be a lie."""
    if lane_open is None:
        lane_open = lane(SUMMARIZER) == CPU
    middle = f" It is holding {reason}." if reason else ""
    tail = _BUSY_TAIL_LANE if lane_open else _BUSY_TAIL_WAIT
    return f"{_BUSY_HEAD}{middle}\n\n{tail}"


def status() -> dict:
    """For the API / Activity view: one look at both roles."""
    pressed = _pressed()
    game = _game() if pressed else False
    ram_free = ram_free_mb()
    ram = ram_ok()[0]
    reasons = [p for p in (gpu_reason() if pressed else "", ram_reason()) if p]
    return {"gpu_pressed": pressed, "game": game,
            "reason": "; ".join(reasons),
            "curator": lane(CURATOR, pressed=pressed, game=game, ram=ram),
            "summarizer": lane(SUMMARIZER, pressed=pressed, game=game, ram=ram),
            "cpu_threads": _cpu_threads(), "cpu_lane": cpu_lane_enabled(),
            "ram_free_mb": ram_free, "ram_min_mb": min_free_mb()}
