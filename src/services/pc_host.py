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

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

POLL_S = 5
MISSES_DOWN = 2
TIMEOUT_S = 3.0

_state: dict = {"available": False, "draining": False, "misses": 0, "busy": False,
                "busy_reason": "", "snapshot": None, "seen_at": 0.0, "changed_at": 0.0,
                "reason": "never polled"}


def _settings():
    from src.config import settings
    return settings


def token() -> str:
    t = _settings().PC_AGENT_TOKEN
    t = t.get_secret_value() if hasattr(t, "get_secret_value") else (t or "")
    return str(t).strip()


def configured() -> bool:
    """A PC agent is set up: its URL and the shared token."""
    return bool((_settings().PC_AGENT_URL or "").strip() and token())


def available() -> bool:
    return bool(_state["available"])


def busy() -> bool:
    """A game runs on the PC or something else holds its card (judged on
    the NAS from the PC's report, every poll - process_monitor.pc_busy)."""
    return bool(_state["busy"])


def set_busy(value: bool, reason: str = "") -> None:
    if bool(value) != _state["busy"]:
        logger.info("[pc] %s", f"busy ({reason}) - no new work for its card" if value
                    else "free again")
    _state.update(busy=bool(value), busy_reason=reason if value else "")


def snapshot() -> Optional[dict]:
    """The PC's last report while it is available, else None."""
    return _state["snapshot"] if _state["available"] else None


def status() -> dict:
    """For the UI and the logs: available / draining / since when / why."""
    return {k: _state[k] for k in ("available", "draining", "busy", "busy_reason", "seen_at",
                                   "changed_at", "reason")} | {
        "configured": configured()}


def _set(available: bool, reason: str, *, draining: bool = False) -> None:
    if available != _state["available"] or draining != _state["draining"]:
        _state["changed_at"] = time.time()
        logger.info("[pc] %s (%s)", "available" if available
                    else ("draining - no new work" if draining else "unavailable"), reason)
    _state.update(available=available, draining=draining, reason=reason)
    if not available:
        _state["snapshot"] = None


def record(report: Optional[dict], error: str = "") -> None:
    """One poll's outcome: the agent's report, or None with what went wrong."""
    if report is not None and isinstance(report, dict):
        _state["misses"] = 0
        _state["seen_at"] = time.time()
        if report.get("shutting_down"):
            _set(False, "the PC is shutting down", draining=True)
            return
        _state["snapshot"] = report
        _set(True, "agent answered")
        return
    _state["misses"] += 1
    if _state["available"] and _state["misses"] >= MISSES_DOWN:
        _set(False, f"{_state['misses']} polls unanswered ({error or 'no answer'})")
    elif not _state["available"] and not _state["draining"]:
        _state["reason"] = error or "no answer"


def goodbye(reason: str = "shutdown") -> None:
    """The agent's shutdown notice: stop sending work to the PC now."""
    _set(False, f"goodbye from the PC ({reason})", draining=True)


async def poll_once(fetch=None) -> None:
    """One poll. ``fetch(url, token) -> dict`` is injectable for tests."""
    if not configured():
        return
    url = _settings().PC_AGENT_URL.rstrip("/") + "/status"
    try:
        report = await (fetch or _fetch)(url, token())
    except Exception as e:  # noqa: BLE001 - every failure is one missed poll
        record(None, f"{type(e).__name__}: {e}"[:200])
        return
    record(report)
    # Split install: is the PC gaming? Judged here, every poll, so a game
    # that starts takes the PC out of the routing within seconds.
    from src.services.ollama_hosts import split
    if available() and split():
        import asyncio
        from src.services.process_monitor import pc_busy
        try:
            value, why = await asyncio.to_thread(pc_busy, report)
        except Exception as e:  # noqa: BLE001 - fails closed: no verdict, no work for the PC
            logger.warning("[pc] busy check failed: %s", e)
            value, why = True, "busy check failed"
        set_busy(value, why)


async def _fetch(url: str, tok: str) -> dict:
    import httpx
    from src.pc_agent import TOKEN_HEADER
    from src.services.http_client import tls_context
    async with httpx.AsyncClient(verify=tls_context(), timeout=TIMEOUT_S) as client:
        r = await client.get(url, headers={TOKEN_HEADER: tok})
    r.raise_for_status()
    return r.json()
