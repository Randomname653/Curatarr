"""Two-bake split: pitcher resolver + eviction-choreography tests.

    python tests/test_pitcher_routing.py

Covers the load-bearing guarantees:
  - resolve_pitcher_model(): unset -> curator; set+installed -> pitcher;
    set+missing -> curator (visible fallback); probe error -> curator.
  - evict_others(target): evicts every OTHER known big model, never the
    target, and NEVER posts an evict for a model that is not resident
    (keep_alive:0 on an absent model = load-then-unload thrash).
  - evict_if_resident(): no-op on absent models.
  - curator_start(exclusive_model=...) routes eviction to the right target.
  - wiring: pillars/engine pass the model through; config has the settings.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.services.llm_priority as lp
from src.config import settings

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── stubs ────────────────────────────────────────────────────────────────────

RESIDENT: list = []      # names /api/ps would report
EVICTED: list = []       # names _evict_model was asked to remove
INSTALLED: list = []     # names /api/tags would report
TAGS_RAISES = False


async def fake_loaded_models():
    return [{"name": n} for n in RESIDENT]


async def fake_evict_model(name, timeout=8.0):
    EVICTED.append(name)
    return True


class _FakeResp:
    def __init__(self, names):
        self.status_code = 200
        self._names = names

    def json(self):
        return {"models": [{"name": n} for n in self._names]}


class _FakeClient:
    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        if TAGS_RAISES:
            raise RuntimeError("ollama down")
        return _FakeResp(INSTALLED)


lp.loaded_models = fake_loaded_models
lp._evict_model = fake_evict_model
lp.httpx.AsyncClient = _FakeClient

settings.CURATOR_MODEL = "curatarr-curator"
settings.SUMMARIZER_MODEL = "curatarr-summarizer"


def reset(resident=(), installed=(), pitcher=""):
    global TAGS_RAISES
    RESIDENT[:] = list(resident)
    INSTALLED[:] = list(installed)
    EVICTED[:] = []
    TAGS_RAISES = False
    settings.PITCHER_MODEL = pitcher


# ── resolve_pitcher_model matrix ─────────────────────────────────────────────

reset(pitcher="")
check("resolver: unset -> curator bake",
      asyncio.run(lp.resolve_pitcher_model()) == "curatarr-curator")

reset(pitcher="curatarr-pitcher", installed=["curatarr-pitcher:latest"])
check("resolver: set + installed -> pitcher (tag-tolerant match)",
      asyncio.run(lp.resolve_pitcher_model()) == "curatarr-pitcher")

reset(pitcher="curatarr-nope", installed=["curatarr-pitcher:latest"])
check("resolver: set + missing -> curator fallback",
      asyncio.run(lp.resolve_pitcher_model()) == "curatarr-curator")

reset(pitcher="curatarr-pitcher", installed=["curatarr-pitcher:latest"])
TAGS_RAISES = True
check("resolver: probe exception -> curator fallback",
      asyncio.run(lp.resolve_pitcher_model()) == "curatarr-curator")

# ── evict_others target math + residency guard ───────────────────────────────

reset(resident=["curatarr-curator:latest", "curatarr-summarizer:latest",
                "curatarr-pitcher:latest"], pitcher="curatarr-pitcher")
asyncio.run(lp.evict_others("curatarr-pitcher"))
check("evict_others(pitcher): curator + summarizer evicted",
      sorted(EVICTED) == ["curatarr-curator", "curatarr-summarizer"])
check("evict_others(pitcher): target never evicted",
      "curatarr-pitcher" not in EVICTED)

reset(resident=["curatarr-pitcher:latest"], pitcher="curatarr-pitcher")
asyncio.run(lp.evict_others("curatarr-curator"))
check("evict_others(curator): lingering pitcher evicted",
      EVICTED == ["curatarr-pitcher"])

reset(resident=[], pitcher="curatarr-pitcher")
asyncio.run(lp.evict_others("curatarr-pitcher"))
check("evict_others: NO evict posts for absent models (thrash guard)",
      EVICTED == [])

reset(resident=["curatarr-summarizer:latest"], pitcher="")
asyncio.run(lp.evict_others("curatarr-curator"))
check("evict_others: disabled pitcher never in the candidate set",
      EVICTED == ["curatarr-summarizer"])

# ── evict_if_resident guard ──────────────────────────────────────────────────

reset(resident=[], pitcher="curatarr-pitcher")
asyncio.run(lp.evict_if_resident("curatarr-pitcher"))
check("evict_if_resident: absent -> no-op", EVICTED == [])

reset(resident=["curatarr-pitcher:latest"], pitcher="curatarr-pitcher")
asyncio.run(lp.evict_if_resident("curatarr-pitcher"))
check("evict_if_resident: resident -> evicted", EVICTED == ["curatarr-pitcher"])

# ── curator_start routes the eviction target ─────────────────────────────────

async def _start_done(exclusive):
    await lp.curator_start("test", exclusive_model=exclusive)
    lp.curator_done()

reset(resident=["curatarr-curator:latest"], pitcher="curatarr-pitcher")
asyncio.run(_start_done("curatarr-pitcher"))
check("curator_start(exclusive=pitcher) evicts resident curator",
      EVICTED == ["curatarr-curator"])

reset(resident=["curatarr-pitcher:latest"], pitcher="curatarr-pitcher")
asyncio.run(_start_done(None))
check("curator_start(default) evicts lingering pitcher",
      EVICTED == ["curatarr-pitcher"])

reset(resident=["curatarr-summarizer:latest"], pitcher="")
asyncio.run(_start_done(None))
check("curator_start(default, split off) still evicts summarizer",
      EVICTED == ["curatarr-summarizer"])

# ── wiring asserts ───────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
pil = (root / "src/services/pillars.py").read_text(encoding="utf-8")
check("adjudicate/monologue pass exclusive_model through",
      pil.count("exclusive_model=model") == 2)

eng = (root / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
check("engine resolves the pitch model once per run",
      "resolve_pitcher_model" in eng and "deletion run model" in eng)
check("pillar branch: gate acquire + re-acquire carry exclusive_model",
      eng.count("exclusive_model=pitch_model") >= 3)
check("judge + monologue run on the pitch model",
      "adjudicate(ev[\"facts\"], model=pitch_model" in eng
      and "model=pitch_model,\n                        lang_directive" in eng)
check("run-end eager evict is guarded", eng.count("evict_if_resident(pitch_model)") == 2)
check("_call_llm override only for pitches",
      eng.count("model_override=pitch_model") == 2)

cfg = (root / "src/config.py").read_text(encoding="utf-8")
check("config: PITCHER_MODEL default empty (split off)",
      'PITCHER_MODEL: str = ""' in cfg)
check("config: BASE_PITCHER_MODEL default qwen3.8:27b",
      'BASE_PITCHER_MODEL: str = "qwen3.8:27b"' in cfg)

pm = (root / "src/services/process_monitor.py").read_text(encoding="utf-8")
check("game watcher unload list includes the pitcher",
      "PITCHER_MODEL" in pm)

sw = (root / "src/services/setup_wizard.py").read_text(encoding="utf-8")
check("build_ollama_models bakes curatarr-pitcher with the curator prompt",
      '"curatarr-pitcher", base_pitcher, CURATOR_SYSTEM_PROMPT' in sw)
check("write_env persists the pitcher keys with live-settings fallback",
      "PITCHER_MODEL={config.get('pitcher_model'" in sw)

bm = (root / "build_models.py").read_text(encoding="utf-8")
check("build_models gates the pitcher on PITCHER_MODEL, not the base",
      "settings.PITCHER_MODEL or \"\").strip() else None" in bm)

st = (root / "tests/pillar_json_stresstest.py").read_text(encoding="utf-8")
check("stresstest gate includes the pitcher bake", '"curatarr-pitcher"' in st)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
