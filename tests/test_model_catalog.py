"""The wizard's model catalog may never drift from the benchmarks.

    python tests/test_model_catalog.py

The setup wizard recommends models from ``src/services/model_catalog.py``.
Those entries claim to be bench-verified — this suite makes the claim
enforceable: every ``verified`` curator entry must have a row in
``tests/benchmarks/model_baselines.csv`` whose verdict_role does not
disqualify it, and no model the bench DISQUALIFIED may appear as a
recommendation. A newer bench run that re-roles a model must be reflected
in the catalog deliberately, or this suite goes red.
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.frontend_files import everything as _frontend_everything  # index.html + js/app.js

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


from src.services.model_catalog import (
    CURATOR_MODELS, SUMMARIZER_MODELS, EMBEDDING_MODELS, PITCHER_MODELS, DISQUALIFIED,
    VRAM_HEADROOM_GB, MATRIX_PATH, bandwidth, hardware_package, load_matrix,
    measured_need_gb, need_gb, recommend_models, tier_for)

# ── drift against the bench CSV ────────────────────────────────────────────

_csv = Path(__file__).resolve().parent / "benchmarks" / "model_baselines.csv"
rows = {}
with _csv.open(encoding="utf-8") as f:
    for row in csv.DictReader(f):
        rows[row["model"]] = row

for e in CURATOR_MODELS:
    if not e.get("verified"):
        continue
    row = rows.get(e["model"])
    check(f"verified curator {e['model']} has a bench row", row is not None)
    if row:
        check(f"...and the bench did not disqualify it",
              "DISQUALIFIED" not in (row.get("verdict_role") or ""))

for name in DISQUALIFIED:
    row = rows.get(name)
    check(f"disqualified {name} matches the bench's own verdict",
          row is not None and "DISQUALIFIED" in (row.get("verdict_role") or "")
          or row is not None and "RED" in (row.get("stress_gate") or "")
          or row is None)
    check(f"...and never appears as a catalog recommendation",
          name not in [e["model"] for e in
                       CURATOR_MODELS + SUMMARIZER_MODELS + EMBEDDING_MODELS])

for e in PITCHER_MODELS:
    if e.get("verified"):
        row = rows.get(e["model"])
        check(f"verified pitcher {e['model']} has a bench row", row is not None)
        check(f"...and the bench called it a pipeline-split candidate",
              row is not None and "pipeline" in (row.get("verdict_role") or "").lower())
check("recommendation carries the pitcher role",
      "pitcher" in recommend_models(24.0) and recommend_models(24.0)["pitcher"][0]["fits"] is True)
check("12 GB: the pitcher note explains single-bake mode",
      recommend_models(12.0)["pitcher_note"] is not None)

check("the production base is the catalog's first curator entry",
      CURATOR_MODELS[0]["model"] == "gemma4:31b"
      and "PRODUCTION" in (rows.get("gemma4:31b", {}).get("verdict_role") or ""))

# ── recommendation logic ───────────────────────────────────────────────────

r24 = recommend_models(24.0, {"gemma4:31b"})
check("24 GB: the production base fits and leads",
      r24["curator"][0]["model"] == "gemma4:31b"
      and r24["curator"][0]["fits"] is True)
check("24 GB: no floor note", r24["floor_note"] is None)
check("installed models are marked", r24["curator"][0]["installed"] is True)

r12 = recommend_models(12.0, set())
check("12 GB: a hardware-matrix package fits (gemma4:e4b-it-qat), no floor note",
      any(c["fits"] and c["model"] == "gemma4:e4b-it-qat" for c in r12["curator"])
      and r12["floor_note"] is None)
r4 = recommend_models(4.0, set())
check("4 GB: no verified curator fits on the estimate alone — the floor note says so honestly",
      not any(c["fits"] for c in r4["curator"])
      and r4["floor_note"] is not None
      and "untested" in r4["floor_note"])

r_unknown = recommend_models(None, set())
check("no VRAM info: entries carry no fit verdict (frontend asks instead)",
      all(c["fits"] is None for c in r_unknown["curator"]))

r24b = recommend_models(24.0, {"gemma4:26b"})
check("an installed model that fits outranks a not-installed one",
      r24b["curator"][0]["model"] == "gemma4:26b"
      and r24b["curator"][0]["installed"] is True)

# The qwen3.6 lesson encoded: fitting means weights + context headroom,
# never weights alone.
_r195 = recommend_models(22.0, set())
_g31 = next(c for c in _r195["curator"] if c["model"] == "gemma4:31b")
check("headroom is demanded on top of the weight footprint",
      VRAM_HEADROOM_GB >= 2.0 and _g31["fits"] is False)
check("...and the fitting fallback is sorted ahead of the non-fitting lead",
      _r195["curator"][0]["model"] == "gemma4:26b"
      and _r195["curator"][0]["fits"] is True)

# 2026-10-15: the catalog said 15 GB for gemma4:26b while its q4 file is
# 18.6 GB, so 18 GB cards were told it fits and then spilled to the CPU.
check("no catalog figure is below the model's installed size",
      next(c for c in CURATOR_MODELS if c["model"] == "gemma4:26b")["vram_gb"] >= 18.6
      and next(c for c in CURATOR_MODELS if c["model"] == "gemma4:31b")["vram_gb"] >= 19.9)
check("18 GB: the 26b no longer claims to fit",
      not next(c for c in recommend_models(18.0)["curator"] if c["model"] == "gemma4:26b")["fits"])

# ── the hardware matrix (bench export) ─────────────────────────────────────
_mx = {"generated": "2026-10-20T00:00:00", "bench_version": "t", "tiers": {
    "8": {"recommended": {"model": "gemma4:26b", "profile": "small", "num_ctx": 8192,
                          "num_gpu": -1, "server": {"OLLAMA_KV_CACHE_TYPE": "q8_0"},
                          "expected": {"gen_tok_s": 12.0, "judge_s": 20.0, "gpu_share_pct": 60},
                          "quality": {"score25": None, "provisional": True,
                                      "agreement": {"exact_pct": 88.0}},
                          "measured_on": {"gpu": "NVIDIA GeForce RTX 4090"}},
          "alternatives": []},
    "12": {"recommended": None, "alternatives": []},
    "24": {"recommended": {"model": "gemma4:31b", "profile": "large", "num_ctx": 16384,
                           "num_gpu": 99, "server": {},
                           "expected": {"gen_tok_s": 33.0, "judge_s": 7.0, "gpu_share_pct": 100},
                           "quality": {"score25": 21.4, "provisional": False, "agreement": {}},
                           "measured_on": {"gpu": "NVIDIA GeForce RTX 4090"}},
           "alternatives": []}}}
check("tier: a 12 GB card reported as 11.99 GB reaches the 12 GB tier",
      tier_for(11.99, _mx["tiers"]) == "12")
check("tier: 6 GB is below every tier -> none", tier_for(6.0, _mx["tiers"]) is None)
check("bandwidth: the longest card name wins",
      bandwidth("NVIDIA GeForce RTX 4070 Ti SUPER") == 672
      and bandwidth("NVIDIA GeForce RTX 4070") == 504 and bandwidth("Radeon") is None)
_p = hardware_package(9.0, "NVIDIA GeForce RTX 3060", {"gemma4:26b"}, matrix=_mx)
check("package: 9 GB gets the 8 GB tier's package, marked installed",
      _p["tier_gb"] == 8 and _p["recommended"]["model"] == "gemma4:26b"
      and _p["recommended"]["installed"] is True)
check("package: no speed estimate for a partly offloaded model (RAM-bound, not bandwidth)",
      "estimate" not in _p)
_p24 = hardware_package(24.0, "NVIDIA GeForce RTX 3090", set(), matrix=_mx)
check("package: a fully resident model's speed is scaled by bandwidth",
      _p24["estimate"]["gen_tok_s"] == round(33.0 * 936 / 1008, 1))
check("package: an empty tier says so instead of recommending",
      hardware_package(12.0, None, set(), matrix=_mx)["recommended"] is None)
check("package: below every tier the note names the floor",
      "8 GB" in hardware_package(6.0, None, set(), matrix=_mx)["note"])
check("package: no matrix -> None, the wizard keeps the fit verdicts",
      hardware_package(24.0, None, set(), matrix={}) is None)

# System RAM: an offloaded package keeps most of its model in host RAM.
_mx4 = {"tiers": {"4": {
    "recommended": {"model": "gemma4:26b-a4b-it-qat", "server": {},
                    "expected": {"gen_tok_s": 15.2, "judge_s": 19.5, "gpu_share_pct": 8,
                                 "ram_need_gb": 19.4},
                    "quality": {"score25": 21.48, "provisional": False}},
    "alternatives": [{"model": "gemma4:e4b-it-qat", "server": {},
                      "expected": {"gen_tok_s": 17.2, "judge_s": 10.7, "gpu_share_pct": 21,
                                   "ram_need_gb": 9.6},
                      "quality": {"score25": 19.69, "provisional": False}}]}}}
_r16 = hardware_package(4.0, None, set(), matrix=_mx4, ram_gb=15.9)
check("RAM: a 16 GB host gets the alternative that fits, and the answer says why",
      _r16["recommended"]["model"] == "gemma4:e4b-it-qat"
      and _r16["ram_fallback"] == {"instead_of": "gemma4:26b-a4b-it-qat", "needs_gb": 19.4, "host_gb": 15.9}
      and _r16["alternatives"][0]["model"] == "gemma4:26b-a4b-it-qat")
_r32 = hardware_package(4.0, None, set(), matrix=_mx4, ram_gb=31.9)
check("RAM: a 32 GB host keeps the better package",
      _r32["recommended"]["model"] == "gemma4:26b-a4b-it-qat" and _r32["ram_fallback"] is None)
check("RAM: unknown host RAM changes nothing",
      hardware_package(4.0, None, set(), matrix=_mx4)["recommended"]["model"] == "gemma4:26b-a4b-it-qat")
_r8 = hardware_package(4.0, None, set(), matrix=_mx4, ram_gb=8.0)
check("RAM: nothing fits -> keep the recommendation but warn",
      _r8["recommended"]["model"] == "gemma4:26b-a4b-it-qat"
      and _r8["ram_fallback"]["instead_of"] is None and _r8["ram_fallback"]["host_gb"] == 8.0)
check("the wizard sends the host RAM and names the fallback",
      "ram_gb: state.setupData.ram_gb" in _frontend_everything()
      and "ram_fallback" in _frontend_everything())

# Measured beats estimated: the sweep's footprint at the role's context.
_fp = {"footprints": {"gemma4:26b": {"fa-auto_kv-f16": {
    "mib_by_ctx": {"8192": 17000, "16384": 18400, "32768": 21200}}}}}
_g26 = next(c for c in CURATOR_MODELS if c["model"] == "gemma4:26b")
check("measured need = footprint at the covering window + reserve + margin",
      measured_need_gb("gemma4:26b", 16384, _fp) == round(18400 / 1024 + 1.5, 1))
check("a window larger than any that fit has no measured need",
      measured_need_gb("gemma4:26b", 65536, _fp) is None)
check("need_gb says which basis it used",
      need_gb(_g26, "curator", _fp)[1] == "measured"
      and need_gb(_g26, "curator", None) == (round(_g26["vram_gb"] + VRAM_HEADROOM_GB, 1), "estimated"))
check("every recommendation row carries need_gb + fit_basis",
      all("need_gb" in r and r["fit_basis"] in ("measured", "estimated")
          for r in recommend_models(24.0)["curator"]))

_shipped = load_matrix()
if _shipped:
    _names = {c["model"] for c in CURATOR_MODELS}
    for _t, _row in (_shipped.get("tiers") or {}).items():
        for _o in [_row.get("recommended")] + (_row.get("alternatives") or []):
            if _o:
                check(f"matrix {_t} GB: {_o['model']} is a deliberate catalog entry",
                      _o["model"] in _names)
else:
    check("no matrix shipped yet -> the wizard runs on the catalog alone",
          not MATRIX_PATH.exists())

# ── wiring: the wizard actually consumes the catalog ───────────────────────

_setup = (Path(__file__).resolve().parents[1]
          / "src/routers/setup.py").read_text(encoding="utf-8")
check("the /recommend route serves the catalog",
      "recommend_models" in _setup and '"/recommend"' in _setup)
check("the recommendation passes the GPU name through (bandwidth estimate)",
      "req.gpu_name" in _setup)
check("a GPU probe route exists and is user-triggered, not automatic",
      '"/gpu"' in _setup and "detect_gpu" in _setup)
check("a warm-up route exists for the post-bake reality check",
      '"/warmup"' in _setup and "warmup_check" in _setup)

_wiz = (Path(__file__).resolve().parents[1]
        / "src/services/setup_wizard.py").read_text(encoding="utf-8")
check("the warm-up measures GPU residency, not just latency",
      "size_vram" in _wiz and "cpu_spill" in _wiz)
check("the GPU probe reads FREE memory too (a game shrinks the card)",
      "memory.free" in _wiz and "busy_note" in _wiz)

_html = _frontend_everything()
check("the wizard offers detection AND a manual VRAM picker",
      "detectGpu()" in _html and 's-vram' in _html)
check("the wizard labels a need as measured or estimate, not a flat +3",
      "e.need_gb" in _html and "e.vram_gb + 3" not in _html)
check("the wizard renders the bench package",
      "renderHardwarePackage(r.package)" in _html and "gpu_name" in _html)
check("untested installed models are labeled, never silently recommended",
      "untested (installed on server)" in _html)
check("the raw installed-list no longer overwrites the recommended selects",
      "refreshModelRecs()" in _html
      and "r.models.map(m=>`<option" not in _html)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
