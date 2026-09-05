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
    VRAM_HEADROOM_GB, recommend_models)

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
check("12 GB: no verified curator fits — the floor note says so honestly",
      not any(c["fits"] for c in r12["curator"])
      and r12["floor_note"] is not None
      and "untested" in r12["floor_note"])

r_unknown = recommend_models(None, set())
check("no VRAM info: entries carry no fit verdict (frontend asks instead)",
      all(c["fits"] is None for c in r_unknown["curator"]))

r24b = recommend_models(24.0, {"gemma4:26b"})
check("an installed model that fits outranks a not-installed one",
      r24b["curator"][0]["model"] == "gemma4:26b"
      and r24b["curator"][0]["installed"] is True)

# The qwen3.6 lesson encoded: fitting means weights + context headroom,
# never weights alone.
_r195 = recommend_models(19.5, set())
_g31 = next(c for c in _r195["curator"] if c["model"] == "gemma4:31b")
check("headroom is demanded on top of the weight footprint",
      VRAM_HEADROOM_GB >= 2.0 and _g31["fits"] is False)
check("...and the fitting fallback is sorted ahead of the non-fitting lead",
      _r195["curator"][0]["model"] == "gemma4:26b"
      and _r195["curator"][0]["fits"] is True)

# ── wiring: the wizard actually consumes the catalog ───────────────────────

_setup = (Path(__file__).resolve().parents[1]
          / "src/routers/setup.py").read_text(encoding="utf-8")
check("the /recommend route serves the catalog",
      "recommend_models" in _setup and '"/recommend"' in _setup)
check("a GPU probe route exists and is user-triggered, not automatic",
      '"/gpu"' in _setup and "detect_gpu" in _setup)
check("a warm-up route exists for the post-bake reality check",
      '"/warmup"' in _setup and "warmup_check" in _setup)

_wiz = (Path(__file__).resolve().parents[1]
        / "src/services/setup_wizard.py").read_text(encoding="utf-8")
check("the warm-up measures GPU residency, not just latency",
      "size_vram" in _wiz and "cpu_spill" in _wiz)

_html = (Path(__file__).resolve().parents[1]
         / "frontend/index.html").read_text(encoding="utf-8")
check("the wizard offers detection AND a manual VRAM picker",
      "detectGpu()" in _html and 's-vram' in _html)
check("untested installed models are labeled, never silently recommended",
      "untested (installed on server)" in _html)
check("the raw installed-list no longer overwrites the recommended selects",
      "refreshModelRecs()" in _html
      and "r.models.map(m=>`<option" not in _html)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
