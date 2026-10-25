"""What each card is doing right now (2026-10-25, ROADMAP "Two cards,
visible").

One read for the NAS card and the PC's card: the lane's holder and queue
(llm_priority, in memory), the PC's state from its agent's last report
(free / busy / which game / going away / away) and its VRAM, and on the
NAS the container's own memory. Until now that was spread over the log,
the lane badge and Activity.

Pure reads, nothing here decides routing."""
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_MB = 1024 * 1024
# nvidia-smi is a subprocess; the view polls every few seconds.
_SMI_TTL_S = 10.0
_smi_cache: dict = {}
_CGROUP = Path("/sys/fs/cgroup")


def lane_view(url: Optional[str]) -> dict:
    """Holder and queue of the lane at ``url``. A lane nobody used yet has
    no holder and no queue."""
    from src.services import llm_priority as lp
    ln = lp._lanes.get(lp._key(url or "")) if url else None
    if ln is None:
        return {"holder": "", "active": 0, "queued": 0, "queued_interactive": 0, "warm": False}
    return {"holder": ln.owner if ln.active else "", "active": ln.active,
            "queued": ln.gate.waiting(), "queued_interactive": ln.gate.waiting(lp.INTERACTIVE),
            "warm": bool(ln.warm)}


def vram(smi) -> Optional[dict]:
    """(used MB, total MB, util %) as nvidia-smi reads it, else None."""
    if isinstance(smi, (list, tuple)) and len(smi) == 3 and smi[0] is not None and smi[1]:
        return {"used_mb": int(smi[0]), "total_mb": int(smi[1]),
                "util": int(smi[2]) if smi[2] is not None else None}
    return None


def _local_smi(now: Optional[float] = None, reader=None):
    now = time.monotonic() if now is None else now
    if _smi_cache and now - _smi_cache["at"] < _SMI_TTL_S:
        return _smi_cache["v"]
    if reader is None:
        from src.services.process_monitor import _nvidia_smi_read as reader
    v = reader()
    _smi_cache.update(at=now, v=v)
    return v


def _read_int(p: Path) -> Optional[int]:
    try:
        s = p.read_text().strip()
    except OSError:
        return None
    return int(s) if s.isdigit() else None   # cgroup v2 writes "max" for no limit


def container_memory(root: Path = _CGROUP, proc=None, free_mb=None) -> dict:
    """This process's RSS, the container's use and limit (cgroup v2, else
    v1; None outside a container or without a limit) and the RAM the host
    has free."""
    out = {"rss_mb": None, "used_mb": None, "limit_mb": None, "free_mb": None}
    try:
        if proc is None:
            import psutil
            proc = psutil.Process()
        out["rss_mb"] = int(proc.memory_info().rss / _MB)
    except Exception as e:  # noqa: BLE001 - a missing reading is shown as unknown
        logger.debug("[cards] rss unreadable: %s", e)
    used = _read_int(root / "memory.current")
    limit = _read_int(root / "memory.max")
    if used is None:
        used = _read_int(root / "memory" / "memory.usage_in_bytes")
        limit = _read_int(root / "memory" / "memory.limit_in_bytes")
        if limit is not None and limit >= 1 << 60:   # v1 "no limit" is a huge number
            limit = None
    out["used_mb"] = int(used / _MB) if used is not None else None
    out["limit_mb"] = int(limit / _MB) if limit is not None else None
    if free_mb is None:
        from src.services.llm_lane import ram_free_mb as free_mb
    out["free_mb"] = free_mb()
    return out


def pc_state(st: dict, snapshot: Optional[dict], targets=()) -> dict:
    """free / busy / going away / away, with the game when one runs."""
    if not st.get("available"):
        state = "away"
    elif st.get("draining"):
        state = "going away"
    elif st.get("busy"):
        state = "busy"
    else:
        state = "free"
    game = None
    if state in ("busy", "free"):
        game = next((p.get("name") for p in (snapshot or {}).get("processes") or []
                     if (p.get("name") or "").lower() in targets), None)
    return {"state": state, "reason": st.get("busy_reason") or st.get("reason") or "",
            "game": game, "since": st.get("changed_at"), "seen_at": st.get("seen_at")}


def cards() -> dict:
    """{"nas": card, "pc": card or None}; blocking (nvidia-smi, cgroup files)."""
    from src.services import ollama_hosts
    from src.services.app_state import get_state
    nas_url = ollama_hosts.main_host().url
    nas = {"name": "NAS" if ollama_hosts.split() else "This machine",
           "lane": lane_view(nas_url),
           "mode": get_state("llm_lane") or "free", "mode_reason": get_state("llm_lane_reason") or "",
           "vram": vram(_local_smi()), "memory": container_memory()}
    pc = None
    pch = ollama_hosts.pc_host()
    if pch is not None:
        from src.services import pc_host
        from src.services.process_monitor import _game_targets
        snap = pc_host.snapshot() or {}
        st = pc_host.status()
        pc = {"name": st.get("name") or "PC", "lane": lane_view(pch.url),
              **pc_state(st, snap, _game_targets()),
              "vram": vram(snap.get("gpu")) if st.get("available") else None}
    return {"nas": nas, "pc": pc}
