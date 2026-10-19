"""Silent-handler sweep, group 4 (docs/SILENT_FAILURE_SWEEP_2026_10.md).

The handlers here guarded a decision and let it proceed when the guard
itself failed. Each check fails on the code before this change:
  * OpenSubtitles: a download whose quota charge could not be stored
    pauses the service for the day instead of letting later titles
    overspend a (possibly shared) account's daily budget
  * curator principles: a broken curator yield gate skips the background
    LLM call instead of taking the GPU from a live chat

    python tests/test_silent_handlers_g4.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.services.app_state as app_state  # noqa: E402
import src.services.curator_principles as cp  # noqa: E402
import src.services.llm_priority as llm_priority  # noqa: E402
import src.services.subtitle_signals as subs  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── 1. OpenSubtitles: an unrecorded charge pauses the service ───────────────
print("opensubtitles quota charge")
_orig_get, _orig_set = app_state.get_state, app_state.set_state
_orig_conf = subs._os_conf


def _broken_set(key, value):
    raise RuntimeError("database is locked")


try:
    app_state.get_state = lambda key: "0"    # counter readable, nothing blocked
    app_state.set_state = _broken_set
    check("precondition: not blocked before the charge", not subs._os_blocked_today())
    subs._os_charge()
    check("a charge that could not be stored blocks OpenSubtitles today",
          subs._os_blocked_today())
    subs._os_conf = lambda: ("key", "", "", 100)
    called = []

    class _NoNetwork:
        def __init__(self, *a, **k):
            called.append(1)
            raise AssertionError("network used while paused")

    import httpx
    _orig_async = httpx.AsyncClient
    httpx.AsyncClient = _NoNetwork
    try:
        res = asyncio.run(subs.fetch_opensubtitles("tt0111161"))
    finally:
        httpx.AsyncClient = _orig_async
    check("fetch_opensubtitles answers transient (None) without calling out",
          res is None and not called)
    getattr(subs, "_os_uncharged", {})["day"] = "subs_os_dl_19990101"
    app_state.get_state = lambda key: None
    check("the pause is scoped to the day it happened on", not subs._os_blocked_today())
finally:
    app_state.get_state, app_state.set_state = _orig_get, _orig_set
    subs._os_conf = _orig_conf
    getattr(subs, "_os_uncharged", {})["day"] = None


# ── 2. curator principles: a broken yield gate skips the call ───────────────
print("curator principles yield gate")
_orig_wait = llm_priority.wait_for_curator
_orig_cp_client = cp.httpx.AsyncClient
posted = []


async def _broken_wait(*a, **k):
    raise RuntimeError("gate bound to another loop")


class _Recorder:
    def __init__(self, *a, **k):
        posted.append(1)
        raise ConnectionError("recorded")


try:
    llm_priority.wait_for_curator = _broken_wait
    cp.httpx.AsyncClient = _Recorder
    err = None
    try:
        asyncio.run(cp._curator_json("sys", "user", {"type": "object"}))
    except Exception as e:
        err = e
    check("the curator model is never called when the gate fails", not posted)
    check("the gate's own error reaches the caller (which logs and skips)",
          isinstance(err, RuntimeError))
finally:
    llm_priority.wait_for_curator = _orig_wait
    cp.httpx.AsyncClient = _orig_cp_client


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
