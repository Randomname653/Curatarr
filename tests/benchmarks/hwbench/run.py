"""Hardware-matrix bench — the command line.

    python -m tests.benchmarks.hwbench.run plan            # no GPU: what would run, what is known
    python -m tests.benchmarks.hwbench.run run [--only a,b] [--phases perf,quality]
    python -m tests.benchmarks.hwbench.run status          # ledger summary
    python -m tests.benchmarks.hwbench.run matrix [--export]
    python -m tests.benchmarks.hwbench.run score-sheets --refs a,b [--kind mono|chat]
    python -m tests.benchmarks.hwbench.run record-scores --csv file.csv

``run`` refuses to start while the owner's Ollama holds a model, the
Curatarr app answers on its port, or the GPU is busy (a game) — numbers
taken then are worthless (gpu-shared-with-gaming). Run it detached:
Bash-tool ``run_in_background`` with ``python -u`` (BENCHMARK_RUNBOOK.md).

Per model, per Ollama server config (flash attention / KV cache type):
  perf     full card -> tightest tier it fits (ballast spot check, so
           Ollama's own estimator agrees) -> offload descent with
           num_gpu auto until too slow, plus one forced num_gpu=99 cell
           below the fit (the trap a small card hits with the default).
  quality  on the default server: gate + judge + monologue + chat over the
           frozen evidence; on a quantised-KV server only gate + judge, and
           only when that KV setting lets the model fit a smaller tier.
Every cell is checked against the ledger first and skipped when its
fingerprint is already there (``--force`` re-measures).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

from . import BENCH_VERSION, gpu, matrix, models, perf, quality, registry, server
from .ledger import Ledger, fingerprint

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
DATA = HERE.parent / "hwbench_data"
OWNER_OLLAMA = os.environ.get("HWBENCH_OWNER_OLLAMA", "http://127.0.0.1:11434")
APP_URL = os.environ.get("HWBENCH_APP_URL", "http://127.0.0.1:8000")
PROFILE_CTX = {"large": 16384, "small": 8192}
SERVERS = {
    "default": server.ServerConfig("auto", "f16"),
    "q8": server.ServerConfig("1", "q8_0"),
    "q4": server.ServerConfig("1", "q4_0"),
}
_LOG = None


def log(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    if _LOG:
        with _LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%H:%M:%S} {line}\n")


def load_candidates(only: str = "") -> tuple[list[dict], str]:
    d = json.loads((HERE / "candidates.json").read_text(encoding="utf-8"))
    cands = d["candidates"]
    if only:
        want = {x.strip() for x in only.split(",")}
        cands = [c for c in cands if c["ref"] in want]
    return cands, d.get("reference", "gemma4:31b")


def slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9.]+", "-", s).strip("-")


# ── preconditions ────────────────────────────────────────────────────────
def preflight(force_busy: bool, busy_mib: int) -> list[str]:
    problems = []
    if server.loaded_models(OWNER_OLLAMA):
        problems.append(f"the owner's Ollama ({OWNER_OLLAMA}) holds a model — stop Curatarr first")
    try:
        httpx.get(APP_URL, timeout=3)
        problems.append(f"Curatarr answers on {APP_URL} — stop the app (it would load models mid-bench)")
    except httpx.HTTPError:
        pass
    q = gpu.query()
    with gpu.Sampler() as s:
        time.sleep(3)
    util = s.summary().get("gpu_util_mean_pct", 0)
    if not force_busy and (q["used_mib"] > busy_mib or util > 25):
        problems.append(f"GPU busy: {q['used_mib']} MiB used, {util} % load — a game? "
                        f"(--force-busy to override; timings would be contaminated)")
    return problems


def environment(bench: server.BenchServer, reserve_mib: int, prefetching: bool) -> dict:
    q = gpu.query()
    try:
        import psutil
        ram = round(psutil.virtual_memory().total / 2**30, 1)
    except ImportError:
        ram = None
    return {"gpu": q["name"], "gpu_total_mib": q["total_mib"], "driver": q["driver"],
            "ollama": bench.version(), "host_ram_gb": ram, "os": platform.platform(),
            "desktop_reserve_mib": reserve_mib, "prefetch_active": prefetching}


# ── plan ─────────────────────────────────────────────────────────────────
def cmd_plan(args) -> None:
    led = Ledger()
    cands, ref = load_candidates(args.only)
    print(f"bench {BENCH_VERSION}, reference {ref}, ledger {led.path.name}: "
          f"{len(led.data['models'])} models, {len(led.data['runs'])} runs\n")
    total = 0.0
    for c in cands:
        remote = registry.remote_manifest(c["ref"])
        local = registry.local_manifest(c["ref"])
        wd = (local or remote or {}).get("weights_digest", "")
        key = f"{c['ref']}@{wd.removeprefix('sha256:')[:12]}"
        done = len(led.runs(key)) if wd else 0
        state = []
        if not remote:
            state.append("NOT ON REGISTRY")
        if local:
            state.append("installed")
            if remote and remote["weights_digest"] != local["weights_digest"]:
                state.append("registry has NEWER weights (bench uses the installed copy)")
        size = (remote or {}).get("total_bytes", 0) / 1e9
        if remote and not local:
            total += size
        print(f"  {c['ref']:30} {c['role']:10} {size:5.1f} GB  runs {done:3}  "
              f"{', '.join(state) or 'to pull'}")
    print(f"\n  downloads needed: {total:.0f} GB (pulled one ahead, removed after)")


# ── run ──────────────────────────────────────────────────────────────────
def _snapshot_path(profile: str) -> Path | None:
    files = sorted(DATA.glob(f"evidence_*-{profile}.json"))
    return files[-1] if files else None


def _perf_facts(snap: dict, n: int = 4) -> list[str]:
    """The longest evidence blocks of the subset: the heaviest real prompts."""
    sub = set(snap.get("subset_40") or [])
    facts = sorted((i["facts"] for i in snap["items"] if i["key"] in sub), key=len, reverse=True)
    return facts[:n] or [i["facts"] for i in snap["items"][:n]]


def tightest_tier(vram_model_mib: int, reserve_mib: int, total_mib: int) -> int | None:
    for t in matrix.TIERS_GB:
        if t * 1024 >= total_mib:
            break
        if vram_model_mib + reserve_mib + matrix.FIT_MARGIN_MIB <= t * 1024:
            return t
    return None


def bench_model(c: dict, args, led: Ledger, bench: server.BenchServer,
                store: models.Store, next_ref: str | None, reserve_mib: int) -> None:
    ref = c["ref"]
    log(f"\n=== {ref} ({c['role']}) — {c['why']}")
    if store.is_installed(ref):
        local = registry.local_manifest(ref) or {}
        remote = registry.remote_manifest(ref) or {}
        if remote and local and remote["weights_digest"] != local["weights_digest"]:
            log("  installed copy differs from the registry — benching the INSTALLED weights; "
                "the bench never re-pulls an installed tag (it would replace them)")
    if not store.ready(ref):
        log(f"  SKIP: could not pull {ref}")
        return
    info = registry.local_manifest(ref) or {}
    remote = registry.remote_manifest(ref) or {}
    key = led.upsert_model(ref, info.get("weights_digest", ""),
                           source_url=registry.library_url(ref),
                           weights_bytes=info.get("weights_bytes"),
                           total_bytes=info.get("total_bytes"),
                           has_draft=info.get("has_draft"), has_projector=info.get("has_projector"),
                           registry_digest_matches=(remote.get("weights_digest") == info.get("weights_digest")) if remote else None,
                           role=c["role"], why=c["why"],
                           **{k: v for k, v in registry.show(bench.endpoint, ref).items()})
    profiles = c["profiles"] or ["small"]
    prefetched = False
    phases = set(args.phases.split(","))
    tier_by_server: dict[str, int | None] = {}

    for sname in args.servers.split(","):
        cfg = SERVERS[sname]
        bench.ensure(cfg)
        for profile in profiles:
            snap_path = _snapshot_path(profile)
            if not snap_path:
                log(f"  no evidence snapshot for profile {profile} — run snapshot_items first")
                continue
            snap = json.loads(snap_path.read_text(encoding="utf-8"))
            facts = _perf_facts(snap)
            ctx = PROFILE_CTX[profile]
            base = {"num_ctx": ctx, "profile": profile, "server": cfg.label,
                    "server_env": cfg.env(), "snapshot": snap["id"], "reps": args.reps}

            def cell(tier, num_gpu):
                s = {**base, "tier_gb": tier, "num_gpu": num_gpu}
                fp = fingerprint(key, "perf", s)
                if led.has_run(fp) and not args.force:
                    row = next(r for r in led.runs(key, "perf") if r["fingerprint"] == fp)
                    log(f"  [ledger] {cfg.label} {profile} tier {tier} gpu {num_gpu}: known")
                    return row["metrics"]
                started = datetime.now().astimezone().isoformat(timespec="seconds")
                with gpu.Ballast() as ballast, gpu.Sampler() as smp:
                    held = {}
                    if tier != "full":
                        server.unload_all(bench.endpoint)
                        held = ballast.to_free(tier * 1024 - reserve_mib)
                    m = perf.run_cell(bench.endpoint, ref, num_ctx=ctx, num_gpu=num_gpu,
                                      profile=profile, facts=facts, reps=args.reps, sampler=smp)
                    m["ballast"] = held
                server.unload_all(bench.endpoint)
                led.add_run(key, "perf", s, env=environment(bench, reserve_mib, prefetched),
                            metrics=m, started=started)   # a failed load IS the result
                log(f"  {cfg.label} {profile} ctx {ctx} tier {tier} gpu {num_gpu}: "
                    f"{m.get('status')} share {m.get('gpu_share_pct')} % "
                    f"vram {m.get('vram_model_mib')} MiB gen {m.get('mono_gen_tok_s_median')} t/s "
                    f"judge {m.get('judge_wall_s_median')} s")
                return m

            if "perf" in phases:
                full = cell("full", 99)
                if next_ref and not prefetched:
                    store.prefetch(next_ref)       # after the cold load: no disk contention there
                    prefetched = True
                total = gpu.query()["total_mib"]
                fit = None
                if full.get("gpu_share_pct") == 100:
                    t = tightest_tier(full.get("vram_model_mib") or 0, reserve_mib, total)
                    for _ in range(2):
                        if t is None:
                            break
                        spot = cell(t, 99)
                        if spot.get("gpu_share_pct") == 100 and spot.get("status") == "ok":
                            fit = t
                            break
                        ups = [x for x in matrix.TIERS_GB if x > t and x * 1024 < total]
                        t = ups[0] if ups else None
                tier_by_server[(cfg.label, profile)] = fit
                below = [t for t in matrix.TIERS_GB if t * 1024 < total and (fit is None or t < fit)]
                if below:
                    cell(below[-1], 99)              # forced full-GPU on a too-small card
                misses = 0
                for t in reversed(below):
                    m = cell(t, None)
                    ok, _ = matrix.perf_ok(m)
                    if m.get("status") == "load_failed" or (m.get("gpu_share_pct") or 0) < 20:
                        break
                    misses += 0 if ok else 1
                    if misses >= 2:
                        break

            if "quality" in phases and c["role"] == "curator":
                default_fit = tier_by_server.get((SERVERS["default"].label, profile))
                this_fit = tier_by_server.get((cfg.label, profile))
                if sname != "default" and "perf" in phases and not (
                        this_fit and (default_fit is None or this_fit < default_fit)):
                    log(f"  {cfg.label} {profile}: no smaller tier than f16 — skipping KV quality")
                    continue
                qphases = ["gate", "judge", "mono", "chat"] if sname == "default" else ["gate", "judge"]
                qs = {"profile": profile, "server": cfg.label, "server_env": cfg.env(),
                      "items": args.items, "snapshot": snap["id"], "phases": qphases}
                if led.has_run(fingerprint(key, "quality", qs)) and not args.force:
                    log(f"  [ledger] quality {profile} {cfg.label}: known")
                    continue
                out_dir = DATA / "runs" / slug(ref) / f"{profile}_{cfg.label}_{args.items}_{datetime.now():%Y%m%d-%H%M}"
                bake = models.bake(bench.endpoint, ref, ctx)
                started = datetime.now().astimezone().isoformat(timespec="seconds")
                try:
                    proc = subprocess.run(
                        [sys.executable, "-m", "tests.benchmarks.hwbench.quality", json.dumps({
                            "bake": bake, "profile": profile, "out_dir": str(out_dir),
                            "phases": qphases, "snapshot": str(snap_path), "items": args.items})],
                        cwd=REPO, env=quality.child_env(bench.endpoint, profile, ctx),
                        capture_output=True, text=True, encoding="utf-8", timeout=6 * 3600)
                finally:
                    server.unload_all(bench.endpoint)
                    models.unbake(bench.endpoint, ref)
                if proc.returncode != 0:
                    log(f"  quality {profile} {cfg.label} FAILED:\n{proc.stderr[-2000:]}")
                    led.add_run(key, "quality", qs, env=environment(bench, reserve_mib, prefetched),
                                metrics={}, status="failed", error=proc.stderr[-2000:], started=started)
                    continue
                qm = json.loads(proc.stdout.strip().splitlines()[-1])
                arts = [str(p) for p in sorted(out_dir.glob("*.jsonl"))]
                led.add_run(key, "quality", qs, env=environment(bench, reserve_mib, prefetched),
                            metrics=qm, artifacts=arts, started=started)
                log(f"  quality {profile} {cfg.label}: gate {qm.get('gate_valid_pct')} % "
                    f"judge {qm.get('judge_valid_pct')} % verdicts {qm.get('judge_verdicts')} "
                    f"mono retry {qm.get('mono_retry_pct')} %")
    bench.ensure(SERVERS["default"])
    store.cleanup(ref)


def cmd_run(args) -> None:
    global _LOG
    DATA.mkdir(parents=True, exist_ok=True)
    _LOG = DATA / f"run_{datetime.now():%Y-%m-%d_%H-%M}.log"
    problems = preflight(args.force_busy, args.busy_mib)
    if problems:
        for p in problems:
            log("REFUSED:", p)
        raise SystemExit(2)
    led = Ledger()
    cands, _ = load_candidates(args.only)
    idle = gpu.query()
    reserve = args.desktop_reserve or max(400, min(1536, idle["used_mib"]))
    log(f"bench {BENCH_VERSION} on {idle['name']} (driver {idle['driver']}), "
        f"desktop reserve {reserve} MiB, {len(cands)} candidates, phases {args.phases}")
    with server.BenchServer(log_dir=DATA / "logs") as bench:
        bench.ensure(SERVERS["default"])
        store = models.Store(bench.endpoint, min_free_gb=args.min_free_gb, log=log)
        store.cleanup_leftovers(keep={c["ref"] for c in cands})
        for i, c in enumerate(cands):
            nxt = cands[i + 1]["ref"] if i + 1 < len(cands) else None
            try:
                bench_model(c, args, led, bench, store, nxt, reserve)
            except Exception as e:   # one model's crash must not end the night
                log(f"  CRASH on {c['ref']}: {type(e).__name__}: {e}")
                try:
                    server.unload_all(bench.endpoint)
                    store.cleanup(c["ref"])
                except Exception:
                    pass
    log("done.")


# ── status / matrix / scoring ────────────────────────────────────────────
def cmd_status(args) -> None:
    led = Ledger()
    for key, m in sorted(led.data["models"].items()):
        runs = led.runs(key, status=None)
        ph = {}
        for r in runs:
            ph[r["phase"]] = ph.get(r["phase"], 0) + 1
        print(f"{key:48} {m.get('architecture', '?'):5} {m.get('parameter_size', '?'):>7} "
              f"{m.get('quantization', '?'):7} {ph}")


def cmd_matrix(args) -> None:
    led = Ledger()
    _, ref = load_candidates()
    mx = matrix.build(led, reference_ref=ref)
    md = matrix.render_md(mx)
    (DATA / "matrix.md").parent.mkdir(parents=True, exist_ok=True)
    (DATA / "matrix.md").write_text(md, encoding="utf-8")
    print(md)
    if args.export:
        print("exported ->", matrix.export(mx))


def cmd_score_sheets(args) -> None:
    """Side-by-side sheets for the 5-axis rubric (Faith/Rel/Spec/Tone/Format,
    0-5 each), models anonymised as M1..Mn, 12 titles per chunk — the 2026-08
    protocol. Writes a sheet .md and a blank .csv per chunk."""
    led = Ledger()
    refs = [r.strip() for r in args.refs.split(",")]
    kind = "monologue" if args.kind == "mono" else "chat"
    runs = []
    for ref in refs:
        for key in led.keys_for_ref(ref):
            for r in led.runs(key, "quality"):
                if r["settings"].get("server") == matrix.DEFAULT_SERVER and \
                        (not args.profile or r["settings"]["profile"] == args.profile):
                    art = next((a for a in r["artifacts"] if a.endswith(f"{kind}.jsonl")), None)
                    if art:
                        runs.append((ref, r, art))
    if not runs:
        raise SystemExit("no matching quality runs with artifacts")
    legend = {f"M{i + 1}": (ref, r["id"]) for i, (ref, r, _) in enumerate(runs)}
    rows = {}
    for (mid, (ref, rid)), (_, _, art) in zip(legend.items(), runs):
        for line in Path(art).read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            k = d.get("key") or d.get("id")
            rows.setdefault(k, {})[mid] = d
    out = DATA / "scoring" / f"{kind}_{datetime.now():%Y%m%d-%H%M}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "legend.json").write_text(json.dumps(legend, indent=1), encoding="utf-8")
    keys = sorted(rows)
    for n in range(0, len(keys), 12):
        chunk = keys[n:n + 12]
        md, cs = [], [["key", "model", "run_id", "faith", "rel", "spec", "tone", "format", "note"]]
        for k in chunk:
            md.append(f"## {k}\n")
            for mid, d in sorted(rows[k].items()):
                text = d.get("text") or d.get("response") or "\n".join(
                    f"> {t['user'][:200]}\n{t['assistant']}" for t in d.get("turns", []))
                md.append(f"**{mid}** ({d.get('verdict', '')})\n\n{text}\n")
                cs.append([k, mid, legend[mid][1], "", "", "", "", "", ""])
        (out / f"chunk_{n // 12 + 1:02}.md").write_text("\n".join(md), encoding="utf-8")
        with (out / f"chunk_{n // 12 + 1:02}.csv").open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(cs)
    print(f"{len(keys)} items, {len(legend)} models -> {out}")


def cmd_record_scores(args) -> None:
    led = Ledger()
    per_run: dict[str, list] = {}
    for p in args.csv:
        with open(p, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                axes = [row.get(a) for a in ("faith", "rel", "spec", "tone", "format")]
                if all(a not in (None, "") for a in axes):
                    per_run.setdefault(row["run_id"], []).append([float(a) for a in axes])
    for rid, items in per_run.items():
        n = len(items)
        means = [round(sum(x[i] for x in items) / n, 2) for i in range(5)]
        agg = {"n": n, "mean25": round(sum(sum(x) for x in items) / n, 2),
               "faith": means[0], "rel": means[1], "spec": means[2], "tone": means[3],
               "format": means[4], "ge22": sum(sum(x) >= 22 for x in items),
               "le19": sum(sum(x) <= 19 for x in items)}
        led.add_score(rid, scorer=args.scorer, rubric="5-axis-2026-08", aggregate=agg,
                      item_file=",".join(args.csv))
        print(rid, agg)


def main() -> None:
    sys.path.insert(0, str(REPO))
    os.chdir(REPO)
    ap = argparse.ArgumentParser(description="Curatarr hardware-matrix bench")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan"); p.add_argument("--only", default="")
    p = sub.add_parser("run")
    p.add_argument("--only", default="", help="comma list of refs (default: all candidates)")
    p.add_argument("--phases", default="perf,quality")
    p.add_argument("--servers", default="default,q8,q4")
    p.add_argument("--items", default="sub40", choices=["sub40", "full"])
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--desktop-reserve", type=int, default=0,
                   help="MiB a real card loses to the desktop (default: measured idle use)")
    p.add_argument("--min-free-gb", type=float, default=15.0)
    p.add_argument("--force", action="store_true", help="re-measure known fingerprints")
    p.add_argument("--force-busy", action="store_true")
    p.add_argument("--busy-mib", type=int, default=3500,
                   help="refuse when more VRAM than this is in use before the run")
    sub.add_parser("status")
    p = sub.add_parser("matrix"); p.add_argument("--export", action="store_true")
    p = sub.add_parser("score-sheets")
    p.add_argument("--refs", required=True); p.add_argument("--kind", default="mono", choices=["mono", "chat"])
    p.add_argument("--profile", default="")
    p = sub.add_parser("record-scores")
    p.add_argument("--csv", nargs="+", required=True); p.add_argument("--scorer", default="claude")
    args = ap.parse_args()
    {"plan": cmd_plan, "run": cmd_run, "status": cmd_status, "matrix": cmd_matrix,
     "score-sheets": cmd_score_sheets, "record-scores": cmd_record_scores}[args.cmd](args)


if __name__ == "__main__":
    main()
