"""Settings > Integrations > Ollama & models: the bench's advice per host
(2026-11-02).

The card showed free-text model names and three numbers with nothing to
compare them against, while the app knows each card. Now:

- GET /api/setup/hosts-advice gives each host's card (this server's from
  nvidia-smi, the node's from its agent, kept from its last report while it
  is away), the values set now and the bench's advice for that card;
- the node's own bases and profile (PC_*) are written and changeable, and
  Rebuild models can bake on the node while it is free;
- the card renders "now / bench" with a badge where they differ.

    python tests/test_host_advice.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio
import json
import pathlib
import re
import shutil
import subprocess
import tempfile

conn = app_db()
from fastapi import BackgroundTasks, HTTPException  # noqa: E402

from src.config import settings  # noqa: E402
from src.services import host_advice as ha  # noqa: E402
from src.services import pc_host  # noqa: E402
from src.services import setup_wizard as sw  # noqa: E402
import src.routers.setup as st  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEYS = ("OLLAMA_PC_ENDPOINT", "PC_AGENT_URL", "PC_AGENT_TOKEN", "BASE_CURATOR_MODEL",
        "LLM_PROFILE", "CURATOR_NUM_CTX", "CURATOR_NUM_GPU", "PC_BASE_CURATOR_MODEL",
        "PC_LLM_PROFILE")
saved = {k: getattr(settings, k) for k in KEYS}
saved_fns = (sw.detect_gpu, sw.test_ollama, sw._host_ram_gb)
saved_state, saved_card = dict(pc_host._state), dict(pc_host._card)
tags = {}


async def fake_gpu():
    return {"ok": True, "vram_gb": 16.0, "name": "NVIDIA RTX PRO 2000 Blackwell", "gpus": []}


async def fake_tags(url):
    return {"ok": True, "models": tags.get(url, [])}


try:
    sw.detect_gpu, sw.test_ollama, sw._host_ram_gb = fake_gpu, fake_tags, lambda: 64.0
    settings.OLLAMA_PC_ENDPOINT = ""
    settings.BASE_CURATOR_MODEL = "gemma4:26b-a4b-it-qat"
    settings.LLM_PROFILE, settings.CURATOR_NUM_CTX, settings.CURATOR_NUM_GPU = "large", 0, -1
    tags[settings.effective_ollama] = ["gemma4:26b-a4b-it-qat", "curatarr-curator:latest"]
    d = asyncio.run(ha.host_advice())
    srv = d["server"]
    check("a single host: the server's card from nvidia-smi, no node",
          srv["card"] == {"vram_gb": 16.0, "name": "NVIDIA RTX PRO 2000 Blackwell",
                          "source": "nvidia-smi"} and d["node"] is None, d)
    check("...the values set now", srv["current"]["curator"] == "gemma4:26b-a4b-it-qat"
          and (srv["current"]["profile"], srv["current"]["num_ctx"], srv["current"]["num_gpu"])
          == ("large", 0, -1))
    pkg = srv["advice"]["package"]
    if pkg:
        check("...the bench's package for 16 GB, with each measured model's profile",
              pkg["tier_gb"] == 16 and pkg["recommended"]["model"] in pkg["by_model"], pkg)
    check("...and the choices per role, marked installed on that host",
          any(e["model"] == "gemma4:26b-a4b-it-qat" and e["installed"]
              for e in srv["advice"]["curator"])
          and srv["advice"]["installed"] == ["curatarr-curator:latest", "gemma4:26b-a4b-it-qat"])
    d = asyncio.run(ha.host_advice(server_vram_gb=24))
    check("a size passed in overrides the reading", d["server"]["card"]["vram_gb"] == 24
          and d["server"]["card"]["source"] == "picked")

    async def no_gpu():
        return {"ok": False, "error": "nvidia-smi not found"}
    sw.detect_gpu = no_gpu
    d = asyncio.run(ha.host_advice())
    check("no nvidia-smi: the card is unknown, the advice waits for a size",
          d["server"]["card"]["vram_gb"] is None and d["server"]["advice"]["package"] is None)
    sw.detect_gpu = fake_gpu

    # ── the node: its card from the agent, kept while it is away ───────────
    settings.OLLAMA_PC_ENDPOINT = "http://pc:11434"
    settings.PC_BASE_CURATOR_MODEL, settings.PC_LLM_PROFILE = "gemma4:31b", "large"
    pc_host._state.update(available=True, snapshot={"gpu": [3000.0, 24564.0, 5.0]})
    d = asyncio.run(ha.host_advice())
    check("the node reporting: its card from the agent, its own values",
          d["node"]["card"] == {"vram_gb": 24.0, "name": None, "source": "agent"}
          and d["node"]["current"]["curator"] == "gemma4:31b", d["node"])
    pc_host._card.update(vram_mb=0, stored=0, loaded=False)
    asyncio.run(pc_host._load_card())
    asyncio.run(pc_host._note_card({"gpu": [3000.0, 24564.0, 5.0]}))
    pc_host._state.update(available=False, snapshot=None)
    pc_host._card.update(vram_mb=0, stored=0, loaded=False)
    asyncio.run(pc_host._load_card())
    d = asyncio.run(ha.host_advice())
    check("the node away, the server restarted: the size it last reported",
          d["node"]["card"]["vram_gb"] == 24.0 and d["node"]["card"]["source"] == "agent, last report",
          d["node"]["card"])
    asyncio.run(pc_host._note_card({"gpu": "missing"}))
    check("...a report without a reading keeps it", pc_host.last_vram_mb() == 24564)

    # ── the node's values are written, validated, and need a rebuild ────────
    tmp = pathlib.Path(tempfile.mkdtemp()) / ".env"
    old_path = sw.ENV_PATH
    sw.ENV_PATH = tmp
    try:
        sw.write_env(sw.merge_env_config(sw.current_env_config(), {
            "pc_base_curator_model": "gemma4:31b-it-qat", "pc_llm_profile": "small",
            "pc_curator_num_ctx": 12288, "pc_curator_num_gpu": -1}))
        written = {}
        with open(tmp, encoding="utf-8") as fh:
            for line in fh:
                k, _, v = line.strip().partition("=")
                written.setdefault(k, []).append(v)
        check("the node's bases and profile reach the .env, once each",
              {k: written.get(k) for k in ("PC_BASE_CURATOR_MODEL", "PC_LLM_PROFILE",
                                           "PC_CURATOR_NUM_CTX", "PC_CURATOR_NUM_GPU")}
              == {"PC_BASE_CURATOR_MODEL": ["gemma4:31b-it-qat"], "PC_LLM_PROFILE": ["small"],
                  "PC_CURATOR_NUM_CTX": ["12288"], "PC_CURATOR_NUM_GPU": ["-1"]}, written)
    finally:
        sw.ENV_PATH = old_path
    for bad in ({"pc_llm_profile": "medium"}, {"pc_curator_num_ctx": 1000},
                {"pc_curator_num_gpu": -5}):
        try:
            st.ReconfigureRequest(**bad)
            ok = False
        except ValueError:
            ok = True
        check(f"the node's values are validated like the server's: {bad}", ok)
    check("a change to the node's package asks for a rebuild",
          {"pc_base_curator_model", "pc_llm_profile", "pc_curator_num_ctx"} <= set(st._MODEL_KEYS))

    try:
        asyncio.run(st.build_models_endpoint(BackgroundTasks(), host="pc", _gate=None))
        refused = None
    except HTTPException as e:
        refused = e.status_code
    check("Rebuild models on the node while it is away: refused, not queued", refused == 409)
    try:
        asyncio.run(st.hosts_advice(server_vram_gb=0.2, _admin=None))
        bad_size = None
    except HTTPException as e:
        bad_size = e.status_code
    check("an implausible card size is refused", bad_size == 422)
finally:
    for k, v in saved.items():
        setattr(settings, k, v)
    sw.detect_gpu, sw.test_ollama, sw._host_ram_gb = saved_fns
    pc_host._state.clear()
    pc_host._state.update(saved_state)
    pc_host._card.clear()
    pc_host._card.update(saved_card)


# ── the card: now / bench, a badge where they differ ────────────────────────
def _fn(src: str, name: str) -> str:
    m = re.search(r"^(?:export )?function %s\(" % re.escape(name), src, re.M)
    end = re.compile(r"^\}$", re.M).search(src, m.start())
    return src[m.start():end.end()].removeprefix("export ")


def _const(src: str, name: str) -> str:
    return re.search(r"^(?:export )?const %s = .*?;$" % re.escape(name), src, re.M).group(0)


node = shutil.which("node")
if node:
    js_src = (ROOT / "frontend/js/host_advice.js").read_text(encoding="utf-8")
    part = {"card": {"vram_gb": 16, "name": "RTX PRO 2000", "source": "nvidia-smi"},
            "current": {"curator": "gemma4:31b", "profile": "large"},
            "advice": {"package": {"tier_gb": 16, "recommended": {
                "model": "gemma4:26b-a4b-it-qat", "profile": "large", "num_ctx": 16384, "num_gpu": -1,
                "expected": {"gen_tok_s": 61, "judge_s": 4.3}, "quality": {"score25": 21.9},
                "server": {"OLLAMA_KV_CACHE_TYPE": "q8_0"}},
                "by_model": {"gemma4:26b-a4b-it-qat": {"profile": "large", "num_ctx": 16384, "num_gpu": -1},
                             "gemma4:e4b-it-qat": {"profile": "small", "num_ctx": 8192, "num_gpu": 99}}}}}
    js = ("const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;');\n"
          "const act = (n, ...a) => `data-action=\"${n}\"`;\nconst actOn = (e, n, ...a) => `data-on-${e}=\"${n}\"`;\n"
          + "\n".join(_const(js_src, n) for n in ("SIZES", "PROFILE_CTX", "_window"))
          + "\n" + "\n".join(_fn(js_src, n) for n in ("_measured", "_row", "_adviceHtml"))
          + f"\nconst part = {json.dumps(part)};\n"
          + "const strip = h => h.replace(/<[^>]+>/g, ' ').replace(/\\s+/g, ' ');\n"
          + "console.log(JSON.stringify({"
          + "unmeasured: strip(_adviceHtml('server', part, {curator: 'gemma4:31b', profile: 'large', num_ctx: '0', num_gpu: '99'})),"
          + "e4b: strip(_adviceHtml('server', part, {curator: 'gemma4:e4b-it-qat', profile: 'large', num_ctx: '0', num_gpu: '-1'})),"
          + "nosize: strip(_adviceHtml('server', {...part, card: {}, advice: {package: null}}, {}))}));")
    r = subprocess.run([node, "-e", js], capture_output=True, text=True, encoding="utf-8")
    out = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {}
    if r.returncode:
        print(r.stderr[:600])
    check("the card names this server's card and its source",
          "RTX PRO 2000 · 16 GB (nvidia-smi)" in out.get("unmeasured", ""), out)
    check("a curator other than the bench's pick is marked, and an unmeasured one gets no profile badges",
          "bench: gemma4:26b-a4b-it-qat" in out.get("unmeasured", "")
          and "as measured" not in out.get("unmeasured", "")
          and "has not measured gemma4:31b on 16 GB" in out.get("unmeasured", ""))
    check("a measured curator: its own values, each mismatch marked",
          "Profile: large bench: small" in out.get("e4b", "")
          and "Context window: 16384 bench: 8192" in out.get("e4b", "")
          and "Layers on the card: -1 bench: 99" in out.get("e4b", ""), out.get("e4b"))
    check("...with the speed it ran at and the Ollama server's own settings",
          "61 tok/s" in out.get("e4b", "") and "OLLAMA_KV_CACHE_TYPE=q8_0" in out.get("e4b", ""))
    check("no card read: it asks for the size instead of advising",
          "pick the card's memory" in out.get("nosize", ""), out.get("nosize"))
else:
    print("  (node not installed - the card's rendering is checked in the browser)")

check.done()
