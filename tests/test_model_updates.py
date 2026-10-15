"""Model updates: the pin becomes visible, old-vs-new is the owner's call.

    python tests/test_model_updates.py

No network, no Ollama, no GPU: the registry/Ollama calls and the app
state are replaced by fakes, so this suite is the same on every machine.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services import model_updates as mu  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── refs + identity ────────────────────────────────────────────────────────
check("a dot in a bare name is a version, not a host (qwen3.8, granite4.1)",
      mu.split_ref("qwen3.8:27b") == ("library", "qwen3.8", "27b")
      and mu.split_ref("granite4.1:8b") == ("library", "granite4.1", "8b"))
check("another registry's host is not looked up", mu.split_ref("hf.co/org/m:q4") is None)
check("a user namespace keeps its user, no tag means latest",
      mu.split_ref("someone/model") == ("someone", "model", "latest"))
mf = ("FROM C:\\Users\\x\\.ollama\\models\\blobs\\sha256-" + "a" * 64 + "\n"
      "FROM C:\\Users\\x\\.ollama\\models\\blobs\\sha256-" + "b" * 64 + "\nSYSTEM x")
check("the FIRST FROM blob is the running weights (the second is the projector)",
      mu._FROM.search(mf).group(1) == "a" * 64)
check("pin name carries the digest", mu.pin_name("gemma4:31b", "sha256:3bf4a7778de8cbf7")
      == "gemma4:31b-pinned-3bf4a7778de8")

check("classify: same weights = current", mu.classify("d1", "d1", None) == "current")
check("classify: registry moved = update", mu.classify("d1", "d2", None) == "update")
check("classify: the declined digest stays quiet", mu.classify("d1", "d2", "d2") == "pinned")
check("classify: a newer digest after declining asks again", mu.classify("d1", "d3", "d2") == "update")
check("classify: unknown on either side", mu.classify(None, "d2", None) == "unknown")


class S:
    CURATOR_MODEL = "curatarr-curator"; BASE_CURATOR_MODEL = "gemma4:31b"
    SUMMARIZER_MODEL = "curatarr-summarizer"; BASE_SUMMARIZER_MODEL = "granite4.1:8b"
    PITCHER_MODEL = ""; BASE_PITCHER_MODEL = "qwen3.8:27b"
    effective_ollama = "http://ollama.test"


roles = [t["role"] for t in mu.tracked_models(S)]
check("no pitcher tracked unless the two-bake split is on", roles == ["curator", "summarizer"])
S.PITCHER_MODEL = "curatarr-pitcher"
check("the pitcher joins when configured", "pitcher" in [t["role"] for t in mu.tracked_models(S)])
check("the embedding model is never offered (it would mean re-embedding)",
      all(t["role"] != "embedding" for t in mu.tracked_models(S)))

# ── check_updates with fakes ───────────────────────────────────────────────
store = {}
mu.load_state = lambda: dict(store)
mu.save_state = lambda st: (store.clear(), store.update(st))
RUN = {"curatarr-curator": "sha256:old", "curatarr-summarizer": "sha256:sum",
       "curatarr-pitcher": "sha256:pit"}
REM = {"gemma4:31b": "sha256:new", "granite4.1:8b": "sha256:sum", "qwen3.8:27b": "sha256:pit"}


async def fake_running(endpoint, model):
    return RUN.get(model)


async def fake_registry(ref):
    return REM.get(ref)


mu.running_weights, mu.registry_weights = fake_running, fake_registry
import src.config  # noqa: E402
src.config.settings = S
rows = asyncio.run(mu.check_updates())
st = {r["role"]: r["status"] for r in rows}
check("check: curator has new weights, the others are current",
      st == {"curator": "update", "summarizer": "current", "pitcher": "current"})
check("check: state is stored with both digests",
      store["curator"]["running"] == "sha256:old" and store["curator"]["remote"] == "sha256:new")
store["curator"]["comparison"] = {"remote": "sha256:new", "summary": {}}
asyncio.run(mu.check_updates())
check("a comparison of the current remote digest survives a re-check", "comparison" in store["curator"])
REM["gemma4:31b"] = "sha256:newer"
asyncio.run(mu.check_updates())
check("...and is dropped once the registry moves again", "comparison" not in store["curator"])

# ── summary ────────────────────────────────────────────────────────────────
def side(verdicts, texts, judge, err=None):
    return {"titles": [{"title": f"T{i}", "verdict": v, "error": (err or [None] * 9)[i],
                        "judge_s": judge, "monologue": t, "monologue_s": 2.0}
                       for i, (v, t) in enumerate(zip(verdicts, texts))],
            "chat": [{"prompt": "q", "text": "same answer here", "s": 1.0}],
            "speed": {"gen_tok_s": 30.0}, "residency": {"vram_gb": 20.1, "gpu_share_pct": 100}}


old = side(["CUT", "HARD_KEEP", "STAGNANT"], ["a b c d", "x y", "p q r"], 7.0)
new = side(["CUT", "CUT", "STAGNANT"], ["a b c d", "x y z", "p q r"], 5.0, err=[None, None, "timeout"])
summ = mu.summarize(old, new)
check("summary: failures counted per side, failed pairs not compared",
      summ["failures"] == {"old": 0, "new": 1} and summ["verdicts_compared"] == 2)
check("summary: changed verdicts are named",
      summ["verdict_changes"] == [{"title": "T1", "old": "HARD_KEEP", "new": "CUT"}])
check("summary: speed, VRAM and wording side by side",
      summ["judge_s"] == {"old": 7.0, "new": 5.0} and summ["vram_gb"]["new"] == 20.1
      and 0 < summ["wording_similarity_pct"] <= 100 and summ["chat_similarity_pct"] == 100)

# ── decide ─────────────────────────────────────────────────────────────────
calls = []


async def fake_ollama(endpoint, method, path, **kw):
    calls.append((method, path, kw.get("json")))


mu._ollama = fake_ollama
store.clear()
store["curator"] = {"role": "curator", "base": "gemma4:31b", "running": "sha256:old",
                    "remote": "sha256:new", "status": "update"}
RUN["gemma4:31b-pinned-old"] = "sha256:old"
row = asyncio.run(mu.decide("curator", "stay"))
check("stay without a test: the pin goes back under the tag",
      ("POST", "/api/copy", {"source": "gemma4:31b-pinned-old", "destination": "gemma4:31b"}) in calls)
check("...the declined digest is remembered and the notice goes quiet",
      row["status"] == "pinned" and row["declined_remote"] == "sha256:new")
check("...pin and test bake are cleaned up",
      ("DELETE", "/api/delete", {"model": mu.TEST_BAKE}) in calls)
try:
    asyncio.run(mu.decide("curator", "update"))
    check("update without a test is refused", False)
except ValueError:
    check("update without a test is refused", True)

built = []


async def fake_build(*a):
    built.append(a)
    return {}


import src.services.setup_wizard as sw  # noqa: E402
sw.build_ollama_models = fake_build
store["curator"].update(status="update", comparison={
    "pinned_as": "gemma4:31b-pinned-old", "new_weights": "sha256:new", "summary": {"x": 1}})
row = asyncio.run(mu.decide("curator", "update"))
check("update after a test rebuilds the bakes and records the new running weights",
      built and row["running"] == "sha256:new" and row["status"] == "current"
      and row["decided"]["summary"] == {"x": 1})

# ── wiring ─────────────────────────────────────────────────────────────────
root = Path(__file__).resolve().parents[1]
main = (root / "src/main.py").read_text(encoding="utf-8")
check("the routes are admin-only",
      'model_updates.router,   prefix="/api/system/model-updates"' in main
      and "dependencies=_ADMIN_ONLY)" in main.split("model_updates.router", 1)[1][:200])
sched = (root / "src/services/scheduler.py").read_text(encoding="utf-8")
check("a daily check is scheduled, and never with next_run_time=None (that pauses it)",
      "job_model_update_check" in sched
      and not __import__("re").search(r"next_run_time\s*=\s*None\s*[,)]", sched))
from tests.frontend_files import everything  # noqa: E402
html = everything()
check("the glance panel shows the notice and Maintenance has the section",
      "/api/system/model-updates" in html and "maint-models" in html
      and "openModelUpdates" in html)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
