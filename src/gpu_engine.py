"""Which programs draw on the graphics card's 3D engine (2026-10-23).

What Task Manager's "GPU" column shows: the Windows "GPU Engine" performance
counters, one instance per process and engine, read through pdh.dll with
ctypes (no extra dependency). The PC agent reports the 3D share per program,
so Curatarr on the NAS sees a game the moment it renders, even one nobody has
classified yet - the GPU-pressure gate alone needs 45 s of a full card.

Only the 3D engine counts: a game renders there. Ollama's runner shows up
on it too (llama-server.exe at 97-100 % while generating, measured on the
4090 2026-10-23), so the agent reports it like any program and Curatarr
leaves its own runners out by name. Elsewhere than Windows, or when the
counters are not there, ``sample()`` returns [] and nothing new is reported.

One query stays open; each ``sample()`` collects once, so the figure is the
average since the previous sample (the NAS polls every 5 s). The first call
only primes the query and returns [].
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)

COUNTER = "\\GPU Engine(*)\\Utilization Percentage"
MIN_PCT = 1.0          # below this a program is not drawing, not worth a line

_PDH_FMT_DOUBLE = 0x00000200
_PDH_MORE_DATA = 0x800007D2


def parse_instance(name: str) -> "tuple | None":
    """(pid, engine type) from an instance name like
    ``pid_1234_luid_0x0_0x1_phys_0_eng_0_engtype_3D``, else None."""
    if not name.startswith("pid_") or "_engtype_" not in name:
        return None
    pid_part = name[4:].split("_", 1)[0]
    if not pid_part.isdigit():
        return None
    return int(pid_part), name.rsplit("_engtype_", 1)[1]


def per_process(values, names_by_pid) -> list[dict]:
    """[{"name", "pct"}] for the 3D engine, one line per program (its
    processes summed, capped at 100), largest first."""
    by_pid: dict = {}
    for inst, pct in values:
        parsed = parse_instance(inst)
        if not parsed or parsed[1].lower() != "3d" or pct is None:
            continue
        by_pid[parsed[0]] = by_pid.get(parsed[0], 0.0) + float(pct)
    by_name: dict = {}
    for pid, pct in by_pid.items():
        name = names_by_pid(pid)
        if name:
            by_name[name] = min(100.0, by_name.get(name, 0.0) + pct)
    return [{"name": n, "pct": round(p, 1)}
            for n, p in sorted(by_name.items(), key=lambda kv: -kv[1]) if p >= MIN_PCT]


def _process_name(pid: int) -> Optional[str]:
    import psutil
    try:
        return psutil.Process(pid).name()
    except (psutil.Error, OSError):
        return None


class Sampler:
    """An open PDH query over every GPU engine instance."""

    def __init__(self):
        self._lock = threading.Lock()
        self._query = None
        self._counter = None
        self._broken = sys.platform != "win32"

    def _open(self) -> bool:
        import ctypes
        from ctypes import wintypes
        pdh = ctypes.WinDLL("pdh")
        query, counter = wintypes.HANDLE(), wintypes.HANDLE()
        if pdh.PdhOpenQueryW(None, None, ctypes.byref(query)) != 0:
            return False
        if pdh.PdhAddEnglishCounterW(query, COUNTER, None, ctypes.byref(counter)) != 0:
            pdh.PdhCloseQuery(query)
            return False
        pdh.PdhCollectQueryData(query)          # primes the rate
        self._pdh, self._query, self._counter = pdh, query, counter
        return True

    def _read(self) -> list:
        import ctypes
        from ctypes import wintypes

        class _Value(ctypes.Structure):
            _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]

        class _Item(ctypes.Structure):
            _fields_ = [("szName", wintypes.LPWSTR), ("FmtValue", _Value)]

        pdh = self._pdh
        if pdh.PdhCollectQueryData(self._query) != 0:
            return []
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        r = pdh.PdhGetFormattedCounterArrayW(self._counter, _PDH_FMT_DOUBLE, ctypes.byref(size),
                                             ctypes.byref(count), None)
        if (r & 0xFFFFFFFF) != _PDH_MORE_DATA or not size.value:
            return []
        buf = (ctypes.c_byte * size.value)()
        r = pdh.PdhGetFormattedCounterArrayW(self._counter, _PDH_FMT_DOUBLE, ctypes.byref(size),
                                             ctypes.byref(count), buf)
        if r != 0:
            return []
        items = ctypes.cast(buf, ctypes.POINTER(_Item))
        # CStatus 0 / 1 = PDH_CSTATUS_VALID_DATA / NEW_DATA; others are stale.
        return [(items[i].szName, items[i].FmtValue.doubleValue)
                for i in range(count.value) if items[i].FmtValue.CStatus in (0, 1)]

    def sample(self) -> list[dict]:
        """The 3D share per program since the last sample; [] when unknown."""
        if self._broken:
            return []
        with self._lock:
            try:
                if self._query is None:
                    if not self._open():
                        self._broken = True
                        logger.info("[agent] no GPU engine counters here - per-program GPU not reported")
                    return []
                return per_process(self._read(), _process_name)
            except Exception as e:  # noqa: BLE001 - a counter failure must not stop the agent
                logger.warning("[agent] GPU engine counters failed: %s", e)
                return []
