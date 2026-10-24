"""The container builds its own models (2026-10-23, NAS move).

start.bat checks and builds the models before the desktop server starts;
the container starts the server directly, so a fresh NAS install came up
with no bakes. With AUTO_BUILD_MODELS (set in the Dockerfile) the app builds
the main host's missing models in the background after start - the server
answers at once - and the Settings "build models" button follows the same
plan (no pitcher on a split install's NAS).

Fakes only: no Ollama is called.

    python tests/test_model_autobuild.py
"""
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services import model_autobuild as ab  # noqa: E402
from src.services import model_check as mc  # noqa: E402
from src.services import ollama_hosts as oh  # noqa: E402
from src.services import setup_wizard as sw  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


class Logs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append((record.levelname, record.getMessage()))


logs = Logs()
ab.logger.addHandler(logs)
ab.logger.setLevel(logging.INFO)

MAIN = {"url": "http://nas:30068", "num_ctx": 16384, "curator": "gemma4:26b-a4b-it-qat",
        "summarizer": "granite4.1:8b", "pitcher": None, "embedding": "nomic-embed-text-v2-moe",
        "models": ["curatarr-curator", "curatarr-summarizer", "nomic-embed-text-v2-moe"]}
PC = {"url": "http://pc:11434", "models": ["curatarr-curator", "curatarr-summarizer"]}
built = []
state = {"missing": {}, "split": False, "ident": "match"}


async def fake_build(url, curator, summarizer, base_pitcher=None, num_ctx=None,
                     with_embedding=True, embed_model=None):
    built.append(dict(url=url, curator=curator, summarizer=summarizer, pitcher=base_pitcher,
                      num_ctx=num_ctx, with_embedding=with_embedding, embed_model=embed_model))
    return {"embedding": True, "curator": True, "summarizer": state.get("summarizer_ok", True)}


saved = (mc.host_plan, mc.missing_on, mc.embedding_identity, sw.build_ollama_models, oh.split)
mc.host_plan = lambda which="main": MAIN if which == "main" else PC
mc.missing_on = lambda url, models, get=None: state["missing"].get(url, [])
mc.embedding_identity = lambda get=None: {"model": "nomic-embed-text-v2-moe", "status": state["ident"],
                                          "recorded": "ff9c2f10ef5e", "host": "ff9c2f10ef5e"
                                          if state["ident"] == "match" else "0a109f422b47"}
sw.build_ollama_models = fake_build
oh.split = lambda: state["split"]


def run(**kw):
    built.clear()
    logs.lines.clear()
    state.update(kw)
    return asyncio.run(ab.ensure_main_models())


try:
    out = run(missing={"http://nas:30068": None})
    check("Ollama not answering: nothing is built, and the log says so",
          built == [] and any("not answering" in m for _, m in logs.lines))

    out = run(missing={"http://nas:30068": []})
    check("everything present: nothing is built", built == [] and out["missing"] == [])

    out = run(missing={"http://nas:30068": ["curatarr-curator", "nomic-embed-text-v2-moe"]})
    b = built[0] if built else {}
    check("missing bakes are built on the main host, with its plan",
          b.get("url") == "http://nas:30068" and b.get("curator") == "gemma4:26b-a4b-it-qat"
          and b.get("num_ctx") == 16384 and b.get("pitcher") is None
          and b.get("with_embedding") is True)
    check("...the embedding model is passed in (no database read on the event loop)",
          b.get("embed_model") == "nomic-embed-text-v2-moe")
    check("...and the result is logged", any("ready on http://nas:30068" in m for _, m in logs.lines))

    out = run(missing={"http://nas:30068": ["curatarr-summarizer"]}, summarizer_ok=False)
    check("a failed build is an ERROR naming what failed",
          any(lv == "ERROR" and "summarizer" in m for lv, m in logs.lines))
    state["summarizer_ok"] = True

    out = run(missing={"http://nas:30068": [], "http://pc:11434": ["curatarr-pitcher"]}, split=True)
    check("split: the PC's missing bakes are named, never pulled onto it from here",
          built == [] and any("is missing curatarr-pitcher" in m for _, m in logs.lines))

    out = run(missing={"http://nas:30068": []}, split=False, ident="mismatch")
    check("embedding weights that differ are an ERROR in the log",
          out["embedding"] == "mismatch"
          and any(lv == "ERROR" and "EMBEDDING WEIGHTS DIFFER" in m for lv, m in logs.lines))

    mc.missing_on = lambda *a, **k: 1 / 0
    out = run(ident="match")
    check("it never raises (a startup background task)", isinstance(out, dict))
finally:
    mc.host_plan, mc.missing_on, mc.embedding_identity, sw.build_ollama_models, oh.split = saved

# ── wiring ──────────────────────────────────────────────────────────────────
main_src = (ROOT / "src/main.py").read_text(encoding="utf-8")
life = main_src[main_src.index("async def lifespan("):main_src.index("    yield\n")]
check("the lifespan starts it in the background, on a configured install with the flag",
      "if settings.AUTO_BUILD_MODELS:" in life and 'track_task(ensure_main_models(), name="model_autobuild")' in life
      and life.index("if settings.is_configured:") < life.index("if settings.AUTO_BUILD_MODELS:"))
from src.config import settings  # noqa: E402
check("off on the desktop by default (start.bat does it there)", settings.AUTO_BUILD_MODELS in (False, True)
      and "AUTO_BUILD_MODELS: bool = False" in (ROOT / "src/config.py").read_text(encoding="utf-8"))
check("on in the container", "AUTO_BUILD_MODELS=1" in (ROOT / "Dockerfile").read_text(encoding="utf-8"))
setup_src = (ROOT / "src/routers/setup.py").read_text(encoding="utf-8")
ep = setup_src[setup_src.index("async def build_models_endpoint("):setup_src.index("# ── post-setup")]
check("the Settings 'build models' button follows the host plan (no pitcher on a split NAS)",
      'host_plan, "main"' in ep and "base_pitcher=plan[\"pitcher\"]" in ep
      and "settings.BASE_PITCHER_MODEL" not in ep)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
