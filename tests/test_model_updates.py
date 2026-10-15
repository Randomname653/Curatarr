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

# ── the self-review (services/model_update_eval.py) ───────────────────────
from src.services import model_update_eval as ev  # noqa: E402

base_summary = {"failures": {"old": 0, "new": 0}, "gpu_share_pct": {"old": 100, "new": 100},
                "gen_tok_s": {"old": 30.0, "new": 29.0}}
check("guards: a clean run has no hard rule", ev.guards(base_summary) == [])
check("guards: more failures, a spill, or a big slowdown are hard rules",
      len(ev.guards({"failures": {"old": 0, "new": 2}, "gpu_share_pct": {"new": 80},
                     "gen_tok_s": {"old": 30.0, "new": 20.0}})) == 3)


def asker(pick):
    """A fake judge: pick(user_text) -> 'A' | 'B' | 'tie'."""
    async def _ask(endpoint, model, system, user, schema):
        if schema is ev.FINAL_SCHEMA:
            return {"recommendation": FINAL["rec"], "reason": "numbers"}
        return {"better": pick(user), "reason": "r"}
    return _ask


FINAL = {"rec": "update"}
# Always prefers whichever answer mentions "NEWTEXT": a content preference.
ev._ask = asker(lambda u: "A" if u.index("ANSWER A") < u.find("NEWTEXT") < u.index("ANSWER B") else "B")
r = asyncio.run(ev.judge_pair("e", "m", "Title", "sys", "ctx", "old text", "NEWTEXT"))
check("judge: a preference that survives the A/B swap counts", r["winner"] == "new" and not r["position_flip"])
ev._ask = asker(lambda u: "A")      # always picks the first slot: pure position bias
r = asyncio.run(ev.judge_pair("e", "m", "Title", "sys", "ctx", "old text", "NEWTEXT"))
check("judge: pure position bias becomes a tie, flagged", r["winner"] == "tie" and r["position_flip"])
check("judge: which side is A is seeded by the title (reproducible, not always the same slot)",
      ev._new_is_a("x") == ev._new_is_a("x")
      and len({ev._new_is_a(t) for t in ("a", "b", "c", "d", "e", "f")}) == 2)

cmp_fixture = {
    "summary": {**base_summary, "verdicts_same": 2, "verdicts_compared": 2,
                "judge_s": {"old": 7, "new": 5}, "vram_gb": {"old": 20, "new": 20}},
    "facts": {"T1": "FACTS one", "T2": "FACTS two"},
    "old": {"titles": [{"title": "T1", "verdict": "CUT", "monologue": "old 1", "error": None},
                       {"title": "T2", "verdict": "CUT", "monologue": "old 2", "error": None}],
            "chat": [{"prompt": "q", "text": "old chat"}]},
    "new": {"titles": [{"title": "T1", "verdict": "CUT", "monologue": "NEWTEXT 1", "error": None},
                       {"title": "T2", "verdict": "CUT", "monologue": "NEWTEXT 2", "error": "timeout"}],
            "chat": [{"prompt": "q", "text": "NEWTEXT chat"}]},
}
seen_ctx = []
def pick_new(u):
    seen_ctx.append(u)
    return "A" if u.index("ANSWER A") < u.find("NEWTEXT") < u.index("ANSWER B") else "B"
ev._ask = asker(pick_new)
out = asyncio.run(ev.evaluate(cmp_fixture, "e", "gemma4:31b-pinned-old"))
check("review: failed pairs are skipped, the rest tallied (titles + chat)",
      out["tally"]["titles"]["new"] == 1 and len(out["titles"]) == 1 and out["tally"]["chat"]["new"] == 1)
check("review: each title is judged against its own facts", any("FACTS one" in u for u in seen_ctx))
check("review: no hard rule -> the model's recommendation stands",
      out["recommendation"] == "update" and not out["overruled"] and out["self_judged"])
spill = {**cmp_fixture, "summary": {**cmp_fixture["summary"], "gpu_share_pct": {"new": 70}}}
out = asyncio.run(ev.evaluate(spill, "e", "m"))
check("review: a hard rule overrules the model's 'update' -> stay, with the reason",
      out["model_recommendation"] == "update" and out["recommendation"] == "stay"
      and out["overruled"] and "GPU" in out["guards"][0])
FINAL["rec"] = "stay"
out = asyncio.run(ev.evaluate(spill, "e", "m"))
check("review: a hard rule never turns a 'stay' into anything else",
      out["recommendation"] == "stay" and not out["overruled"])

