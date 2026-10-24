"""use_mmap per Ollama host (2026-10-24).

Measured on the NAS: Ollama loaded the 8B summariser without mmap and held
4.9 GB of anonymous memory next to the 6.1 GB on the card; reloaded with
use_mmap it held 0.45 GB (the rest became file pages the kernel can drop).
OLLAMA_USE_MMAP / PC_OLLAMA_USE_MMAP set it per host. A runner option that
differs between two calls reloads the model, so EVERY call to a host carries
that host's value - and none carries another host's.

    python tests/test_ollama_mmap.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import settings  # noqa: E402
from src.services import llm_utils as lu  # noqa: E402
from src.services import ollama_hosts as oh  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


KEYS = ("OLLAMA_USE_MMAP", "PC_OLLAMA_USE_MMAP", "OLLAMA_PC_ENDPOINT", "ENRICH_ON_PC")
saved = {k: getattr(settings, k) for k in KEYS}
saved_usable = oh.pc_usable


def mm(opts):
    return opts["options"].get("use_mmap", "unset")


try:
    settings.OLLAMA_PC_ENDPOINT = ""
    settings.OLLAMA_USE_MMAP = None
    check("unset: no call carries use_mmap (Ollama decides, as before)",
          mm(lu.ollama_options(0.1, 10)) == "unset" and mm(lu.curator_options(0.1, 10)) == "unset"
          and mm(lu.summarizer_options(0.1, 10)) == "unset")

    settings.OLLAMA_USE_MMAP = True
    check("OLLAMA_USE_MMAP: summariser, curator and fallback-chain calls all carry it",
          mm(lu.ollama_options(0.1, 10)) is True and mm(lu.curator_options(0.1, 10)) is True
          and mm(lu.summarizer_options(0.1, 10)) is True
          and mm(lu.options_for(settings.CURATOR_MODEL or settings.BASE_CURATOR_MODEL)) is True)
    check("...and the main host says so", oh.main_host().runner == {"use_mmap": True})
    settings.OLLAMA_USE_MMAP = False
    check("false is sent as false (an explicit choice, not 'unset')",
          mm(lu.ollama_options(0.1, 10)) is False)

    # ── split: each host its own value ──────────────────────────────────────
    settings.OLLAMA_USE_MMAP = True
    settings.PC_OLLAMA_USE_MMAP = None
    settings.OLLAMA_PC_ENDPOINT = "http://node:11434"
    settings.ENRICH_ON_PC = False
    oh.pc_usable = lambda: True
    pc = oh.pc_host()
    check("a curator call without a host goes where ollama_for('curator') goes: the node's options",
          mm(lu.curator_options(0.1, 10)) == "unset"
          and lu.curator_options(0.1, 10)["options"]["num_ctx"] == pc.num_ctx)
    check("...a summariser call goes where enrichment goes: the server's",
          mm(lu.ollama_options(0.1, 10)) is True)
    check("...an explicit host is honoured", mm(lu.curator_options(0.1, 10, host=oh.main_host())) is True
          and mm(lu.curator_options(0.1, 10, host=pc)) == "unset")
    settings.PC_OLLAMA_USE_MMAP = False
    check("PC_OLLAMA_USE_MMAP is the node's own", mm(lu.curator_options(0.1, 10, host=oh.pc_host())) is False
          and mm(lu.curator_options(0.1, 10)) is False)
    settings.ENRICH_ON_PC = True
    check("ENRICH_ON_PC: the summariser takes the node's value", mm(lu.ollama_options(0.1, 10)) is False)
    oh.pc_usable = lambda: False
    check("the node away: everything is the server's again", mm(lu.ollama_options(0.1, 10)) is True
          and mm(lu.curator_options(0.1, 10)) is True)
finally:
    for k, v in saved.items():
        setattr(settings, k, v)
    oh.pc_usable = saved_usable

src = {p: (ROOT / p).read_text(encoding="utf-8") for p in (
    "src/services/embed_service.py", "src/services/model_updates.py", "src/services/model_update_eval.py")}
check("embeddings carry the embed host's runner options",
      '"options": {**host_for("embed").runner, "num_gpu": 0, "num_ctx": num_ctx}' in src["src/services/embed_service.py"])
check("model updates measure on the server with the server's options, never the node's",
      src["src/services/model_updates.py"].count("host=_main()") == 2
      and "**main_host().runner" in src["src/services/model_update_eval.py"])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
