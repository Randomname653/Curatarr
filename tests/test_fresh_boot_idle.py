"""The interactivity clock on a freshly booted host.

time.monotonic() counts seconds since boot on Linux. llm_priority's
``_last_interactive`` started at 0.0, so on a host up for less than the
quiet window (a new container, a CI runner) seconds_since_interactive()
returned the uptime and background LLM work waited out a quiet window for
a user who had never been there. It now starts at -inf: no sign of a user
yet reads as idle for ever.

This suite never sets ``_last_interactive`` itself: it checks the value
the module starts with (run_all gives every suite its own process).
"""
from _harness import check  # noqa: I001 - first: temp stores

import asyncio
import time
import types

import src.services.llm_priority as lp  # noqa: E402
from src.services.llm_utils import CURATOR_IDLE_KEEP_S  # noqa: E402

# A host up for 20 s. Only llm_priority's own clock is pinned: the event
# loop keeps the real one, so the timeout below still fires.
_clock = types.ModuleType("time")
_clock.__dict__.update(time.__dict__)
_clock.monotonic = lambda: 20.0
_real_time = lp.time
lp.time = _clock
try:
    idle = lp.seconds_since_interactive()
    check(f"no sign of a user yet reads as idle at boot+20 s ({idle})",
          idle >= CURATOR_IDLE_KEEP_S)

    async def _quiet():
        lp._reset_lanes()
        try:
            await asyncio.wait_for(lp.wait_quiet(45.0), timeout=2)
            return True
        except asyncio.TimeoutError:
            return False

    check("...so background work does not wait out a quiet window for nobody",
          asyncio.run(_quiet()))

    lp.touch_interactive()
    check("a real sign of the user still starts the window",
          lp.seconds_since_interactive() == 0.0)
finally:
    lp.time = _real_time

check.done()