# ── a quiet card: exact residency, co-resident models, the bulk hold ──────
class R:
    def __init__(self, d): self._d = d; self.status_code = 200
    def json(self): return self._d


PS = {"models": [{"name": "curatarr-summarizer:latest", "size": 6 * 2**30, "size_vram": 2 * 2**30},
                 {"name": "curatarr-update-test:latest", "size": 20 * 2**30, "size_vram": 20 * 2**30}]}


async def ps_ollama(endpoint, method, path, **kw):
    return R(PS)


mu._ollama = ps_ollama
res = asyncio.run(mu._residency("e", "curatarr-update-test"))
check("residency: exactly the named model (a prefix match once read the 2 GB summariser)",
      res["vram_gb"] == 20.0 and res["gpu_share_pct"] == 100)
check("residency: every other resident model is recorded", res["co_resident"] == ["curatarr-summarizer:latest"])
check("names with and without :latest are the same model", mu._same("x", "x:latest") and not mu._same("x", "xy"))

import src.services.llm_priority as lp  # noqa: E402


async def hold_case():
    # The curator gate is closed; enrichment would give up after `timeout`.
    lp.release_bulk_hold()      # the review tests above left a 900 s hold
    lp._get_event().clear()
    lp.extend_bulk_hold(0.6)
    t0 = asyncio.get_event_loop().time()
    await lp.wait_for_enrichment_slot(timeout=0.1)
    waited = asyncio.get_event_loop().time() - t0
    lp.release_bulk_hold()
    t1 = asyncio.get_event_loop().time()
    await lp.wait_for_enrichment_slot(timeout=0.1)
    plain = asyncio.get_event_loop().time() - t1
    lp._get_event().set()
    return waited, plain


waited, plain = asyncio.run(hold_case())
check("bulk hold: enrichment keeps waiting past its timeout while a long job holds",
      waited >= 0.55)
check("bulk hold: without a hold the 600 s safety timeout still applies (here 0.1 s)",
      plain < 0.4)

# ── run_evaluation end to end (fakes for Ollama, the gate and the judge) ──
from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def no_gate(*a, **k):
    yield


async def noop(*a, **k):
    return None


async def fake_measure(endpoint, model):
    return {"speed": {"gen_tok_s": 30.0}, "residency": {"vram_gb": 19.9, "gpu_share_pct": 100,
                                                         "co_resident": []}, "clean": True}


judged_by = []


async def fake_evaluate(cmp_, endpoint, judge):
    judged_by.append(judge)
    return {"recommendation": "either", "guards": [], "tally": {}, "titles": [], "chat": []}


lp.curator_priority, lp.evict_if_resident = no_gate, noop
real_measure = mu.measure
mu._refill_facts, mu._alone, mu.measure = noop, noop, fake_measure
ev.evaluate = fake_evaluate
store.clear()
store["curator"] = {**row0, "comparison": {
    "pinned_as": PIN, "summary": {"failures": {"old": 0, "new": 0}},
    "old": {"titles": [], "chat": [], "clean": False}, "new": {"titles": [], "chat": []}}}
RUN.update({PIN: "sha256:old", "gemma4:31b": "sha256:new"})
out = asyncio.run(mu.run_evaluation("curator", 1))
check("review: runs end to end and picks the PIN as judge (the current weights, no persona)",
      judged_by == [PIN] and store["curator"]["comparison"]["evaluation"]["recommendation"] == "either")
check("review: the clean re-measure replaces the test's speed/VRAM, contaminated times are flagged",
      store["curator"]["comparison"]["summary"]["vram_gb"] == {"old": 19.9, "new": 19.9}
      and store["curator"]["comparison"]["summary"]["timings_contaminated"] is True)
RUN[PIN] = None
judged_by.clear()
asyncio.run(mu.run_evaluation("curator", 1))
check("review: without a valid pin the bake judges (its persona is replaced by the reviewer's prompt)",
      judged_by == ["curatarr-curator"])
import inspect, re as _re  # noqa: E401,E402
check("no `await` inside next(<generator>) in the update code (the first live review died of it)",
      not _re.search(r"next\(\([^)]*await", inspect.getsource(mu)))

class _Admin:
    is_admin = True


rmu._start("curator", "review")
item = next(i for i in nt.system_notifications(_Admin())["items"] if "gemma4" in i["title"])
check("bell: a running REVIEW says review, not test", "reviews" in item["title"] and item["action"] is None)
rmu._stop()
store["curator"]["eval_error"] = "TypeError: boom"
store["curator"]["comparison"].pop("evaluation", None)
item = next(i for i in nt.system_notifications(_Admin())["items"] if "gemma4" in i["title"])
check("bell: a failed review is shown, the test result kept", item["title"].startswith("Review failed"))

