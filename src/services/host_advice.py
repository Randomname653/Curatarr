"""What the bench recommends for each Ollama host, next to what is set
(Settings > Integrations > Ollama & models, 2026-11-02).

The wizard knew the card it ran on; Settings afterwards showed free-text
model names and three numbers, with nothing to compare them against. Each
host's card is known without asking: this server's from nvidia-smi (the
same reading the Nodes view shows), the node's from its agent's report,
kept from its last report while it is away. With the card size, the bench's
package (model_catalog.hardware_package) says which curator to run there and
with which profile, window and layers, and recommend_models gives the
choices for every role, marked installed or not on that host.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def _installed(url: str) -> list:
    from src.services.setup_wizard import test_ollama
    try:
        tags = await test_ollama(url)
    except Exception as e:  # noqa: BLE001 - unreachable host: nothing marked installed
        logger.debug("[advice] tags of %s unreadable: %s", url, e)
        return []
    return sorted(tags.get("models") or []) if tags.get("ok") else []


def _advice(vram_gb: Optional[float], gpu_name: Optional[str], installed: list,
            ram_gb: Optional[float] = None) -> dict:
    from src.services.model_catalog import recommend_models
    rec = recommend_models(vram_gb, set(installed), gpu_name, ram_gb)
    return {**rec, "installed": installed}


async def server_card() -> dict:
    """This server's card: {"vram_gb", "name", "source"} (None values when
    nvidia-smi sees none - an Ollama on another machine)."""
    from src.services.setup_wizard import detect_gpu
    try:
        g = await detect_gpu()
    except Exception as e:  # noqa: BLE001 - no reading: the owner picks the size
        logger.debug("[advice] nvidia-smi failed: %s", e)
        g = {}
    if g.get("ok"):
        return {"vram_gb": g.get("vram_gb"), "name": g.get("name"), "source": "nvidia-smi"}
    return {"vram_gb": None, "name": None, "source": None}


def node_card() -> dict:
    """The node's card from its agent: now while it reports, else the last
    size it reported (pc_host keeps it across restarts)."""
    from src.services import pc_host
    snap = pc_host.snapshot() or {}
    gpu = snap.get("gpu")
    if isinstance(gpu, (list, tuple)) and len(gpu) >= 2 and gpu[1]:
        return {"vram_gb": round(float(gpu[1]) / 1024, 1), "name": None, "source": "agent"}
    mb = pc_host.last_vram_mb()
    if mb:
        return {"vram_gb": round(mb / 1024, 1), "name": None, "source": "agent, last report"}
    return {"vram_gb": None, "name": None, "source": None}


def _ram_gb() -> Optional[float]:
    from src.services.setup_wizard import _host_ram_gb
    return _host_ram_gb()


async def host_advice(server_vram_gb: Optional[float] = None,
                      node_vram_gb: Optional[float] = None) -> dict:
    """{"server": {...}, "node": {...} | None}. Each: the card, its source,
    the values set now, and the bench's advice for that card. A size passed
    in overrides the detected one (the owner's pick when nothing reads it)."""
    from src.services import ollama_hosts, pc_host
    from src.services.setup_wizard import current_env_config

    # What is set now, as the integrations panel reads it (one source).
    cfg = current_env_config()
    main = ollama_hosts.main_host()
    card = await server_card()
    if server_vram_gb:
        card = {**card, "vram_gb": server_vram_gb, "source": "picked"}
    pc = ollama_hosts.pc_host()
    installed_main, installed_pc = await asyncio.gather(
        _installed(main.url), _installed(pc.url) if pc else asyncio.sleep(0, result=[]))
    out = {"server": {
        "card": card,
        "current": {"curator": cfg["base_curator_model"],
                    "summarizer": cfg["base_summarizer_model"],
                    "embedding": cfg["embedding_model"],
                    "pitcher": cfg["base_pitcher_model"],
                    "profile": cfg["llm_profile"],
                    "num_ctx": cfg["curator_num_ctx"],
                    "num_gpu": cfg["curator_num_gpu"]},
        "advice": _advice(card["vram_gb"], card["name"], installed_main, _ram_gb()),
    }, "node": None}
    if pc is not None:
        ncard = node_card()
        if node_vram_gb:
            ncard = {**ncard, "vram_gb": node_vram_gb, "source": "picked"}
        out["node"] = {
            "name": pc_host.node_name(cap=True),
            "available": pc_host.available(),
            "card": ncard,
            "current": {"curator": cfg["pc_base_curator_model"],
                        "summarizer": cfg["pc_base_summarizer_model"],
                        "pitcher": cfg["pc_base_pitcher_model"],
                        "profile": cfg["pc_llm_profile"],
                        "num_ctx": cfg["pc_curator_num_ctx"],
                        "num_gpu": cfg["pc_curator_num_gpu"]},
            "advice": _advice(ncard["vram_gb"], ncard["name"], installed_pc),
        }
    return out
