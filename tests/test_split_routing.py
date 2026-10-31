"""Routing on a split install: the NAS and the gaming PC (2026-10-22, NAS
move phase 2, PR 4a).

    role       PC on and free     PC off, asleep, shutting down or gaming
    chat       PC                 NAS (OLLAMA_ENDPOINT)
    curator    PC                 NAS
    judge      PC                 PcUnavailable - deletion runs wait
    enrich     NAS, always (ENRICH_ON_PC=true: PC when free)
    embed      NAS, always

The PC is used only while its agent reports it free; a game there (judged
every poll from the PC's report) takes it out within seconds and evicts what
Curatarr left on its card. The NAS's own game flag reads the NAS. Without
OLLAMA_PC_ENDPOINT nothing changes.

    python tests/test_split_routing.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import settings  # noqa: E402
from src.services import ollama_hosts as oh  # noqa: E402
from src.services import pc_host  # noqa: E402
from src.services import process_monitor as pm  # noqa: E402

from _checks import check  # noqa: E402


TOKEN = "t" * 24
KEYS = ("OLLAMA_PC_ENDPOINT", "PC_LLM_PROFILE", "PC_CURATOR_NUM_CTX", "PC_CURATOR_NUM_GPU",
        "PC_AGENT_URL", "PC_AGENT_TOKEN")
saved = {k: getattr(settings, k) for k in KEYS}
main = settings.effective_ollama


def raises_pc_unavailable(role):
    try:
        oh.host_for(role)
        return False
    except oh.PcUnavailable:
        return True


def poll(report):
    async def fetch(url, tok):
        if isinstance(report, Exception):
            raise report
        return report
    asyncio.run(pc_host.poll_once(fetch))


QUIET = {"v": 1, "processes": [{"name": "chrome.exe", "rss_mb": 900}],
         "gpu": [3000.0, 24564.0, 5.0], "shutting_down": False}

try:
    # ── not split: everything as before ─────────────────────────────────────
    settings.OLLAMA_PC_ENDPOINT = ""
    check("no OLLAMA_PC_ENDPOINT: every role on the one endpoint, judge included",
          all(oh.ollama_for(r) == main for r in oh.ROLES) and oh.judge_ready())

    # ── split, but no agent: the PC is never used blind ─────────────────────
    settings.OLLAMA_PC_ENDPOINT = "http://pc:11434/"
    settings.PC_AGENT_URL, settings.PC_AGENT_TOKEN = "", None
    check("split without an agent: chat stays on the NAS", oh.ollama_for("chat") == main)
    check("...and the deletion run waits", raises_pc_unavailable("judge") and not oh.judge_ready())

    # ── split, agent reporting, PC free ─────────────────────────────────────
    settings.PC_AGENT_URL, settings.PC_AGENT_TOKEN = "http://pc:11435", TOKEN
    pc_host._state.update(available=False, draining=False, misses=0, busy=False, snapshot=None)
    pm._cached_targets, pm._cached_time = frozenset({"fallout4.exe"}), 10 ** 12
    pm._pc_gpu_cache.update(at=0.0, busy=False, since=0.0, reason="")
    poll(QUIET)
    pc = oh.host_for("chat")
    check("PC free: chat, curator and judge go to the PC",
          all(oh.ollama_for(r) == "http://pc:11434" for r in ("chat", "curator", "judge")))
    check("...enrichment stays on the NAS, so both cards work at once",
          oh.ollama_for("enrich") == main)
    from src.config import settings as _s
    _s.ENRICH_ON_PC = True
    check("...unless ENRICH_ON_PC sends it to the free PC", oh.ollama_for("enrich") == "http://pc:11434")
    _s.ENRICH_ON_PC = False
    check("...with the PC's own profile (large, 16k, all layers)",
          pc.profile == "large" and pc.num_ctx == 16384 and pc.gpu == {"num_gpu": 99})
    check("embeddings never leave the NAS", oh.ollama_for("embed") == main)
    check("a deletion run may start", oh.judge_ready())

    # ── a game on the PC ────────────────────────────────────────────────────
    poll({**QUIET, "processes": [{"name": "Fallout4.exe", "rss_mb": 9000}]})
    check("a game on the PC: busy, judged on the poll", pc_host.busy()
          and "Fallout4.exe" in pc_host.status()["busy_reason"])
    check("...chat falls back to the NAS, with the NAS's profile",
          oh.ollama_for("chat") == main and oh.host_for("chat").num_ctx == oh.main_host().num_ctx)
    check("...the deletion run waits", raises_pc_unavailable("judge") and not oh.judge_ready())
    poll(QUIET)
    check("the game ends: the PC is back on the next poll", oh.ollama_for("chat") == "http://pc:11434")

    # ── shutdown / power-off ────────────────────────────────────────────────
    pc_host.goodbye("shutdown")
    check("goodbye: everything back on the NAS at once, the run waits",
          oh.ollama_for("curator") == main and raises_pc_unavailable("judge"))
    poll(QUIET)
    poll(OSError("timed out"))
    check("one missed poll keeps the PC", oh.ollama_for("chat") == "http://pc:11434")
    poll(OSError("timed out"))
    check("two: back on the NAS", oh.ollama_for("chat") == main)

    # ── the PC's profile settings ───────────────────────────────────────────
    settings.PC_LLM_PROFILE, settings.PC_CURATOR_NUM_CTX, settings.PC_CURATOR_NUM_GPU = "small", 0, -1
    h = oh.pc_host()
    check("PC_LLM_PROFILE=small: 8k, and num_gpu -1 leaves the split to Ollama",
          h.profile == "small" and h.num_ctx == 8192 and h.gpu == {})
    settings.PC_CURATOR_NUM_CTX = 12000
    check("an explicit PC_CURATOR_NUM_CTX wins", oh.pc_host().num_ctx == 12000)
    settings.PC_LLM_PROFILE, settings.PC_CURATOR_NUM_CTX, settings.PC_CURATOR_NUM_GPU = "large", 0, 99

    # ── this machine's game flag reads this machine on a split install ──────
    check("split: the NAS's own game flag reads the NAS, not the PC report",
          pm._pc_report() is pm._LOCAL)
    check("...and an unreachable PC does not block evicting on the NAS", not pm.pc_unreachable())
    check("pc_busy without a report fails closed", pm.pc_busy(None)[0])
finally:
    for k, v in saved.items():
        setattr(settings, k, v)
    pm._cached_targets = None

# ── wiring ──────────────────────────────────────────────────────────────────
sched = (ROOT / "src/services/scheduler.py").read_text(encoding="utf-8")
eng = (ROOT / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
rec = (ROOT / "src/routers/recommendations.py").read_text(encoding="utf-8")
lp = (ROOT / "src/services/llm_priority.py").read_text(encoding="utf-8")
mu = (ROOT / "src/services/model_updates.py").read_text(encoding="utf-8")
check("the scheduled deletion run waits (stays due) while the PC is not free",
      sched.index("if not judge_ready():") < sched.index('task_monitor.create(name="ARR Sync"'))
check("the Analyze button says why instead of judging on the NAS",
      rec.index("if not judge_ready():") < rec.index('acquire_state_lock, "deletion_run"'))
check("the run resolves its host BEFORE the pitch bake, and defers without it",
      eng.index('judge_host = host_for("judge")') < eng.index("resolve_pitcher_model(judge_host.url)")
      and "except PcUnavailable" in eng)
check("the VRAM choreography acts on the active host", "effective_ollama" not in lp
      and lp.count("ollama_for('curator')") == 3)   # ps, evict, shutdown default
check("a game on the PC evicts Curatarr's models from the PC's card",
      "await unload_llm_models(_pc.url)" in sched)
check("the model-update test judges on the host it manages",
      "host=main_host()" in mu and "host=host)" in mu)

check.done()
