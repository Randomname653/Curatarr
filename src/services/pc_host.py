"""What Curatarr knows about the gaming PC (2026-10-22, NAS move phase 2).

Curatarr on the NAS polls the PC agent (src/pc_agent.py) every POLL_S
seconds. The PC counts as AVAILABLE after one good answer, and as gone after
MISSES_DOWN missed ones in a row: one dropped packet must not flip the
routing, a powered-off PC must not hold work for the 5-10 minutes an HTTP
timeout would. A shutdown notice (the agent's goodbye, or ``shutting_down``
in a status) takes it away at once - DRAINING until it answers again
without the flag.

process_monitor reads ``snapshot()`` for game detection: the PC's processes
and GPU instead of the NAS's own. Without PC_AGENT_URL nothing here runs and
Curatarr reads its own machine, as before.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

POLL_S = 5
MISSES_DOWN = 2
TIMEOUT_S = 3.0

_state: dict = {"available": False, "draining": False, "misses": 0, "busy": False,
                "busy_reason": "", "snapshot": None, "seen_at": 0.0, "changed_at": 0.0,
                "generation": 0,
                "reason": "never polled"}


def _settings():
    from src.config import settings
    return settings


def token() -> str:
    t = getattr(_settings(), "PC_AGENT_TOKEN", None)
    t = t.get_secret_value() if hasattr(t, "get_secret_value") else (t or "")
    return str(t).strip()


def configured() -> bool:
    """A PC agent is set up: its URL and the shared token."""
    return bool((getattr(_settings(), "PC_AGENT_URL", "") or "").strip() and token())


def available() -> bool:
    return bool(_state["available"])


def busy() -> bool:
    """A game runs on the PC or something else holds its card (judged on
    the NAS from the PC's report, every poll - process_monitor.pc_busy)."""
    return bool(_state["busy"])


def _taking_work() -> bool:
    return bool(_state["available"] and not _state["draining"] and not _state["busy"])


def generation() -> int:
    """Counts the times the PC STOPPED taking work (gone, draining, busy).
    A call on the PC that sees it change was cut off (services/pc_guard)."""
    return int(_state["generation"])


def _note_lost(was_taking: bool) -> None:
    if was_taking and not _taking_work():
        _state["generation"] += 1


def set_busy(value: bool, reason: str = "") -> None:
    was = _taking_work()
    if bool(value) != _state["busy"]:
        logger.info("[node] %s %s", node_name(), f"busy ({reason}) - no new work for its card"
                    if value else "free again")
    _state.update(busy=bool(value), busy_reason=reason if value else "")
    _note_lost(was)


def snapshot() -> Optional[dict]:
    """The PC's last report while it is available, else None."""
    return _state["snapshot"] if _state["available"] else None


def build() -> Optional[dict]:
    """The PC checkout's update state from its last report (src/self_update):
    {"head", "latest", "behind", "can_update", "reason"}, or None (an older
    agent, or the PC is not available)."""
    snap = snapshot()
    b = (snap or {}).get("build")
    return b if isinstance(b, dict) else None


# ── the node's name (2026-10-23) ────────────────────────────────────────────
# Not "the gaming PC": a node, named like a Tdarr node. It reports a name of
# its own (PC_AGENT_NAME on the node, else its computer name), and the owner
# can rename it here for good. Both are kept in app_state, so the name shows
# while the node is away and after a restart.
NAME_MAX = 40
_CUSTOM_KEY = "node_name"
_REPORTED_KEY = "node_name_reported"
DEFAULT_NAME = "the node"
_names: dict = {"custom": "", "reported": "", "loaded": False, "dirty": False}


def clean_name(name) -> str:
    """A name as stored: printable, single-spaced, at most NAME_MAX long."""
    s = " ".join(str(name or "").split())
    return "".join(ch for ch in s if ch.isprintable())[:NAME_MAX].strip()


def node_name(cap: bool = False) -> str:
    """What the UI and the log call the node: the owner's name for it, else
    the one it reports, else "the node" ("The node" with ``cap``)."""
    name = _names["custom"] or _names["reported"]
    if name:
        return name
    return DEFAULT_NAME.capitalize() if cap else DEFAULT_NAME


async def load_names() -> None:
    """Read the stored names once (the first poll does it, off the loop)."""
    if _names["loaded"]:
        return
    from src.services.app_state import aget_state
    try:
        _names["custom"] = clean_name(await aget_state(_CUSTOM_KEY))
        _names["reported"] = clean_name(await aget_state(_REPORTED_KEY))
        _names["loaded"] = True
    except Exception as e:  # noqa: BLE001 - a name is cosmetic; the next poll retries
        logger.debug("[node] stored names unreadable: %s", e)


async def set_custom_name(name) -> str:
    """Rename the node; an empty name goes back to the reported one."""
    from src.services.app_state import aset_state
    value = clean_name(name)
    await aset_state(_CUSTOM_KEY, value)
    _names["custom"] = value
    _names["loaded"] = True
    logger.info("[node] renamed to %s", value or f"its own name ({node_name()})")
    return node_name()


def _note_reported(report: dict) -> None:
    node = report.get("node") if isinstance(report.get("node"), dict) else {}
    name = clean_name(node.get("name"))
    if name and name != _names["reported"]:
        _names["reported"] = name
        _names["dirty"] = True


# ── when the node last took work (2026-11-01) ────────────────────────────────
# Deletion runs wait for the node (ollama_hosts.PC_ONLY), but not forever:
# once it has not been free for JUDGE_FALLBACK_HOURS the server judges with
# its own model. The moment is kept in app_state, so a restart of the server
# does not reset the clock; written at most every FREE_WRITE_S.
_FREE_KEY = "node_last_free"
FREE_WRITE_S = 600
_free: dict = {"at": 0.0, "stored": 0.0, "loaded": False, "start": time.time()}


def last_free() -> float:
    """When the node last took work (epoch seconds). Before it was ever
    seen: when this server started, so a node that never answers counts as
    away since then."""
    return _free["at"] or _free["start"]


async def _load_free() -> None:
    if _free["loaded"]:
        return
    from src.services.app_state import aget_state
    try:
        stored = float(await aget_state(_FREE_KEY) or 0)
    except (TypeError, ValueError):
        stored = 0.0
    except Exception as e:  # noqa: BLE001 - unreadable: the clock starts now
        logger.debug("[node] last-free time not read: %s", e)
        stored = 0.0
    _free.update(at=max(_free["at"], stored), stored=stored, loaded=True)


async def _note_free() -> None:
    """The node takes work now: move the clock, store it now and then."""
    now = time.time()
    _free["at"] = now
    if now - _free["stored"] < FREE_WRITE_S:
        return
    from src.services.app_state import aset_state
    try:
        await aset_state(_FREE_KEY, str(int(now)))
        _free["stored"] = now
    except Exception as e:  # noqa: BLE001 - kept in memory, written next time
        logger.debug("[node] last-free time not stored: %s", e)


# The node's card size from its last report, kept like the clock above:
# Settings shows the bench's advice for the node while it is away too.
_VRAM_KEY = "node_vram_mb"
_card: dict = {"vram_mb": 0, "stored": 0, "loaded": False}


def last_vram_mb() -> int:
    """The total memory of the node's card as it last reported it (0: never)."""
    return int(_card["vram_mb"])


async def _load_card() -> None:
    if _card["loaded"]:
        return
    from src.services.app_state import aget_state
    try:
        _card["stored"] = _card["vram_mb"] = int(float(await aget_state(_VRAM_KEY) or 0))
    except Exception as e:  # noqa: BLE001 - unreadable: the next report sets it
        logger.debug("[node] card size not read: %s", e)
    _card["loaded"] = True


async def _note_card(report: dict) -> None:
    from src.services.app_state import aset_state
    gpu = report.get("gpu") if isinstance(report, dict) else None
    if not (isinstance(gpu, (list, tuple)) and len(gpu) >= 2):
        return
    try:
        total = int(float(gpu[1]))
    except (TypeError, ValueError):
        return
    if total <= 0:
        return
    _card["vram_mb"] = total
    if total != _card["stored"]:
        try:
            await aset_state(_VRAM_KEY, str(total))
            _card["stored"] = total
        except Exception as e:  # noqa: BLE001 - kept in memory, written next time
            logger.debug("[node] card size not stored: %s", e)


def status() -> dict:
    """For the UI and the logs: available / draining / since when / why."""
    return {k: _state[k] for k in ("available", "draining", "busy", "busy_reason", "seen_at",
                                   "changed_at", "reason")} | {"last_free": last_free()} | {
        "configured": configured(), "build": build(), "name": node_name(),
        "name_custom": _names["custom"], "name_reported": _names["reported"]}


async def request_update(post=None) -> "tuple[int, dict]":
    """Ask the agent to fast-forward its checkout and restart (POST /update).
    (HTTP status, body); the agent decides whether it can. ``post(url, token)``
    is injectable for tests."""
    url = _settings().PC_AGENT_URL.rstrip("/") + "/update"
    return await (post or _post_update)(url, token())


async def _post_update(url: str, tok: str) -> "tuple[int, dict]":
    import httpx
    from src.pc_agent import TOKEN_HEADER
    from src.services.http_client import tls_context
    # A fetch plus a fast-forward: seconds, not the poll's 3 s.
    async with httpx.AsyncClient(verify=tls_context(), timeout=120) as client:
        r = await client.post(url, headers={TOKEN_HEADER: tok})
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body if isinstance(body, dict) else {}


def _set(available: bool, reason: str, *, draining: bool = False) -> None:
    was = _taking_work()
    if available != _state["available"] or draining != _state["draining"]:
        _state["changed_at"] = time.time()
        logger.info("[node] %s %s (%s)", node_name(), "available" if available
                    else ("draining - no new work" if draining else "unavailable"), reason)
    _state.update(available=available, draining=draining, reason=reason)
    _note_lost(was)
    if not available:
        _state["snapshot"] = None


def record(report: Optional[dict], error: str = "") -> None:
    """One poll's outcome: the agent's report, or None with what went wrong."""
    if report is not None and isinstance(report, dict):
        _state["misses"] = 0
        _state["seen_at"] = time.time()
        if report.get("shutting_down"):
            _set(False, "shutting down", draining=True)
            return
        _state["snapshot"] = report
        _note_reported(report)
        _set(True, "agent answered")
        return
    _state["misses"] += 1
    if _state["available"] and _state["misses"] >= MISSES_DOWN:
        _set(False, f"{_state['misses']} polls unanswered ({error or 'no answer'})")
    elif not _state["available"] and not _state["draining"]:
        _state["reason"] = error or "no answer"


def goodbye(reason: str = "shutdown") -> None:
    """The agent's shutdown notice: stop sending work to the PC now."""
    _set(False, f"said goodbye ({reason})", draining=True)


async def poll_once(fetch=None) -> None:
    """One poll. ``fetch(url, token) -> dict`` is injectable for tests."""
    if not configured():
        return
    await load_names()
    await _load_free()
    await _load_card()
    url = _settings().PC_AGENT_URL.rstrip("/") + "/status"
    try:
        report = await (fetch or _fetch)(url, token())
    except asyncio.CancelledError:
        # The app is stopping (the scheduler cancels the poll in flight): no
        # missed poll, and no ERROR traceback in every shutdown's log.
        return
    except Exception as e:  # noqa: BLE001 - every failure is one missed poll
        record(None, f"{type(e).__name__}: {e}"[:200])
        # Away long enough, the server judges itself: a queued Analyze
        # starts then, without the node.
        await _kick_queue()
        return
    record(report)
    await _note_card(report)
    if _names["dirty"]:
        from src.services.app_state import aset_state
        try:
            await aset_state(_REPORTED_KEY, _names["reported"])
            _names["dirty"] = False
        except Exception as e:  # noqa: BLE001 - kept in memory, written next poll
            logger.debug("[node] reported name not stored: %s", e)
    # Split install: is the PC gaming? Judged here, every poll, so a game
    # that starts takes the PC out of the routing within seconds.
    from src.services.ollama_hosts import split
    if available() and split():
        from src.services.process_monitor import pc_busy
        try:
            value, why = await asyncio.to_thread(pc_busy, report)
        except Exception as e:  # noqa: BLE001 - fails closed: no verdict, no work for the PC
            logger.warning("[node] %s busy check failed: %s", node_name(), e)
            value, why = True, "busy check failed"
        set_busy(value, why)
    if _taking_work():
        await _note_free()
    # An Analyze queued while the node was away starts now (deletion_queue
    # asks judge_ready: the node free, or away long enough).
    await _kick_queue()


async def _kick_queue() -> None:
    from src.services import deletion_queue
    try:
        await deletion_queue.kick()
    except Exception as e:  # noqa: BLE001 - the next poll tries again
        logger.warning("[node] queued analysis not started: %s", e)


async def _fetch(url: str, tok: str) -> dict:
    import httpx
    from src.pc_agent import TOKEN_HEADER
    from src.services.http_client import tls_context
    async with httpx.AsyncClient(verify=tls_context(), timeout=TIMEOUT_S) as client:
        r = await client.get(url, headers={TOKEN_HEADER: tok})
    r.raise_for_status()
    return r.json()
