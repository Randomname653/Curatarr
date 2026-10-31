"""Is this server's Ollama answering?

2026-10-29 the NAS's Ollama app went down at 19:10 and stayed down for 21
hours: embeddings, the summariser, lyrics and memory extraction failed,
each in a log line of its own, while chat kept working on the PC. Nobody
saw it until a memory extraction error surfaced. A cheap probe every 30 s
(the scheduler) keeps the answer here; the bell shows it (notifications),
the log says when it goes and when it comes back.

State lives in this process: after a restart it is judged afresh.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Failed probes in a row before the host counts as down: a busy Ollama can
# miss one 3 s probe while it loads a 19 GB model.
DOWN_AFTER = 3
_state: dict = {"fails": 0, "first_fail": None, "down_since": None}


def _answers(url: str, get=None) -> bool:
    import httpx
    try:
        return (get or httpx.get)(f"{url.rstrip('/')}/api/version", timeout=3).status_code == 200
    except Exception:  # noqa: BLE001 - not answering is the answer
        return False


def check(get=None, now: Optional[datetime] = None) -> None:
    """One probe of the main host (blocking: run it on a worker thread)."""
    from src.services.ollama_hosts import main_host
    now = now or datetime.now()
    url = main_host().url
    if _answers(url, get):
        if _state["down_since"]:
            logger.info("[hosts] Ollama at %s answers again (down since %s)",
                        url, _state["down_since"].strftime("%Y-%m-%d %H:%M"))
        _state.update(fails=0, first_fail=None, down_since=None)
        return
    _state["fails"] += 1
    _state["first_fail"] = _state["first_fail"] or now
    if _state["fails"] == DOWN_AFTER:
        _state["down_since"] = _state["first_fail"]
        logger.warning("[hosts] Ollama at %s is not answering (since %s): embeddings, the "
                       "summariser and memory extraction wait until it is back",
                       url, _state["first_fail"].strftime("%H:%M"))


def down_since() -> Optional[datetime]:
    return _state["down_since"]
