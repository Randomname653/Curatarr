"""AMD cards where nvidia-smi is missing (2026-11-02).

Every card reading asked nvidia-smi only, so on an AMD card the setup's
detection, Settings' bench advice, the Nodes view, the "another program
holds the card" gate and the PC agent all read nothing. src/amd_gpu.py
reads the amdgpu driver's sysfs files, else amd-smi. No AMD card here: the
shapes below are the drivers' and amd-smi's documented outputs.

    python tests/test_amd_gpu.py
"""
from _harness import check  # noqa: I001 - first: temp stores

import asyncio
import subprocess
import tempfile
from pathlib import Path

from src import amd_gpu  # noqa: E402
from src import pc_agent  # noqa: E402
from src.services import host_advice as ha  # noqa: E402
from src.services import process_monitor as pm  # noqa: E402
from src.services import setup_wizard as sw  # noqa: E402


def sysfs(cards: dict) -> Path:
    root = Path(tempfile.mkdtemp())
    for name, files in cards.items():
        dev = root / name / "device"
        dev.mkdir(parents=True)
        for f, v in files.items():
            (dev / f).write_text(f"{v}\n", encoding="utf-8")
    return root


GIB = 2 ** 30
root = sysfs({
    "card0": {"vendor": "0x1002", "mem_info_vram_total": 16 * GIB, "mem_info_vram_used": 1 * GIB,
              "gpu_busy_percent": 5, "product_name": "AMD Radeon RX 7800 XT"},
    "card0-DP-1": {"vendor": "0x1002", "mem_info_vram_total": 16 * GIB},
    "card1": {"vendor": "0x8086", "mem_info_vram_total": 0},
    "card2": {"vendor": "0x1002", "mem_info_vram_total": 24 * GIB, "mem_info_vram_used": 2 * GIB},
})
found = amd_gpu.sysfs_cards(root)
check("sysfs: the amdgpu cards, not their connectors or another vendor's card",
      [c["total_mb"] for c in found] == [16384, 24576], found)
check("...with name, memory in use and load",
      found[0] == {"name": "AMD Radeon RX 7800 XT", "total_mb": 16384, "used_mb": 1024, "util": 5})
check("...and a card without a product name or load still counts",
      found[1]["name"] == "AMD Radeon (card2)" and found[1]["util"] is None)
check("sysfs: no /sys/class/drm (Windows, a container without it) reads nothing",
      amd_gpu.sysfs_cards(Path(tempfile.mkdtemp()) / "missing") == [])

# amd-smi: three JSON shapes from different ROCm releases.
new = [{"gpu": 0, "usage": {"gfx_activity": {"value": 7, "unit": "%"}},
        "mem_usage": {"total_vram": {"value": 24560, "unit": "MB"},
                      "used_vram": {"value": 512, "unit": "MB"}}}]
static = [{"gpu": 0, "asic": {"market_name": "AMD Radeon RX 7900 XTX"}}]
old = {"gpu_data": [{"gpu": 0, "usage": {"gfx_usage": "12 %"},
                     "mem_usage": {"total_vram": "16368 MB", "used_vram": "300 MB"}}]}
plain = [{"gpu": 1, "mem_usage": {"total_vram": 8 * GIB, "used_vram": 0}}]
check.table("amd-smi: every release's shape", [
    ("value/unit objects, named by static",
     (new, static), [{"name": "AMD Radeon RX 7900 XTX", "total_mb": 24560, "used_mb": 512, "util": 7}]),
    ("gpu_data wrapper, '16368 MB' strings",
     (old, None), [{"name": "AMD GPU 0", "total_mb": 16368, "used_mb": 300, "util": 12}]),
    ("plain numbers are MB", (plain, None),
     [{"name": "AMD GPU 1", "total_mb": 8 * GIB, "used_mb": 0, "util": None}]),
    ("no memory figure: no card", ([{"gpu": 0, "mem_usage": {}}], None), []),
], lambda args: amd_gpu.parse_amd_smi(*args))
check("amd-smi: bytes and GiB convert to MB",
      amd_gpu._mb({"value": 2 * GIB, "unit": "B"}) == 2048 and amd_gpu._mb("1.5 GiB") == 1536)

saved = (amd_gpu.cards, subprocess.run, sw._host_ram_gb)
try:
    amd_gpu.cards = lambda: [{"name": "AMD Radeon RX 7800 XT", "total_mb": 16384, "used_mb": 1024, "util": 5},
                             {"name": "iGPU", "total_mb": 512, "used_mb": 100, "util": 0}]
    check("usage: the largest card, in nvidia-smi's (used, total, util) shape",
          amd_gpu.usage() == (1024, 16384, 5))

    def no_nvidia(*a, **k):
        raise FileNotFoundError("nvidia-smi")
    subprocess.run = no_nvidia
    check("the card gate and the Nodes view read the AMD card without nvidia-smi",
          pm._nvidia_smi_read() == (1024.0, 16384.0, 5.0))
    check("...and so does the PC agent", pc_agent._gpu_reading() == [1024.0, 16384.0, 5.0])
    sw._host_ram_gb = lambda: 32.0
    g = asyncio.run(sw.detect_gpu())
    check("the setup finds the AMD card, and says the bench measured NVIDIA only",
          g["ok"] and g["vendor"] == "amd" and g["vram_gb"] == 16.0
          and g["name"] == "AMD Radeon RX 7800 XT" and g["gpus"][0]["free_gb"] == 15.0
          and "NVIDIA cards only" in g["busy_note"], g)
    c = asyncio.run(ha.server_card())
    check("Settings names it an AMD card", c["vram_gb"] == 16.0 and c["source"].startswith("AMD card"))

    amd_gpu.cards = lambda: []
    check("neither vendor: the gate reports nvidia-smi missing, as before",
          pm._nvidia_smi_read() == pm.SMI_MISSING)
    check("...the agent too", pc_agent._gpu_reading() == "missing")
    g = asyncio.run(sw.detect_gpu())
    check("...and the setup asks for the size by hand", not g["ok"] and "AMD" in g["error"])
finally:
    amd_gpu.cards, subprocess.run, sw._host_ram_gb = saved

check.done()
