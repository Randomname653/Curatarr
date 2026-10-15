"""Ledger -> decision matrix -> src/services/model_matrix.json.

For every VRAM tier the matrix names ONE recommended curator package (model
+ LLM_PROFILE + num_ctx + num_gpu + Ollama server settings) with the speed
it was measured at, plus the runners-up. The setup wizard reads it
(model_catalog.recommend_models).

Rules, in order:

1. Quality K.O. (per model x profile x server): JSON gate valid >= 95 %,
   judge valid >= 95 %, empty monologues <= 5 %.
2. Performance K.O. (per cell): the cell finished without timeout; either
   fully on the GPU, or — when partly offloaded — monologue generation
   >= MIN_GEN_TOK_S and judge p90 <= MAX_JUDGE_P90_S; no latency
   escalation beyond MAX_ESCALATION; the long-context probe did not fail.
3. A tier is served by a measured cell at that tier, or DERIVED from a
   full-card cell when the model's measured VRAM plus the desktop reserve
   fits the tier with margin AND a ballast spot check at or below that tier
   confirmed Ollama agrees (its own estimator decides, not our arithmetic).
   A full-card cell counts as measured at the bench card's own tier.
4. A quantised KV cache (q8_0 / q4_0) is only judged, not re-scored: it
   inherits the default server's quality when >= MIN_KV_AGREEMENT % of its
   verdicts are unchanged, and is a K.O. when fewer are.
5. Ranking: scored quality (Claude/owner rubric, mean of 25) when present,
   else agreement with the reference model's verdicts — marked
   ``provisional``. Ties go to the faster package, then to less VRAM.

Thresholds are module constants so a test can pin them and a change shows
up in review.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import BENCH_VERSION
from .ledger import Ledger

TIERS_GB = (4, 6, 8, 10, 12, 16, 20, 24)
MIN_GATE_VALID = 95.0
MIN_JUDGE_VALID = 95.0
MAX_MONO_EMPTY = 5.0
MIN_GEN_TOK_S = 6.0
MAX_JUDGE_P90_S = 90.0
MAX_ESCALATION = 3.0
FIT_MARGIN_MIB = 512
MIN_KV_AGREEMENT = 90.0     # % of verdicts a quantised KV cache must leave unchanged
DEFAULT_SERVER = "fa-auto_kv-f16"
EXPORT = Path(__file__).resolve().parents[3] / "src" / "services" / "model_matrix.json"


def _judge_rows(run: dict) -> dict:
    for a in run.get("artifacts") or []:
        if a.endswith("judge.jsonl") and Path(a).exists():
            return {r["key"]: r for r in map(json.loads,
                    Path(a).read_text(encoding="utf-8").splitlines()) if r.get("verdict")}
    return {}


def agreement(run: dict, ref: dict) -> dict:
    """Verdict agreement with the reference run over their common titles:
    exact verdict, and the coarser keep-vs-delete side."""
    a, b = _judge_rows(run), _judge_rows(ref)
    common = sorted(set(a) & set(b))
    if not common:
        return {}
    side = lambda v: "delete" if v in ("CUT", "STAGNANT") else ("keep" if v else None)
    exact = sum(a[k]["verdict"] == b[k]["verdict"] for k in common)
    sides = sum(side(a[k]["verdict"]) == side(b[k]["verdict"]) for k in common)
    return {"n": len(common), "exact_pct": round(100 * exact / len(common), 1),
            "side_pct": round(100 * sides / len(common), 1)}


def quality_ok(m: dict) -> tuple[bool, str]:
    if (m.get("gate_valid_pct") or 0) < MIN_GATE_VALID:
        return False, f"JSON gate {m.get('gate_valid_pct')} % < {MIN_GATE_VALID}"
    if m.get("judge_valid_pct") is not None and m["judge_valid_pct"] < MIN_JUDGE_VALID:
        return False, f"judge valid {m['judge_valid_pct']} % < {MIN_JUDGE_VALID}"
    if (m.get("mono_empty_pct") or 0) > MAX_MONO_EMPTY:
        return False, f"empty monologues {m['mono_empty_pct']} %"
    return True, ""


def perf_ok(m: dict) -> tuple[bool, str]:
    if m.get("status") != "ok":
        return False, m.get("status") or "no status"
    if m.get("ps_implausible"):
        # The GPU share comes from /api/ps; when ps under-reports the model
        # that share is meaningless, so the cell cannot vouch for a fit.
        return False, "Ollama's residency figure is implausible (ps << nvidia-smi)"
    if m.get("long_error"):
        return False, f"long-context probe: {m['long_error'][:80]}"
    if (m.get("judge_escalation_ratio") or 1) > MAX_ESCALATION:
        return False, f"latency escalation x{m['judge_escalation_ratio']}"
    if (m.get("gpu_share_pct") or 0) < 100:
        if (m.get("mono_gen_tok_s_median") or 0) < MIN_GEN_TOK_S:
            return False, f"offloaded, {m.get('mono_gen_tok_s_median')} tok/s"
        if (m.get("judge_wall_s_p90") or 1e9) > MAX_JUDGE_P90_S:
            return False, f"offloaded, judge p90 {m.get('judge_wall_s_p90')} s"
    return True, ""


def _tier_fit_derived(cell: dict, tier_gb: int, reserve_mib: int) -> bool:
    need = (cell.get("vram_model_mib") or 0) + reserve_mib + FIT_MARGIN_MIB
    return cell.get("gpu_share_pct") == 100 and need <= tier_gb * 1024


def build(ledger: Ledger, reference_ref: str = "gemma4:31b") -> dict:
    runs = ledger.runs()
    quality = [r for r in runs if r["phase"] == "quality"]
    perf = [r for r in runs if r["phase"] == "perf"]
    ref_run = next((q for q in quality if ledger.model(q["model_key"])["ref"] == reference_ref
                    and q["settings"].get("profile") == "large"
                    and q["settings"].get("items") == "sub40"), None)
    # Scored quality, keyed by quality run id.
    scored = {s["run_id"]: s["aggregate"] for s in ledger.data["scores"]}

    qual_by = {}   # (model_key, profile, server) -> verdict
    # Default-server runs first: a quantised-KV run is only a judge pass and
    # inherits the default run's score when its verdicts did not move.
    for q in sorted(quality, key=lambda q: q["settings"].get("server") != DEFAULT_SERVER):
        s = q["settings"]
        ok, why = quality_ok(q["metrics"])
        agr = agreement(q, ref_run) if ref_run and q is not ref_run else (
            {"n": 0, "exact_pct": 100.0, "side_pct": 100.0} if q is ref_run else {})
        score = scored.get(q["id"], {}).get("mean25")
        entry = {"ok": ok, "why": why, "agreement": agr, "score25": score,
                 "provisional": score is None, "run_id": q["id"]}
        base = qual_by.get((q["model_key"], s.get("profile"), DEFAULT_SERVER))
        if s.get("server") != DEFAULT_SERVER and base:
            base_run = next(r for r in quality if r["id"] == base["run_id"])
            kv = agreement(q, base_run)
            entry["kv_agreement"] = kv
            if kv.get("exact_pct", 0) >= MIN_KV_AGREEMENT and score is None:
                entry.update(score25=base["score25"], provisional=base["provisional"],
                             agreement=base["agreement"])
            elif kv.get("exact_pct", 100) < MIN_KV_AGREEMENT:
                entry.update(ok=False, why=f"KV quantisation moved verdicts "
                                          f"({kv.get('exact_pct')} % unchanged)")
        qual_by[(q["model_key"], s.get("profile"), s.get("server"))] = entry

    # Spot checks: the lowest tier at which a full-GPU cell was CONFIRMED.
    confirmed = defaultdict(lambda: 99)
    for p in perf:
        s = p["settings"]
        if isinstance(s.get("tier_gb"), int) and p["metrics"].get("gpu_share_pct") == 100:
            k = (p["model_key"], s["num_ctx"], s["server"])
            confirmed[k] = min(confirmed[k], s["tier_gb"])

    options = defaultdict(list)
    for p in perf:
        s, m = p["settings"], p["metrics"]
        q = qual_by.get((p["model_key"], s.get("profile"), s.get("server")))
        if not q or not q["ok"]:
            continue
        ok, _ = perf_ok(m)
        if not ok:
            continue
        reserve = p["env"].get("desktop_reserve_mib", 1024)
        measured_tier = None
        if isinstance(s.get("tier_gb"), int):
            measured_tier, tiers = s["tier_gb"], [s["tier_gb"]]
        else:
            # A full-card cell IS a measurement at the bench card's own tier;
            # every smaller tier it provably fits is derived from it.
            card = [t for t in TIERS_GB if t * 1024 <= p["env"].get("gpu_total_mib", 0) + 256]
            measured_tier = max(card) if card else None
            floor = confirmed[(p["model_key"], s["num_ctx"], s["server"])]
            tiers = sorted({t for t in TIERS_GB if t >= floor and _tier_fit_derived(m, t, reserve)}
                           | ({measured_tier} if measured_tier else set()))
        model = ledger.model(p["model_key"])
        for t in tiers:
            options[t].append({
                "model": model["ref"], "weights_digest": model["weights_digest"],
                "profile": s.get("profile"), "num_ctx": s["num_ctx"],
                "num_gpu": s.get("num_gpu", 99) if s.get("num_gpu") is not None else -1,
                "server": s.get("server_env", {}),
                "derived": t != measured_tier,
                "expected": {
                    "gen_tok_s": m.get("mono_gen_tok_s_median"),
                    "judge_s": m.get("judge_wall_s_median"),
                    "monologue_s": m.get("mono_wall_s_median"),
                    "vram_gb": round((m.get("vram_model_mib") or 0) / 1024, 1),
                    "gpu_share_pct": m.get("gpu_share_pct"),
                    "ram_taken_gb": round((m.get("ram_taken_mib") or 0) / 1024, 1),
                },
                "quality": {"score25": q["score25"], "provisional": q["provisional"],
                            "agreement": q["agreement"]},
                "perf_run": p["id"], "quality_run": q["run_id"],
                "measured_on": {k: p["env"].get(k) for k in ("gpu", "driver", "ollama")},
            })

    def rank(o):
        qv = o["quality"]
        primary = (qv["score25"] / 25 * 100) if qv["score25"] is not None else \
            (qv["agreement"].get("exact_pct") or 0)
        return (not qv["provisional"], primary, o["expected"]["gen_tok_s"] or 0,
                -(o["expected"]["vram_gb"] or 0))

    tiers = {}
    for t in TIERS_GB:
        opts = sorted(options.get(t, []), key=rank, reverse=True)
        # One entry per model: its best package at this tier.
        seen, uniq = set(), []
        for o in opts:
            if o["model"] not in seen:
                seen.add(o["model"])
                uniq.append(o)
        tiers[str(t)] = {"recommended": uniq[0] if uniq else None,
                         "alternatives": uniq[1:4]}
    # Measured footprints for the catalog: MiB per context window, per model
    # and server setting — what the wizard checks a card against.
    footprints: dict = {}
    for sw in (r for r in runs if r["phase"] == "ctx_sweep"):
        ref = ledger.model(sw["model_key"])["ref"]
        pts = {str(p["num_ctx"]): p["vram_model_mib"] for p in sw["metrics"].get("points", [])
               if p.get("gpu_share_pct") == 100 and p.get("vram_model_mib")}
        if pts:
            footprints.setdefault(ref, {})[sw["settings"]["server"]] = {
                "mib_by_ctx": pts, "max_resident_ctx": sw["metrics"].get("max_resident_ctx"),
                "measured_on": sw["env"].get("gpu"),
                "kv_mib_per_1k_ctx": sw["metrics"].get("kv_mib_per_1k_ctx")}
    return {"generated": datetime.now().isoformat(timespec="seconds"),
            "bench_version": BENCH_VERSION, "reference": reference_ref,
            "footprints": footprints,
            "thresholds": {"min_gate_valid": MIN_GATE_VALID, "min_judge_valid": MIN_JUDGE_VALID,
                           "min_gen_tok_s": MIN_GEN_TOK_S, "max_judge_p90_s": MAX_JUDGE_P90_S},
            "tiers": tiers}


def export(matrix: dict, path: Path = EXPORT) -> Path:
    path.write_text(json.dumps(matrix, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def render_md(matrix: dict) -> str:
    lines = [f"# Decision matrix ({matrix['generated']}, bench {matrix['bench_version']})", "",
             "| VRAM | model | profile | ctx | num_gpu | server | tok/s | judge s | quality | basis |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for t, row in matrix["tiers"].items():
        r = row["recommended"]
        if not r:
            lines.append(f"| {t} GB | — none passes — | | | | | | | | |")
            continue
        q = r["quality"]
        qs = f"{q['score25']}/25" if q["score25"] is not None else \
            f"{q['agreement'].get('exact_pct', '?')} % agree (prov.)"
        srv = ",".join(f"{k.removeprefix('OLLAMA_')}={v}" for k, v in r["server"].items()
                       if k in ("OLLAMA_KV_CACHE_TYPE", "OLLAMA_FLASH_ATTENTION"))
        lines.append(f"| {t} GB | {r['model']} | {r['profile']} | {r['num_ctx']} | {r['num_gpu']} "
                     f"| {srv} | {r['expected']['gen_tok_s']} | {r['expected']['judge_s']} | {qs} "
                     f"| {'derived' if r['derived'] else 'measured'} |")
    return "\n".join(lines) + "\n"
