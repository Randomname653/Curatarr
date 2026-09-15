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
         game: "bool | None" = None) -> str:
    """Where a call of this role can run: GPU, CPU, or NONE (do not call).
    ``pressed`` and ``game`` are injectable for tests."""
    if pressed is None:
        pressed = _pressed()
    if not pressed:
        return GPU
    if game is None:
        game = _game()
    if game:
        return NONE
    if role in _CPU_CAPABLE and cpu_lane_enabled():
        return CPU
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
    """(can this role run now, why not). The reason is user-facing text."""
    if lane(role, **kw) != NONE:
        return True, ""
    return False, gpu_reason()


def curator_available() -> Tuple[bool, str]:
    return available(CURATOR)


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
_BUSY_TAIL = ("Nothing is lost: enrichment, lyrics profiles and memory keep running on the "
              "processor in the background, just slower. For a conversation I need the "
              "graphics card back — end the job that is holding it, or come back once it "
              "has finished.")


def busy_message(reason: str = "") -> str:
    middle = f" It is holding {reason}." if reason else ""
    return f"{_BUSY_HEAD}{middle}\n\n{_BUSY_TAIL}"


def status() -> dict:
    """For the API / Activity view: one look at both roles."""
    pressed = _pressed()
    game = _game() if pressed else False
    return {"gpu_pressed": pressed, "game": game,
            "reason": gpu_reason() if pressed else "",
            "curator": lane(CURATOR, pressed=pressed, game=game),
            "summarizer": lane(SUMMARIZER, pressed=pressed, game=game),
            "cpu_threads": _cpu_threads(), "cpu_lane": cpu_lane_enabled()}
