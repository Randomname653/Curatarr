"""Game/GPU probes and TLS setup never run on the event loop.

  * gpu_pressure / game_process_running called on the loop thread answer
    from the last verdict: no nvidia-smi, no Ollama /api/ps call, no
    process walk (each LLM call asked through ollama_options -> llm_lane)
  * called off the loop they probe and refresh that verdict, and
    is_game_running runs both probes (no short circuit) so neither goes stale
  * the lifespan seeds game_active with a real probe, on a worker
  * every httpx.AsyncClient in src/ shares one TLS context: building a
    client no longer loads the CA bundle (~50 ms of CPU each)

Each behaviour check fails on the code before this change.

    python tests/test_loop_free_probes.py
"""
import ast
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.services.process_monitor as pm  # noqa: E402
from src.config import settings  # noqa: E402

from _checks import check  # noqa: E402


# ── 1. probes ───────────────────────────────────────────────────────────────
print("game / GPU probes")
probes = []
settings.GPU_PRESSURE_GATE = True
pm._nvidia_smi_read = lambda: (probes.append("smi"), (23000.0, 24000.0, 99.0))[1]
pm._ollama_loaded_models = lambda: (probes.append("ps"), [])[1]
pm._probe_game_process = lambda: (probes.append("walk"), True)[1]
pm._gpu_cache.update(at=0.0, busy=False, since=0.0, reason="")
pm._last_game["running"] = False


async def _on_loop():
    return pm.gpu_pressure(), pm.game_process_running(), pm.is_game_running()


check("on the loop: no nvidia-smi, no /api/ps, no process walk",
      asyncio.run(_on_loop()) == (False, False, False) and probes == [])

probes.clear()
pm._GPU_HOLD_S = 0
got = pm.is_game_running()          # off the loop: a real probe
check("off the loop: both probes run, no short circuit",
      got is True and "smi" in probes and "walk" in probes)
check("... and the loop then reads the refreshed verdicts",
      asyncio.run(_on_loop()) == (True, True, True))


async def _watcher_style():
    return await asyncio.to_thread(pm.is_game_running)


probes.clear()
pm._gpu_cache["at"] = 0.0
check("a probe handed to a worker (the game watcher) still probes",
      asyncio.run(_watcher_style()) is True and "walk" in probes)

main_src = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
check("the lifespan seeds game_active with a real probe on a worker",
      "_game = await asyncio.to_thread(_igr)" in main_src)

# ── 2. TLS ──────────────────────────────────────────────────────────────────
print("shared TLS context")
import httpx  # noqa: E402
from src.services.http_client import tls_context  # noqa: E402

check("one context, built once", tls_context() is tls_context())
t0 = time.perf_counter()
for _ in range(10):
    httpx.AsyncClient(timeout=5, verify=tls_context())
per = (time.perf_counter() - t0) / 10
check(f"a client with it costs {per * 1000:.1f} ms (a default one loads the CA bundle)",
      per < 0.02)

missing = []
for p in sorted((ROOT / "src").rglob("*.py")):
    for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "AsyncClient"
                and not any(k.arg == "verify" for k in n.keywords)):
            missing.append(f"{p.relative_to(ROOT).as_posix()}:{n.lineno}")
check(f"every AsyncClient in src/ passes verify= ({len(missing)} without: {missing[:3]})",
      not missing)
check("the lifespan builds the context before serving",
      "tls_context()" in main_src[main_src.index("async def lifespan"):
                                  main_src.index("loop_guard.serving(True)")])

check.done()
