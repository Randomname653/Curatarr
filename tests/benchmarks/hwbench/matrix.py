"""Ledger -> decision matrix -> src/services/model_matrix.json.

For every VRAM tier the matrix names ONE recommended curator package (model
+ LLM_PROFILE + num_ctx + num_gpu + Ollama server settings) with the speed
it was measured at, plus the runners-up. The setup wizard reads it
(model_catalog.recommend_models).

Rules, in order:

1. Quality K.O. (per model x profile x server): JSON gate valid >= 95 %,
   judge valid >= 95 %, empty monologues <= 5 %, and no degenerate judge
   (one verdict on more than MAX_VERDICT_SHARE of the titles).
2. Performance K.O. (per cell): the cell finished without timeout; either
   fully on the GPU, or — when partly offloaded — monologue generation
   >= MIN_GEN_TOK_S and judge p90 <= MAX_JUDGE_P90_S; no latency
   escalation beyond MAX_ESCALATION; the long-context probe did not fail.
3. A tier is served by a measured cell at that tier, or DERIVED from a
   full-card cell when the model's measured VRAM plus the desktop reserve
   fits the tier with margin AND a spot check at or below that tier
   confirmed llama.cpp agrees (its own fit decides, not our arithmetic).
   A full-card cell counts as measured at the bench card's own tier.
   Tier cells count only when simulated with TIER_METHOD: the earlier
   CUDA-ballast cells are void (WDDM paged the ballast out), and a cell
   that took more than the simulated card had (TIER_OVERRUN_TOL_MIB) is a
   performance K.O. — that card would spill or fail the load.
4. A quantised KV cache (q8_0 / q4_0) is only judged, not re-scored: it
   inherits the SAME model's default-server quality when >= MIN_KV_AGREEMENT
   % of its own verdicts are unchanged (self-consistency, not agreement with
   another model), and is a K.O. when fewer are.
5. Ranking: the scored quality of the model's own reasoning, never its
   agreement with the current production model (Don, 2026-10-20: "a new
   model may be RIGHT where the old one is wrong"). Quality = JUDGE_WEIGHT x
   the judge-argumentation score (faith / law / logic / judgment / clarity of
   the pillar reasoning, scorer JUDGE_SCORER) + the rest x the card-note
   score (NOTES_SCORER). A package without a judge score is ``provisional``
   and ranks after every scored one. Agreement is still exported, as a
   diagnostic only. Ties go to the faster package, then to less VRAM.
6. Recommendation gates (``recommend``): within QUALITY_WINDOW of the best
   score the faster package wins, and a package slower than COMFORT_JUDGE_S
   per verdict is only an alternative while a comfortable one exists.

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
MIN_KV_AGREEMENT = 90.0     # % of its own verdicts a quantised KV cache must leave unchanged
JUDGE_SCORER = "claude-judge"   # rubric judge-argument-2026-10
NOTES_SCORER = "claude"         # rubric 5-axis-2026-08 (card notes)
JUDGE_WEIGHT = 0.6              # the judge decides deletions; the notes are what people read
QUALITY_WINDOW = 1.5            # points of 25 within which the faster package wins
COMFORT_JUDGE_S = 20.0          # median seconds per judge verdict for a recommendation
MAX_VERDICT_SHARE = 0.75        # one verdict on more titles than this = a degenerate judge
MIN_DEGENERATE_N = 10           # too few verdicts to call it either way
DEFAULT_SERVER = "fa-auto_kv-f16"
TIER_METHOD = "llama_fit_target"
TIER_OVERRUN_TOL_MIB = 256
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


def share(m: dict):
    """What really sits on the card: the effective share when the driver
    spilled into shared memory, else Ollama's own figure."""
    return m.get("gpu_share_effective_pct", m.get("gpu_share_pct"))


def perf_ok(m: dict) -> tuple[bool, str]:
    if m.get("status") != "ok":
        return False, m.get("status") or "no status"
    if m.get("ps_implausible"):
        # The GPU share comes from /api/ps; when ps under-reports the model
        # that share is meaningless, so the cell cannot vouch for a fit.
        return False, "Ollama's residency figure is implausible (ps << nvidia-smi)"
    if m.get("long_error"):
        return False, f"long-context probe: {m['long_error'][:80]}"
    if (m.get("tier_overrun_mib") or 0) > TIER_OVERRUN_TOL_MIB:
        return False, f"took {m['tier_overrun_mib']} MiB more than the simulated card has"
    if (m.get("judge_escalation_ratio") or 1) > MAX_ESCALATION:
        return False, f"latency escalation x{m['judge_escalation_ratio']}"
    if (share(m) or 0) < 100:
        if (m.get("mono_gen_tok_s_median") or 0) < MIN_GEN_TOK_S:
            return False, f"offloaded, {m.get('mono_gen_tok_s_median')} tok/s"
        if (m.get("judge_wall_s_p90") or 1e9) > MAX_JUDGE_P90_S:
            return False, f"offloaded, judge p90 {m.get('judge_wall_s_p90')} s"
    return True, ""


