"""The hardware-matrix bench's bookkeeping and decision rules — no GPU needed.

    python tests/test_hwbench.py

Pins what must never silently change: a model is identified by its weights
digest (not its tag), a known measurement is never repeated, the matrix
K.O. rules and tier derivation, the KV-cache inheritance rule, the nested
manifest walk, and the reproducible item subset.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.benchmarks.hwbench import matrix, registry  # noqa: E402
from tests.benchmarks.hwbench.ledger import Ledger, fingerprint, model_key  # noqa: E402
from tests.benchmarks.hwbench.quality import buzzwords, sentences  # noqa: E402
from tests.benchmarks.hwbench.run import tightest_tier  # noqa: E402
from tests.benchmarks.hwbench.server import ServerConfig  # noqa: E402
from tests.benchmarks.hwbench.snapshot_items import select_subset  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


tmp = Path(tempfile.mkdtemp(prefix="hwbench-test-"))

# ── identity + ledger ──────────────────────────────────────────────────────
check("model key = ref + weights digest prefix",
      model_key("gemma4:31b", "sha256:280af6832eca23cb") == "gemma4:31b@280af6832eca")
check("same tag, other weights -> another model",
      model_key("gemma4:31b", "sha256:280af6") != model_key("gemma4:31b", "sha256:cd20f2"))
s1 = {"num_ctx": 16384, "tier_gb": 12, "server": "fa-auto_kv-f16"}
check("fingerprint ignores dict order",
      fingerprint("m@1", "perf", s1) == fingerprint("m@1", "perf", dict(reversed(list(s1.items())))))
check("fingerprint changes with a setting",
      fingerprint("m@1", "perf", s1) != fingerprint("m@1", "perf", {**s1, "tier_gb": 8}))

led = Ledger(tmp / "ledger.json")
k = led.upsert_model("x:1b", "sha256:abc123", family="x", architecture="dense")
led.upsert_model("x:1b", "sha256:abc123", family="")      # a blank must not erase
check("upsert keeps known facts", led.model(k)["family"] == "x")
row = led.add_run(k, "perf", s1, env={"gpu": "t"}, metrics={"status": "ok"})
check("a recorded run is known by its fingerprint", led.has_run(row["fingerprint"]))
led.add_run(k, "quality", {"profile": "small"}, env={}, metrics={}, status="failed")
check("a FAILED run does not count as known",
      not led.has_run(fingerprint(k, "quality", {"profile": "small"})))
check("ledger survives a reload", Ledger(tmp / "ledger.json").has_run(row["fingerprint"]))
led.add_score(row["id"], scorer="claude", rubric="r", aggregate={"mean25": 20})
led.add_score(row["id"], scorer="claude", rubric="r", aggregate={"mean25": 21})
check("re-scoring replaces, never duplicates",
      [s["aggregate"]["mean25"] for s in led.scores_for(row["id"])] == [21])

# ── registry ───────────────────────────────────────────────────────────────
check("refs split into namespace/name/tag",
      registry.split_ref("gemma4:31b") == ("library", "gemma4", "31b")
      and registry.split_ref("user/m") == ("user", "m", "latest"))
man = {"layers": [
    {"mediaType": "application/vnd.ollama.image.projector", "digest": "sha256:p", "size": 10},
    {"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:w", "size": 100},
    {"mediaType": "application/vnd.ollama.image.draft", "digest": "sha256:d", "size": 5}]}
summ = registry.summarize_manifest(man)
check("weights layer is the identity; draft + projector are noticed",
      summ["weights_digest"] == "sha256:w" and summ["has_draft"] and summ["has_projector"]
      and summ["total_bytes"] == 115)
# A manifest LIST (newer Ollama) keeps the weights one level down.
root = tmp / "models"
(root / "blobs").mkdir(parents=True)
inner = {"layers": [{"mediaType": "application/vnd.ollama.image.model",
                     "digest": "sha256:inner", "size": 7}]}
(root / "blobs" / "sha256-nested").write_text(json.dumps(inner))
mdir = root / "manifests" / "registry.ollama.ai" / "library" / "m"
mdir.mkdir(parents=True)
(mdir / "1b").write_text(json.dumps({"layers": [
    {"mediaType": "application/vnd.ollama.image.license", "digest": "sha256:l", "size": 1},
    {"mediaType": "application/vnd.docker.distribution.manifest.v2+json",
     "digest": "sha256:nested", "size": 2}]}))
check("a nested manifest list is followed to the weights",
      registry.local_manifest("m:1b", root)["weights_digest"] == "sha256:inner")
check("an uninstalled tag reads as None", registry.local_manifest("m:2b", root) is None)

# ── server config ──────────────────────────────────────────────────────────
check("the default server sets no flash-attention override",
      "OLLAMA_FLASH_ATTENTION" not in ServerConfig().env()
      and ServerConfig().label == matrix.DEFAULT_SERVER)
check("q8 server: FA on + q8_0 KV", ServerConfig("1", "q8_0").env()["OLLAMA_KV_CACHE_TYPE"] == "q8_0"
      and ServerConfig("1", "q8_0").env()["OLLAMA_FLASH_ATTENTION"] == "1")

# ── tier planning + K.O. rules ─────────────────────────────────────────────
check("tightest tier: 19.9 GB model + 1 GB reserve -> 24 is the card itself, none below",
      tightest_tier(20400, 1024, 24564) is None)
check("tightest tier: a 7 GB model fits 10 GB with reserve and margin",
      tightest_tier(7000, 1024, 24564) == 10)
ok = {"status": "ok", "gpu_share_pct": 100, "mono_gen_tok_s_median": 30, "judge_wall_s_p90": 8}
check("a resident, clean cell passes", matrix.perf_ok(ok)[0])
check("a timeout fails", not matrix.perf_ok({**ok, "status": "timeout"})[0])
check("escalation beyond x3 fails", not matrix.perf_ok({**ok, "judge_escalation_ratio": 4})[0])
check("offloaded but too slow fails",
      not matrix.perf_ok({**ok, "gpu_share_pct": 55, "mono_gen_tok_s_median": 3})[0])
check("offloaded and usable passes",
      matrix.perf_ok({**ok, "gpu_share_pct": 55, "mono_gen_tok_s_median": 9, "judge_wall_s_p90": 40})[0])
check("a cell whose /api/ps figure is implausible cannot vouch for a fit",
      not matrix.perf_ok({**ok, "ps_implausible": True})[0])
from tests.benchmarks.hwbench.perf import ps_implausible  # noqa: E402
check("ps 2.1 GB vs nvidia-smi 22 GB is implausible, a close pair is not",
      ps_implausible({"size_mib": 2160}, 22400) and not ps_implausible({"size_mib": 21000}, 22400))
from tests.benchmarks.hwbench.perf import sysmem_fallback  # noqa: E402
_sf = sysmem_fallback({"vram_model_mib": 2323, "size_vram_mib": 2974, "size_mib": 2974,
                       "gpu_share_pct": 100})
check("driver sysmem fallback: Ollama says 2974 MiB resident, the card took 2323 -> spilled",
      _sf["sysmem_fallback"] and _sf["gpu_share_effective_pct"] == round(100 * 2323 / 2974))
check("...a model that really fits is not flagged (compute buffers make smi >= ps)",
      not sysmem_fallback({"vram_model_mib": 3366, "size_vram_mib": 2974, "size_mib": 2974,
                           "gpu_share_pct": 100})["sysmem_fallback"])
_full = sysmem_fallback({"vram_model_mib": 19787, "size_vram_mib": 19326, "size_mib": 19326,
                         "gpu_share_pct": 100}, idle_free_mib=19915)
check("a FULL card counts as spilled even when Ollama under-reports (gemma4:31b on 20 GB: 128 MiB left)",
      _full["sysmem_fallback"] and _full["card_full"] and _full["gpu_share_effective_pct"] < 100)
check("...16k on the full 24 GB card, 1.4 GB left, does not",
      not sysmem_fallback({"vram_model_mib": 22156, "size_vram_mib": 19326, "size_mib": 19326,
                           "gpu_share_pct": 100}, idle_free_mib=23529)["sysmem_fallback"])
from tests.benchmarks.hwbench.perf import stalled  # noqa: E402
_pts = [{"num_ctx": c, "vram_model_mib": v} for c, v in
        ((2048, 21022), (4096, 21184), (8192, 21496), (16384, 22156), (24576, 22656), (32768, 23274))]
check("sweep: growth that stalls (32k -> 48k: +48 MiB, ~1.2 GB expected) means spilled",
      stalled(_pts, 49152, 23322))
check("sweep: normal KV growth does not", not stalled(_pts[:5], 32768, 23274))
_g12 = [{"num_ctx": c, "vram_model_mib": v} for c, v in ((2048, 8900), (4096, 8936), (8192, 9374), (12288, 9459))]
check("sweep: flat growth with a half-empty card is sliding-window KV, not a spill (gemma4:12b)",
      not stalled(_g12, 16384, 9534, free_after_mib=14000))
check("...the same flat growth with 300 MiB left is a spill",
      stalled(_g12, 16384, 9534, free_after_mib=300))
check("a spilled cell counts as offloaded in the matrix (effective share wins)",
      matrix.share({"gpu_share_pct": 100, "gpu_share_effective_pct": 78}) == 78
      and not matrix._tier_fit_derived({**ok, "gpu_share_effective_pct": 78, "vram_model_mib": 2000}, 8, 1024))
check("JSON gate under 95 % is a K.O.", not matrix.quality_ok({"gate_valid_pct": 93.3})[0])
check("empty monologues over 5 % are a K.O.",
      not matrix.quality_ok({"gate_valid_pct": 100, "mono_empty_pct": 10})[0])

# ── matrix build on a synthetic ledger ─────────────────────────────────────
def judge_file(name, verdicts):
    p = tmp / f"{name}_judge.jsonl"
    p.write_text("\n".join(json.dumps({"key": f"k{i}", "verdict": v})
                           for i, v in enumerate(verdicts)), encoding="utf-8")
    return str(p)


L = Ledger(tmp / "mx.json")
ref = L.upsert_model("gemma4:31b", "sha256:aaa")
small = L.upsert_model("tiny:4b", "sha256:bbb")
V = ["CUT", "HARD_KEEP", "STAGNANT", "CUT", "KEEP_WITH_FLAG"]
qm = {"gate_valid_pct": 100, "judge_valid_pct": 100, "mono_empty_pct": 0}
env = {"gpu": "NVIDIA GeForce RTX 4090", "gpu_total_mib": 24564, "desktop_reserve_mib": 1024}
rq = L.add_run(ref, "quality", {"profile": "large", "server": matrix.DEFAULT_SERVER, "items": "sub40"},
               env=env, metrics=qm, artifacts=[judge_file("ref", V)])
L.add_score(rq["id"], scorer=matrix.JUDGE_SCORER, rubric="j", aggregate={"mean25": 23.0})
L.add_score(rq["id"], scorer=matrix.NOTES_SCORER, rubric="n", aggregate={"mean25": 21.0})
qs = L.add_run(small, "quality", {"profile": "small", "server": matrix.DEFAULT_SERVER, "items": "sub40"},
               env=env, metrics=qm, artifacts=[judge_file("small", ["CUT", "HARD_KEEP", "CUT", "CUT", "HARD_KEEP"])])
L.add_run(small, "quality", {"profile": "small", "server": "fa-1_kv-q8_0", "items": "sub40"},
          env=env, metrics=qm, artifacts=[judge_file("smallq8", ["CUT", "HARD_KEEP", "CUT", "CUT", "HARD_KEEP"])])
L.add_run(ref, "perf", {"profile": "large", "server": matrix.DEFAULT_SERVER, "num_ctx": 16384,
                        "tier_gb": "full", "num_gpu": 99}, env=env,
          metrics={**ok, "vram_model_mib": 21000})
for tier, srv in ((6, matrix.DEFAULT_SERVER), (4, "fa-1_kv-q8_0")):
    L.add_run(small, "perf", {"profile": "small", "server": srv, "num_ctx": 8192,
                              "tier_gb": tier, "num_gpu": None,
                              "tier_method": matrix.TIER_METHOD}, env=env,
              metrics={**ok, "vram_model_mib": 3000})
L.add_run(small, "perf", {"profile": "small", "server": matrix.DEFAULT_SERVER, "num_ctx": 8192,
                          "tier_gb": "full", "num_gpu": 99}, env=env,
          metrics={**ok, "vram_model_mib": 3000, "mono_gen_tok_s_median": 90})
mx = matrix.build(L, reference_ref="gemma4:31b")
check("24 GB: the reference's full-card cell counts as measured at 24 GB",
      mx["tiers"]["24"]["recommended"]["model"] == "gemma4:31b"
      and not mx["tiers"]["24"]["recommended"]["derived"])
check("a model with no tier spot check is NOT derived onto smaller tiers",
      mx["tiers"]["20"]["recommended"] is None or
      mx["tiers"]["20"]["recommended"]["model"] != "gemma4:31b")
check("an unscored model is provisional; agreement is exported as a diagnostic only",
      mx["tiers"]["6"]["recommended"]["quality"]["agreement"]["exact_pct"] == 60.0
      and mx["tiers"]["6"]["recommended"]["quality"]["provisional"]
      and mx["tiers"]["6"]["recommended"]["quality"]["score25"] is None)
check("quality = judge and notes, weighted",
      mx["tiers"]["24"]["recommended"]["quality"]["score25"] == round(
          matrix.JUDGE_WEIGHT * 23.0 + (1 - matrix.JUDGE_WEIGHT) * 21.0, 2))
check("q8 KV with unchanged verdicts serves the 4 GB tier and inherits the agreement",
      mx["tiers"]["4"]["recommended"]["model"] == "tiny:4b"
      and mx["tiers"]["4"]["recommended"]["quality"]["agreement"]["exact_pct"] == 60.0)
check("the spot check at 6 GB lets the full-card cell derive 8 GB and up",
      mx["tiers"]["8"]["recommended"]["model"] == "tiny:4b"
      and mx["tiers"]["8"]["recommended"]["derived"])
L.add_score(qs["id"], scorer=matrix.NOTES_SCORER, rubric="n", aggregate={"mean25": 22.0})
check("card notes alone leave a model provisional (the judge decides deletions)",
      matrix.build(L)["tiers"]["8"]["recommended"]["quality"]["provisional"])
L.add_score(qs["id"], scorer=matrix.JUDGE_SCORER, rubric="j", aggregate={"mean25": 24.0})
mx_s = matrix.build(L)
check("a judge-scored model is no longer provisional",
      not mx_s["tiers"]["8"]["recommended"]["quality"]["provisional"])
check("agreement with the production model does not rank: 60 % agreement but better reasoning wins 24 GB",
      mx_s["tiers"]["24"]["recommended"]["model"] == "tiny:4b")
check("the matrix renders", "| 24 GB | gemma4:31b" in matrix.render_md(mx))

# Recommendation gates: comfort, then the quality window.
def _opt(name, score, tok, judge, prov=False):
    return {"model": name, "quality": {"score25": score, "provisional": prov},
            "expected": {"gen_tok_s": tok, "judge_s": judge}}
slow, fast = _opt("big", 22.2, 6.1, 44.0), _opt("moe", 20.1, 95.6, 3.6)
check("comfort gate: a 44 s-per-verdict package is only an alternative while a comfortable one exists",
      matrix.recommend([slow, fast])["model"] == "moe" and not slow["comfortable"])
check("comfort gate falls away when nothing on the card is comfortable",
      matrix.recommend([slow, _opt("big2", 21.0, 5.0, 60.0)])["model"] == "big")
check("quality window: within 1.5 points the faster package wins",
      matrix.recommend([_opt("a", 22.0, 30, 8), _opt("b", 21.0, 90, 3)])["model"] == "b")
check("...outside it the better one stays",
      matrix.recommend([_opt("a", 22.0, 30, 8), _opt("b", 20.0, 90, 3)])["model"] == "a")
check("a scored package beats a faster provisional one",
      matrix.recommend([_opt("a", 20.0, 30, 8), _opt("p", None, 200, 1, prov=True)])["model"] == "a")

# The context sweep's measured footprints reach the export, spilled points not.
L.add_run(small, "ctx_sweep", {"server": matrix.DEFAULT_SERVER, "num_gpu": 99}, env=env,
          metrics={"points": [{"num_ctx": 2048, "vram_model_mib": 2500, "gpu_share_pct": 100},
                              {"num_ctx": 8192, "vram_model_mib": 3000, "gpu_share_pct": 100},
                              {"num_ctx": 131072, "vram_model_mib": 24000, "gpu_share_pct": 80}],
                   "max_resident_ctx": 8192})
fp = matrix.build(L)["footprints"]["tiny:4b"][matrix.DEFAULT_SERVER]
check("sweep: resident windows exported, the spilled one left out",
      fp["mib_by_ctx"] == {"2048": 2500, "8192": 3000} and fp["max_resident_ctx"] == 8192)

# KV quantisation that MOVES verdicts disqualifies that server setting.
L2 = Ledger(tmp / "mx2.json")
m2 = L2.upsert_model("t:1b", "sha256:ccc")
L2.add_run(m2, "quality", {"profile": "small", "server": matrix.DEFAULT_SERVER}, env=env,
           metrics=qm, artifacts=[judge_file("t2", V)])
L2.add_run(m2, "quality", {"profile": "small", "server": "fa-1_kv-q4_0"}, env=env,
           metrics=qm, artifacts=[judge_file("t2q4", ["HARD_KEEP"] * 5)])
L2.add_run(m2, "perf", {"profile": "small", "server": "fa-1_kv-q4_0", "num_ctx": 8192,
                        "tier_gb": 4, "num_gpu": None, "tier_method": matrix.TIER_METHOD},
           env=env, metrics={**ok, "vram_model_mib": 2000})
check("KV quantisation that changes verdicts is a K.O.",
      matrix.build(L2, reference_ref="none")["tiers"]["4"]["recommended"] is None)

# ── simulated cards: llama.cpp fit target, not ballast ─────────────────────
from tests.benchmarks.hwbench import gpu as _gpu  # noqa: E402
info = {"free_mib": 23036, "ctx_mib": 336}
check("fit target: an 8 GB card with 1 GB desktop leaves llama-server what that card would",
      _gpu.fit_target_for(8192, 1024, info) == 23036 - (8192 - 1024 - 336) + 1024)
check("fit target never drops below llama.cpp's own margin",
      _gpu.fit_target_for(32768, 0, info) == _gpu.LLAMA_FIT_MARGIN_MIB)
check("fit target reaches the server env; the real server's env stays as it was",
      ServerConfig(fit_target_mib=9000).env()["LLAMA_ARG_FIT_TARGET"] == "9000"
      and "LLAMA_ARG_FIT_TARGET" not in ServerConfig().env()
      and ServerConfig(fit_target_mib=9000).label == ServerConfig().label)
L3 = Ledger(tmp / "mx3.json")
m3 = L3.upsert_model("t:2b", "sha256:ddd")
L3.add_run(m3, "quality", {"profile": "small", "server": matrix.DEFAULT_SERVER}, env=env,
           metrics=qm, artifacts=[judge_file("t3", V)])
L3.add_run(m3, "perf", {"profile": "small", "server": matrix.DEFAULT_SERVER, "num_ctx": 8192,
                        "tier_gb": 6, "num_gpu": 99}, env=env, metrics={**ok, "vram_model_mib": 3000})
check("a ballast-era tier cell (no tier_method) is void",
      matrix.build(L3, reference_ref="none")["tiers"]["6"]["recommended"] is None)
L3.add_run(m3, "perf", {"profile": "small", "server": matrix.DEFAULT_SERVER, "num_ctx": 8192,
                        "tier_gb": 8, "num_gpu": None, "tier_method": matrix.TIER_METHOD}, env=env,
           metrics={**ok, "vram_model_mib": 7600, "tier_overrun_mib": 432})
check("a cell that took more than the simulated card has is a K.O.",
      matrix.build(L3, reference_ref="none")["tiers"]["8"]["recommended"] is None
      and not matrix.tier_fits({**ok, "tier_overrun_mib": 432})
      and matrix.tier_fits({**ok, "tier_overrun_mib": 200}))

# ── borderline cells get measured again ────────────────────────────────────
off = {**ok, "gpu_share_pct": 18}
check("borderline: offloaded and near the tok/s K.O. line",
      matrix.borderline({**off, "mono_gen_tok_s_median": 8.2})
      and matrix.borderline({**off, "mono_gen_tok_s_median": 4.7}))
check("not borderline: clear of the line, or fully on the GPU, or failed",
      not matrix.borderline({**off, "mono_gen_tok_s_median": 30, "judge_wall_s_p90": 10})
      and not matrix.borderline({**ok, "gpu_share_pct": 100, "mono_gen_tok_s_median": 7})
      and not matrix.borderline({"status": "load_failed", "gpu_share_pct": 18}))
check("borderline: a judge p90 near its line counts too",
      matrix.borderline({**off, "mono_gen_tok_s_median": 30, "judge_wall_s_p90": 80}))
st = matrix.settle([{**off, "mono_gen_tok_s_median": g} for g in (8.2, 4.7, 7.9)])
check("settle: the median attempt wins, all attempts and the spread are kept",
      st["mono_gen_tok_s_median"] == 7.9 and len(st["attempts"]) == 3
      and st["attempt_spread_pct"] == 42.7 and st["speed_unstable"])
check("settle: close attempts are stable",
      not matrix.settle([{**off, "mono_gen_tok_s_median": g} for g in (7.0, 7.4, 7.2)])["speed_unstable"])
q = _gpu.wait_quiet_cpu(max_pct=100, window_s=0.1, timeout_s=1)
check("quiet-host wait reports its reading", q == {} or (q["host_quiet"] and "host_cpu_idle_pct" in q))

# ── subset + text helpers ──────────────────────────────────────────────────
items = [{"key": f"{c}:{i}", "category": c, "title": f"T{i}",
          "flags": {"acclaim_present": i % 3 == 0, "owner_watched": i % 4 == 0}}
         for c in ("movie", "show", "anime", "music") for i in range(20)]
sub_a = select_subset(items, {("movie", "T7")})
check("subset: 10 per category, spot-check titles first, reproducible",
      len(sub_a) == 40 and sub_a[0] == "movie:7" and sub_a == select_subset(items, {("movie", "T7")}))
check("subset covers flagged strata",
      any(next(i for i in items if i["key"] == k)["flags"]["acclaim_present"] for k in sub_a))
check("buzzwords count whole words only", buzzwords("an epic epicentre, epic.") == 2)
check("sentences split on terminal punctuation", sentences("One. Two! Three?") == 3)

# ── no download when the ledger already has everything ─────────────────────
import argparse as _ap  # noqa: E402
from tests.benchmarks.hwbench import run as _run  # noqa: E402
snapdir = tmp / "snaps"
snapdir.mkdir()
(snapdir / "evidence_2026-10-16-large.json").write_text(json.dumps({"id": "S1"}), encoding="utf-8")
_run.DATA = snapdir
_run.registry.remote_manifest = lambda ref: {"weights_digest": "sha256:feedface0000"}
PL = Ledger(tmp / "pending.json")
pk = PL.upsert_model("m:1b", "sha256:feedface0000")
cand = {"ref": "m:1b", "role": "curator", "profiles": ["large"]}
A = _ap.Namespace(no_tiers=True, phases="sweep,perf,quality", force=False, servers="default,q8",
                  reps=3, items="sub40", repull_for_kv=False)
check("pending: an empty ledger row means work", _run.pending_work(cand, A, PL))
for srv, vram in (("default", 21000), ("q8", 19500)):
    cfg = _run.SERVERS[srv]
    PL.add_run(pk, "ctx_sweep", _run.sweep_settings(cfg), env={}, metrics={})
    PL.add_run(pk, "perf", {**_run.perf_base(cfg, "large", "S1", 3), "tier_gb": "full", "num_gpu": 99},
               env={}, metrics={"vram_model_mib": vram})
PL.add_run(pk, "quality", _run.quality_settings(_run.SERVERS["default"], "large", "S1", "sub40", True),
           env={}, metrics={})
check("pending: everything but a KV pass known -> no re-pull by default",
      not _run.pending_work(cand, A, PL, kv=False))
check("pending: ...but the q8 pass (saves 1.5 GB) is still work when KV counts",
      _run.pending_work(cand, A, PL, kv=True))
AT = _ap.Namespace(**{**vars(A), "no_tiers": False})
check("pending: a tier pass is work until the model has a tier cell",
      _run.pending_work(cand, AT, PL, kv=False))
for srv in ("default", "q8"):
    PL.add_run(pk, "perf", {**_run.perf_base(_run.SERVERS[srv], "large", "S1", 3), "tier_gb": 16,
                            "num_gpu": None, "tier_method": matrix.TIER_METHOD},
               env={}, metrics={"status": "ok"})
check("pending: ...and no re-pull once every server has one",
      not _run.pending_work(cand, AT, PL, kv=False))

# ── remote host (the NAS card) ─────────────────────────────────────────────
from tests.benchmarks.hwbench import models as _models, server as _server  # noqa: E402
local_fp = fingerprint(pk, "perf", _run.perf_base(_run.SERVERS["default"], "large", "S1", 3))
_run.REMOTE_HOST = "nas"
check("remote: perf and sweep fingerprints carry the host, so a NAS cell never hides a 4090 cell",
      fingerprint(pk, "perf", _run.perf_base(_run.SERVERS["default"], "large", "S1", 3)) != local_fp
      and _run.sweep_settings(_run.SERVERS["default"])["host"] == "nas")
_run.REMOTE_HOST = ""
check("...and local fingerprints are unchanged without it",
      fingerprint(pk, "perf", _run.perf_base(_run.SERVERS["default"], "large", "S1", 3)) == local_fp)
check("remote: pull state is kept per host (a tag pulled here never marks the NAS copy as the bench's)",
      _models.state_file("nas") != _models.state_file() and "nas" in _models.state_file("nas").name)
import threading as _th  # noqa: E402
import time as _time  # noqa: E402
order = []
st_ = object.__new__(_models.Store)
st_.endpoint, st_.log, st_.preinstalled, st_.pulled = "http://x", (lambda *a: None), set(), {"a:1"}
st_.state, st_._lock, st_._results = tmp / "pulled.json", _th.Lock(), {}
st_._prefetch = {"b:1": _th.Thread(target=lambda: (_time.sleep(0.3), order.append("pull b done")))}
st_._prefetch["b:1"].start()
_saved_req = _models.httpx.request
_models.httpx.request = lambda *a, **k: order.append("delete a")
st_.cleanup("a:1")
_models.httpx.request = _saved_req
check("cleanup waits for a running prefetch before deleting (shared blobs)",
      order == ["pull b done", "delete a"])

rs = _server.RemoteServer("http://nas:30068/", "nas")
try:
    rs.ensure(_run.SERVERS["q8"])
    refused = False
except SystemExit:
    refused = True
check("remote: only the default server config can be measured", refused and rs.endpoint == "http://nas:30068")
hdr = "r b swpd free buff cache si so bi bo in cs us sy id wa st gu".split()
check("vmstat row -> CPU busy % and free+buff+cache",
      _gpu._vmstat_row(hdr, "3 0 0 1048576 524288 524288 0 0 0 0 1 1 20 10 65 5 0 0")
      == {"cpu_pct": 35, "ram_avail_mib": 2048}
      and _gpu._vmstat_row(hdr, "procs ---memory---") == {})
RA = _ap.Namespace(remote="http://nas:30068", remote_host="nas", remote_ssh="ssh nas",
                   phases="sweep,perf,quality", servers="default,q8,q4", no_tiers=False)
_saved_log, _run.log = _run.log, (lambda *a: None)
_run.remote_setup(RA)
_run.log = _saved_log
check("remote setup: quality dropped, default server only, no simulated tiers",
      RA.phases == "sweep,perf" and RA.servers == "default" and RA.no_tiers
      and _gpu.is_remote() and _run.REMOTE_HOST == "nas")
_gpu.set_remote([])
_run.REMOTE_HOST = ""

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
