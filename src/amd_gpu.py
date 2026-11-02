"""AMD graphics cards, read where nvidia-smi is missing (2026-11-02).

Every card reading in Curatarr asked nvidia-smi: the setup's card
detection, Settings' bench advice, the Nodes view, the "another program
holds the card" gate and the PC agent. On an AMD card all of them read
nothing, and the owner had to type the card size in.

Two sources, no extra install:

* Linux: the amdgpu driver publishes the card's memory and load in sysfs
  (/sys/class/drm/cardN/device/mem_info_vram_total, mem_info_vram_used,
  gpu_busy_percent). A container sees them too when /sys is the host's.
* amd-smi (ROCm 6+), where it is installed: ``amd-smi metric --json`` and
  ``amd-smi static --json``. Its JSON changed between releases (plain
  numbers, "24560 MB" strings, {"value", "unit"} objects, a top-level list
  or {"gpu_data": [...]}); all of them are read.

Untested on real AMD hardware: the parsers are checked against recorded
output shapes (tests/test_amd_gpu.py). Process lists per card (which
program holds it) stay NVIDIA-only. No vendor library is imported, so the
PC agent can use this module on its own.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Optional

SYSFS = Path("/sys/class/drm")
AMD_VENDOR = "0x1002"
_CARD = re.compile(r"^card\d+$")
_UNITS = {"b": 1 / 2**20, "kb": 1 / 1024, "kib": 1 / 1024, "mb": 1.0, "mib": 1.0,
          "gb": 1024.0, "gib": 1024.0}


def _read(p: Path) -> Optional[str]:
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _int(s) -> Optional[int]:
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return None


def sysfs_cards(root: Path = SYSFS) -> list:
    """[{"name", "total_mb", "used_mb", "util"}] for every amdgpu card."""
    out = []
    try:
        entries = sorted(root.iterdir()) if root.is_dir() else []
    except OSError:
        return out
    for card in entries:
        if not _CARD.match(card.name):
            continue           # card0-DP-1 and friends are connectors
        dev = card / "device"
        if (_read(dev / "vendor") or "").lower() != AMD_VENDOR:
            continue
        total = _int(_read(dev / "mem_info_vram_total"))
        if not total:
            continue
        used = _int(_read(dev / "mem_info_vram_used"))
        name = _read(dev / "product_name") or f"AMD Radeon ({card.name})"
        out.append({"name": name, "total_mb": round(total / 2**20),
                    "used_mb": round(used / 2**20) if used is not None else None,
                    "util": _int(_read(dev / "gpu_busy_percent"))})
    return out


def _mb(v) -> Optional[float]:
    """MB from an amd-smi field: 24560, "24560 MB", {"value": 24560, "unit": "MB"}."""
    if isinstance(v, dict):
        unit = str(v.get("unit") or "MB")
        v = f"{v.get('value')} {unit}"
    if isinstance(v, (int, float)):
        return float(v)
    m = re.match(r"^\s*([\d.]+)\s*([A-Za-z]*)\s*$", str(v or ""))
    if not m:
        return None
    factor = _UNITS.get((m.group(2) or "mb").lower())
    return float(m.group(1)) * factor if factor else None


def _pct(v) -> Optional[int]:
    if isinstance(v, dict):
        v = v.get("value")
    m = re.match(r"^\s*([\d.]+)", str(v if v is not None else ""))
    return round(float(m.group(1))) if m else None


def _gpus(data) -> list:
    if isinstance(data, dict):
        data = data.get("gpu_data", [data])
    return [g for g in (data or []) if isinstance(g, dict)]


def parse_amd_smi(metric, static=None) -> list:
    """amd-smi's ``metric`` (and, for names, ``static``) JSON, decoded, as
    [{"name", "total_mb", "used_mb", "util"}]."""
    names = {}
    for g in _gpus(static):
        asic = g.get("asic") or {}
        names[g.get("gpu")] = asic.get("market_name") or asic.get("product_name")
    out = []
    for g in _gpus(metric):
        mem = g.get("mem_usage") or {}
        total = _mb(mem.get("total_vram"))
        if not total:
            continue
        used = _mb(mem.get("used_vram"))
        usage = g.get("usage") or {}
        out.append({"name": names.get(g.get("gpu")) or f"AMD GPU {g.get('gpu', 0)}",
                    "total_mb": round(total), "used_mb": round(used) if used is not None else None,
                    "util": _pct(usage.get("gfx_activity", usage.get("gfx_usage")))})
    return out


def _run_json(args: list):
    try:
        from src.winproc import NO_WINDOW
    except ImportError:
        NO_WINDOW = {}
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=4, **NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def amd_smi_cards() -> list:
    metric = _run_json(["amd-smi", "metric", "--usage", "--mem-usage", "--json"])
    if metric is None:
        return []
    return parse_amd_smi(metric, _run_json(["amd-smi", "static", "--asic", "--json"]))


def cards() -> list:
    """Every AMD card this machine shows, from sysfs, else from amd-smi."""
    return sysfs_cards() or amd_smi_cards()


def usage() -> Optional[tuple]:
    """(used MB, total MB, utilization %) of the largest AMD card - the
    shape nvidia-smi's reading has everywhere - or None."""
    found = cards()
    if not found:
        return None
    top = max(found, key=lambda c: c["total_mb"])
    return (top["used_mb"], top["total_mb"], top["util"])