# ── VRAM: nvidia-smi beats an implausible /api/ps (the 2.1 GB gemma4 case) ─
async def run_measure(endpoint, smi_values, ps_gb, file_gb):
    vals = iter(smi_values)
    async def fake_smi():
        return next(vals, None)
    async def fake_speed(e, m):
        return {"gen_tok_s": 32.1}
    async def fake_res(e, m):
        return {"vram_gb": ps_gb, "gpu_share_pct": 100, "co_resident": []}
    async def fake_file(e, m):
        return file_gb
    async def fake_settled(*a, **k):
        return await fake_smi()
    saved = (mu._smi_settled, mu._speed, mu._residency, mu._file_gb, mu._alone)
    mu._smi_settled, mu._speed, mu._residency, mu._file_gb, mu._alone = (
        fake_settled, fake_speed, fake_res, fake_file, noop)
    lp._evict_model = noop
    try:
        return await real_measure(endpoint, "curatarr-update-test")
    finally:
        mu._smi_settled, mu._speed, mu._residency, mu._file_gb, mu._alone = saved


async def settled_case():
    seq = iter([1500, 1620, 1490])
    async def smi():
        return next(seq)
    saved = mu._smi_used_mib
    mu._smi_used_mib = smi
    try:
        return await mu._smi_settled(0, 3)
    finally:
        mu._smi_used_mib = saved


check("a settled reading is the median of three (one flicker does not move it)",
      asyncio.run(settled_case()) == 1500)


m_local = asyncio.run(run_measure("http://127.0.0.1:11434", [1500, 23900], 2.0, 19.9))
check("local Ollama: VRAM is the nvidia-smi difference, ps flagged implausible",
      m_local["residency"]["vram_gb"] == round((23900 - 1500) / 1024, 1)
      and m_local["residency"]["vram_source"] == "nvidia-smi"
      and m_local["residency"]["ps_implausible"] and m_local["residency"]["vram_gb_ps"] == 2.0)
m_remote = asyncio.run(run_measure("http://192.0.2.10:11434", [], 2.0, 19.9))
check("remote Ollama + implausible ps: VRAM is unknown, not 2 GB",
      m_remote["residency"]["vram_gb"] is None and "implausible" in m_remote["residency"]["vram_source"])
m_ok = asyncio.run(run_measure("http://192.0.2.10:11434", [], 19.5, 19.9))
check("remote Ollama + plausible ps: Ollama's figure is used",
      m_ok["residency"]["vram_gb"] == 19.5 and m_ok["residency"]["vram_source"] == "ollama")
check("the reviewer is told 'not measurable', never a None or a fake number",
      ev._gb(None) == "not measurable" and ev._gb(19.9) == "19.9")

# ── wiring ─────────────────────────────────────────────────────────────────
root = Path(__file__).resolve().parents[1]
main = (root / "src/main.py").read_text(encoding="utf-8")
check("the routes are admin-only",
      'model_updates.router,   prefix="/api/system/model-updates"' in main
      and "dependencies=_ADMIN_ONLY)" in main.split("model_updates.router", 1)[1][:200])
sched = (root / "src/services/scheduler.py").read_text(encoding="utf-8")
check("a cut-off test is recovered once at startup", "recover_interrupted" in sched)
_router = (root / "src/routers/model_updates.py").read_text(encoding="utf-8")
check("the review runs right after a test and can be started on its own",
      "run_evaluation" in _router and '"/{role}/evaluate"' in _router)
check("a daily check is scheduled, and never with next_run_time=None (that pauses it)",
      "job_model_update_check" in sched
      and not __import__("re").search(r"next_run_time\s*=\s*None\s*[,)]", sched))
from tests.frontend_files import everything  # noqa: E402
html = everything()
check("while a test or review runs, the view disables actions and re-reads itself",
      "const off = run ? 'disabled' : ''" in html and "_muPoll(!!run)" in html)
check("an existing review can be run again, and an old one is flagged",
      "Review again (re-measures speed and VRAM)" in html and "!summary?.vram_source" in html)
check("the view says when per-title times were taken next to another model",
      "timings_contaminated" in html)
check("the Maintenance view shows the review and offers it when missing",
      "_muEval(" in html and "act('evaluateModelUpdate'" in html)
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
rmu._start("curator", "test")
item = next(i for i in nt.system_notifications(U(True))["items"] if "gemma4" in i["title"])
check("bell: a running test is info without an action (nothing to decide yet)",
      item["title"].startswith("Testing") and item["action"] is None)
rmu._stop()
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