BORDERLINE_FACTOR = 1.5      # within x1.5 of a K.O. line, either side
BORDERLINE_REPEATS = 2       # extra measurements of a borderline cell
UNSTABLE_SPREAD_PCT = 35.0


def borderline(m: dict) -> bool:
    """An offloaded cell whose speed sits close enough to a K.O. line that
    host noise can flip it. Offloaded layers run on the CPU, and the same
    granite4.1:8b 4 GB cell measured 8.2 and 4.7 tok/s (2026-10-19) — one
    pass, one side of MIN_GEN_TOK_S."""
    if m.get("status") != "ok" or (share(m) or 0) >= 100:
        return False
    gen, p90 = m.get("mono_gen_tok_s_median"), m.get("judge_wall_s_p90")
    near_gen = gen is not None and MIN_GEN_TOK_S / BORDERLINE_FACTOR <= gen <= MIN_GEN_TOK_S * BORDERLINE_FACTOR
    near_p90 = p90 is not None and MAX_JUDGE_P90_S / BORDERLINE_FACTOR <= p90 <= MAX_JUDGE_P90_S * BORDERLINE_FACTOR
    return near_gen or near_p90


def settle(attempts: list[dict]) -> dict:
    """The median attempt by generation speed, carrying every attempt's
    numbers and their spread."""
    ok = [a for a in attempts if a.get("status") == "ok"] or attempts
    ranked = sorted(ok, key=lambda a: a.get("mono_gen_tok_s_median") or 0)
    pick = dict(ranked[len(ranked) // 2])
    gens = [a.get("mono_gen_tok_s_median") or 0 for a in ok]
    spread = round(100 * (max(gens) - min(gens)) / max(gens), 1) if max(gens) else None
    pick["attempts"] = [{k: a.get(k) for k in ("status", "mono_gen_tok_s_median", "judge_wall_s_p90",
                                               "cpu_mean_pct", "host_cpu_idle_pct", "ram_taken_mib")}
                        for a in attempts]
    pick["attempt_spread_pct"] = spread
    pick["speed_unstable"] = bool(spread is not None and spread > UNSTABLE_SPREAD_PCT)
    return pick


def tier_fits(m: dict) -> bool:
    """A simulated-card cell that ran fully on the GPU within the card."""
    return (m.get("status") == "ok" and share(m) == 100
            and (m.get("tier_overrun_mib") or 0) <= TIER_OVERRUN_TOL_MIB)


def valid_tier_cell(run: dict) -> bool:
    """Tier cells of a method that really shrank the card."""
    return run["settings"].get("tier_method") == TIER_METHOD


def _tier_fit_derived(cell: dict, tier_gb: int, reserve_mib: int) -> bool:
    need = (cell.get("vram_model_mib") or 0) + reserve_mib + FIT_MARGIN_MIB
    return share(cell) == 100 and need <= tier_gb * 1024


def recommend(opts: list[dict]) -> dict | None:
    """The package a card gets recommended, from its per-model best packages
    (Don, 2026-10-20). Two gates over the quality ranking:

    * comfort - a package over COMFORT_JUDGE_S per judge verdict is only an
      alternative, unless nothing on the card is comfortable (gemma4:31b on
      16 GB: 6 tok/s, 44 s a verdict, best quality);
    * quality window - among packages within QUALITY_WINDOW points of the
      best, the fastest wins (a 2-point quality lead is real, a 0.3 lead is
      scoring noise, ten times the speed is not).

    Scored packages always beat provisional ones."""
    if not opts:
        return None
    for o in opts:
        o["comfortable"] = (o["expected"]["judge_s"] or 1e9) <= COMFORT_JUDGE_S
    pool = [o for o in opts if not o["quality"]["provisional"]] or opts
    pool = [o for o in pool if o["comfortable"]] or pool
    best = max(o["quality"]["score25"] or 0 for o in pool)
    window = [o for o in pool if (o["quality"]["score25"] or 0) >= best - QUALITY_WINDOW]
    return max(window, key=lambda o: o["expected"]["gen_tok_s"] or 0)


def degenerate(run: dict) -> str:
    """A judge that hands nearly every title the same verdict is not judging:
    llama3.2:3b called 31 of 40 KEEP_WITH_FLAG, granite4.2:3b 34 of 40
    STAGNANT (2026-10-19). Empty when the verdicts are spread."""
    rows = _judge_rows(run)
    if len(rows) < MIN_DEGENERATE_N:
        return ""
    counts: dict = defaultdict(int)
    for r in rows.values():
        counts[r["verdict"]] += 1
    verdict, n = max(counts.items(), key=lambda kv: kv[1])
    if n / len(rows) > MAX_VERDICT_SHARE:
        return f"degenerate judge: {n} of {len(rows)} verdicts are {verdict}"
    return ""


def quality_score(by_scorer: dict) -> dict:
    """The ranking quality of one quality run from its scores: judge
    argumentation and card notes, weighted; provisional without a judge score."""
    judge = (by_scorer.get(JUDGE_SCORER) or {}).get("mean25")
    notes = (by_scorer.get(NOTES_SCORER) or {}).get("mean25")
    if judge is not None and notes is not None:
        score = round(JUDGE_WEIGHT * judge + (1 - JUDGE_WEIGHT) * notes, 2)
    else:
        score = judge if judge is not None else notes
    return {"score25": score, "judge25": judge, "notes25": notes, "provisional": judge is None}


def build(ledger: Ledger, reference_ref: str = "gemma4:31b") -> dict:
    runs = ledger.runs()
    quality = [r for r in runs if r["phase"] == "quality"]
    perf = [r for r in runs if r["phase"] == "perf"
            and (not isinstance(r["settings"].get("tier_gb"), int) or valid_tier_cell(r))]
    ref_run = next((q for q in quality if ledger.model(q["model_key"])["ref"] == reference_ref
                    and q["settings"].get("profile") == "large"
                    and q["settings"].get("items") == "sub40"), None)
    # Scored quality per quality run id and scorer.
    scored: dict = defaultdict(dict)
    for s in ledger.data["scores"]:
        scored[s["run_id"]][s["scorer"]] = s["aggregate"]

    qual_by = {}   # (model_key, profile, server) -> verdict
    # Default-server runs first: a quantised-KV run is only a judge pass and
    # inherits the default run's score when its verdicts did not move.
    for q in sorted(quality, key=lambda q: q["settings"].get("server") != DEFAULT_SERVER):
        s = q["settings"]
        ok, why = quality_ok(q["metrics"])
        degen = degenerate(q) if ok else ""
        if degen:
            ok, why = False, degen
        agr = agreement(q, ref_run) if ref_run and q is not ref_run else (
            {"n": 0, "exact_pct": 100.0, "side_pct": 100.0} if q is ref_run else {})
        entry = {"ok": ok, "why": why, "agreement": agr, "run_id": q["id"],
                 **quality_score(scored.get(q["id"], {}))}
        score = entry["score25"]
        base = qual_by.get((q["model_key"], s.get("profile"), DEFAULT_SERVER))
        if s.get("server") != DEFAULT_SERVER and base:
            base_run = next(r for r in quality if r["id"] == base["run_id"])
            kv = agreement(q, base_run)
            entry["kv_agreement"] = kv
            if kv.get("exact_pct", 0) >= MIN_KV_AGREEMENT and score is None:
                entry.update({k: base[k] for k in ("score25", "judge25", "notes25",
                                                   "provisional", "agreement")})
            elif kv.get("exact_pct", 100) < MIN_KV_AGREEMENT:
                entry.update(ok=False, why=f"KV quantisation moved verdicts "
                                          f"({kv.get('exact_pct')} % unchanged)")
        qual_by[(q["model_key"], s.get("profile"), s.get("server"))] = entry

    # Spot checks: the lowest tier at which a full-GPU cell was CONFIRMED.
    confirmed = defaultdict(lambda: 99)
    for p in perf:
        s = p["settings"]
        if isinstance(s.get("tier_gb"), int) and tier_fits(p["metrics"]):
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
                    "gpu_share_pct": share(m),
                    "sysmem_fallback": bool(m.get("sysmem_fallback")),
                    "speed_unstable": bool(m.get("speed_unstable")),
                    "ram_taken_gb": round((m.get("ram_taken_mib") or 0) / 1024, 1),
                },
                "quality": {"score25": q["score25"], "judge25": q["judge25"],
                            "notes25": q["notes25"], "provisional": q["provisional"],
                            "agreement": q["agreement"]},
                "perf_run": p["id"], "quality_run": q["run_id"],
                "measured_on": {k: p["env"].get(k) for k in ("gpu", "driver", "ollama")},
            })

    def rank(o):
        qv = o["quality"]
        return (not qv["provisional"], qv["score25"] or 0, o["expected"]["gen_tok_s"] or 0,
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
        rec = recommend(uniq)
        tiers[str(t)] = {"recommended": rec,
                         "alternatives": [o for o in uniq if o is not rec][:3]}
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
                # Sweeps cut by the pre-5b4ce76 stall rule: more context fits.
                "max_resident_ctx_lower_bound": bool(sw["metrics"].get("max_resident_ctx_lower_bound")),
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
        qs = (f"{q['score25']}/25 (judge {q['judge25']}, notes {q['notes25']})"
              + (" prov." if q["provisional"] else "")) if q["score25"] is not None else "unscored"
        srv = ",".join(f"{k.removeprefix('OLLAMA_')}={v}" for k, v in r["server"].items()
                       if k in ("OLLAMA_KV_CACHE_TYPE", "OLLAMA_FLASH_ATTENTION"))
        lines.append(f"| {t} GB | {r['model']} | {r['profile']} | {r['num_ctx']} | {r['num_gpu']} "
                     f"| {srv} | {r['expected']['gen_tok_s']} | {r['expected']['judge_s']} | {qs} "
                     f"| {'derived' if r['derived'] else 'measured'} |")
    return "\n".join(lines) + "\n"
