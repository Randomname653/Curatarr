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
# Imported before src.config.settings is swapped for a fake further down.
import src.routers.model_updates as rmu  # noqa: E402
from src.services import notifications as nt  # noqa: E402

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

# ── cleanup: rollback, failed test, restart mid-test ──────────────────────
row0 = {"role": "curator", "base": "gemma4:31b", "bake": "curatarr-curator",
        "running": "sha256:old", "remote": "sha256:new", "status": "update", "comparable": True}
PIN = mu.pin_name("gemma4:31b", "sha256:old")


def reset(base_on):
    calls.clear()
    RUN.update({"gemma4:31b": base_on, PIN: "sha256:old"})
    store.clear()
    store["curator"] = dict(row0)


async def tracking_ollama(endpoint, method, path, **kw):
    calls.append((method, path, kw.get("json")))
    if path == "/api/copy":     # the copy really moves the tag in the fake
        RUN[kw["json"]["destination"]] = RUN.get(kw["json"]["source"])


mu._ollama = tracking_ollama
reset("sha256:new")      # the pull had already moved the tag
asyncio.run(mu.rollback("http://localhost:11434", store["curator"]))
check("rollback: the tag goes back to the pinned weights",
      ("POST", "/api/copy", {"source": PIN, "destination": "gemma4:31b"}) in calls
      and RUN["gemma4:31b"] == "sha256:old")
check("rollback: the test bake is deleted (that frees the new weights) and then the pin",
      [c[2]["model"] for c in calls if c[0] == "DELETE"] == [mu.TEST_BAKE, PIN])
reset("sha256:new")
RUN[PIN] = None          # the pin is missing: the tag must not be touched
asyncio.run(mu.rollback("http://localhost:11434", store["curator"]))
check("rollback without a valid pin: no copy, and no pin delete",
      not any(c[1] == "/api/copy" for c in calls)
      and [c[2]["model"] for c in calls if c[0] == "DELETE"] == [mu.TEST_BAKE])


async def failing_compare(*a, **k):
    raise RuntimeError("could not pull gemma4:31b")


real_compare = mu._compare
mu._compare = failing_compare
reset("sha256:old")
try:
    asyncio.run(mu.run_comparison("curator", 1))
except RuntimeError:
    pass
mu._compare = real_compare
check("a failed test rolls back by itself and clears its marker",
      any(c[0] == "DELETE" and c[2]["model"] == mu.TEST_BAKE for c in calls)
      and "test_started" not in store["curator"])

reset("sha256:new")
store["curator"]["test_started"] = "2026-10-15T12:00:00+00:00"   # a restart cut it off
done = asyncio.run(mu.recover_interrupted())
check("startup recovery rolls back a cut-off test and says so",
      done == ["curator"] and RUN["gemma4:31b"] == "sha256:old"
      and "restart" in store["curator"]["compare_error"] and "test_started" not in store["curator"])
reset("sha256:new")
store["curator"]["test_started"] = "x"
store["curator"]["comparison"] = {"summary": {}}
check("...but leaves a FINISHED test alone (its result waits for the decision)",
      asyncio.run(mu.recover_interrupted()) == [])
check("partials are only touched for a local Ollama",
      mu.remove_partials("http://192.168.0.5:11434", "sha256:new") == 0)

# ── wiring ─────────────────────────────────────────────────────────────────
root = Path(__file__).resolve().parents[1]
main = (root / "src/main.py").read_text(encoding="utf-8")
check("the routes are admin-only",
      'model_updates.router,   prefix="/api/system/model-updates"' in main
      and "dependencies=_ADMIN_ONLY)" in main.split("model_updates.router", 1)[1][:200])
sched = (root / "src/services/scheduler.py").read_text(encoding="utf-8")
check("a cut-off test is recovered once at startup", "recover_interrupted" in sched)
check("a daily check is scheduled, and never with next_run_time=None (that pauses it)",
      "job_model_update_check" in sched
      and not __import__("re").search(r"next_run_time\s*=\s*None\s*[,)]", sched))
from tests.frontend_files import everything  # noqa: E402
html = everything()
check("Maintenance has the section, the bell reaches it",
      "/api/system/model-updates" in html and "maint-models" in html
      and "act('openModelUpdates')" in html)
check("the notice lives in the bell + the Settings bubble, not the chat glance panel",
      "/api/system/notifications" in html and 'id="settings-badge"' in html
      and "setBadge('settings-badge'" in html)
_chat = (root / "frontend/js/chat.js").read_text(encoding="utf-8")
check("...the glance panel no longer fetches model updates", "model-updates" not in _chat)

# ── the bell's system section (services/notifications.py) ──────────────────


class U:
    def __init__(self, admin): self.is_admin = admin


store.clear()
store["curator"] = {"role": "curator", "base": "gemma4:31b", "bake": "curatarr-curator",
                    "running": "sha256:old", "remote": "sha256:" + "n" * 64,
                    "status": "update", "comparable": True}
store["summarizer"] = {"role": "summarizer", "base": "granite4.1:8b", "bake": "curatarr-summarizer",
                       "running": "sha256:s", "remote": "sha256:t" * 1, "status": "update",
                       "comparable": False}
n = nt.system_notifications(U(True))
check("bell: an update waiting = an item with the test action, counted as needs-action",
      any(i["title"].startswith("New weights for gemma4:31b") and i["action"]["key"] == "model_updates"
          for i in n["items"]) and n["needs_action"] == 2)
check("bell: the summarizer says there is no test for it",
      any("no test for this role" in i["text"] for i in n["items"]))
check("bell: a non-admin sees nothing of it", nt.system_notifications(U(False))["items"] == [])
rmu._running["role"] = "curator"
item = next(i for i in nt.system_notifications(U(True))["items"] if "gemma4" in i["title"])
check("bell: a running test is info without an action (nothing to decide yet)",
      item["title"].startswith("Testing") and item["action"] is None)
rmu._running["role"] = None
store["curator"]["comparison"] = {"summary": {"verdicts_same": 9, "verdicts_compared": 10,
                                              "wording_similarity_pct": 41},
                                  "at": "t", "old": {"titles": [], "chat": []},
                                  "new": {"titles": [], "chat": []}}
item = next(i for i in nt.system_notifications(U(True))["items"] if "gemma4" in i["title"])
check("bell: a finished test asks for the decision with its numbers",
      item["title"].startswith("Test result ready") and "9 of 10" in item["text"])


def broken():
    raise RuntimeError("boom")


nt.PROVIDERS.insert(0, (broken, False))
check("bell: one broken provider does not empty the list",
      len(nt.system_notifications(U(True))["items"]) == 2)
nt.PROVIDERS.pop(0)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
