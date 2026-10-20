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
  sweep    load at every context window from 2k to the model's maximum and
           read the VRAM it really takes — the footprint the setup uses,
           instead of the download size;
  perf     full card -> tightest tier it fits -> offload descent with
           num_gpu auto until too slow. A smaller card is simulated through
           llama.cpp's --fit target (gpu.fit_target_for): since Ollama 0.40
           llama-server places the layers itself against CUDA's free memory,
           so it splits exactly as on that card. The CUDA ballast and
           OLLAMA_GPU_OVERHEAD do not work for this (WDDM pages the ballast
           out; the overhead is computed but ignored — probes 2026-10-19).
           No forced num_gpu=99 cell any more: -ngl 99 bypasses the fit and
           would load onto the real 24 GB.
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
import shlex
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import httpx

from . import BENCH_VERSION, gpu, matrix, models, perf, quality, registry, server
from .ledger import Ledger, fingerprint, model_key

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
DATA = HERE.parent / "hwbench_data"
OWNER_OLLAMA = os.environ.get("HWBENCH_OWNER_OLLAMA", "http://127.0.0.1:11434")
APP_URL = os.environ.get("HWBENCH_APP_URL", "http://127.0.0.1:8000")
PROFILE_CTX = {"large": 16384, "small": 8192}
KV_QUALITY_MIN_SAVING_MIB = 1024
SERVERS = {
    "default": server.ServerConfig("auto", "f16"),
    "q8": server.ServerConfig("1", "q8_0"),
    "q4": server.ServerConfig("1", "q4_0"),
}
_LOG = None
# Set by --remote: the host whose card is measured. It joins the perf and
# sweep fingerprints (a NAS cell is not a 4090 cell) and is empty locally,
# so every local fingerprint stays what it was.
REMOTE_HOST = ""


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
    try:
        httpx.get(APP_URL, timeout=3)
        app_up = True
        problems.append(f"Curatarr answers on {APP_URL} — stop the app (it would load models mid-bench)")
    except httpx.HTTPError:
        app_up = False
    leftovers = server.loaded_models(OWNER_OLLAMA)
    if leftovers and app_up:
        problems.append(f"the owner's Ollama ({OWNER_OLLAMA}) holds a model — stop Curatarr first")
    elif leftovers:
        # With the app off, what is still loaded is a leftover (snapshot_items
        # loads the embedder for the considerations) — unload, don't refuse.
        names = [m.get("name") for m in leftovers]
        server.unload_all(OWNER_OLLAMA)
        log(f"preflight: unloaded leftovers on the owner's Ollama: {', '.join(names)}")
        if server.loaded_models(OWNER_OLLAMA):
            problems.append(f"the owner's Ollama ({OWNER_OLLAMA}) still holds {names} after unloading")
    q = gpu.query()
    with gpu.Sampler() as s:
        time.sleep(3)
    util = s.summary().get("gpu_util_mean_pct", 0)
    if not force_busy and (q["used_mib"] > busy_mib or util > 25):
        problems.append(f"GPU busy: {q['used_mib']} MiB used, {util} % load — a game? "
                        f"(--force-busy to override; timings would be contaminated)")
    return problems


def environment(bench, reserve_mib: int, prefetching: bool) -> dict:
    q = gpu.query()
    if gpu.is_remote():
        try:
            info = gpu.remote_run("grep MemTotal /proc/meminfo; nproc; uname -sr").splitlines()
            ram = round(int(info[0].split()[1]) / 2**20, 1)
            osname, cpus = info[2].strip(), int(info[1])
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            ram, osname, cpus = None, "", None
    else:
        try:
            import psutil
            ram = round(psutil.virtual_memory().total / 2**30, 1)
        except ImportError:
            ram = None
        osname, cpus = platform.platform(), os.cpu_count()
    return {"gpu": q["name"], "gpu_total_mib": q["total_mib"], "driver": q["driver"],
            "ollama": bench.version(), "host_ram_gb": ram, "os": osname, "cpus": cpus,
            "host": REMOTE_HOST or platform.node(),
            "desktop_reserve_mib": reserve_mib, "prefetch_active": prefetching}


def preflight_remote(bench, busy_mib: int) -> list[str]:
    """The NAS card is shared (Plex transcoding, Immich ML): measure only
    when nothing else holds it, and never unload someone else's model."""
    if not bench.version():
        return [f"remote Ollama at {bench.endpoint} does not answer"]
    problems = []
    loaded = server.loaded_models(bench.endpoint)
    if loaded:
        problems.append(f"remote Ollama holds {[m.get('name') for m in loaded]} - someone is using it")
    apps = gpu.compute_apps()
    if apps:
        problems.append(f"GPU on {REMOTE_HOST} in use by {[a['name'] for a in apps]}")
    q = gpu.query()
    if q["used_mib"] > busy_mib:
        problems.append(f"GPU on {REMOTE_HOST}: {q['used_mib']} MiB used")
    return problems


RUNNER_CMDLINE = "for p in $(pgrep -x llama-server); do tr '\\0' ' ' < /proc/$p/cmdline; echo; done"


def runner_cmdline() -> str:
    """The remote llama-server's arguments: which flash-attention / KV /
    parallel settings the NAS app really runs (its env is not readable)."""
    try:
        return gpu.remote_run(RUNNER_CMDLINE).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


# ── plan ─────────────────────────────────────────────────────────────────
def cmd_plan(args) -> None:
    led = Ledger()
    cands, ref = load_candidates(args.only)
    print(f"bench {BENCH_VERSION}, reference {ref}, ledger {led.path.name}: "
          f"{len(led.data['models'])} models, {len(led.data['runs'])} runs\n")
    total = 0.0
    for c in cands:
        remote = registry.remote_manifest(c["ref"])
        installed = registry.installed_weights(OWNER_OLLAMA, c["ref"])
        local = {"weights_digest": installed} if installed else None
        wd = installed or (remote or {}).get("weights_digest", "")
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


def _host() -> dict:
    return {"host": REMOTE_HOST} if REMOTE_HOST else {}


def sweep_settings(cfg) -> dict:
    return {"server": cfg.label, "server_env": cfg.env(), "num_gpu": 99, **_host()}


def perf_base(cfg, profile: str, snap_id: str, reps: int) -> dict:
    return {"num_ctx": PROFILE_CTX[profile], "profile": profile, "server": cfg.label,
            "server_env": cfg.env(), "snapshot": snap_id, "reps": reps, **_host()}


def quality_settings(cfg, profile: str, snap_id: str, items: str, default: bool) -> dict:
    return {"profile": profile, "server": cfg.label, "server_env": cfg.env(), "items": items,
            "snapshot": snap_id,
            "phases": ["gate", "judge", "mono", "chat"] if default else ["gate", "judge"]}


def pending_work(c: dict, args, led: Ledger, kv: bool = True) -> bool:
    """Is anything of this model still unmeasured — decided from the ledger
    alone, BEFORE a download? The 2026-10-19 --no-tiers restart re-pulled
    19 GB for gemma4:31b-it-qat whose every cell was already recorded.
    Identity comes from the registry's weights digest (what a pull would
    install). Which tier cells a model gets depends on what the first one
    measures, so a (server, profile) counts as done once ANY tier cell of
    the current method is recorded. ``kv`` False ignores the quantised-KV
    quality passes: for a model the bench already removed, ~10 minutes of
    gate + judge do not justify a 20 GB download (``--repull-for-kv`` turns
    them back on)."""
    if args.force:
        return True
    remote = registry.remote_manifest(c["ref"]) or {}
    if not remote.get("weights_digest"):
        return True
    key = model_key(c["ref"], remote["weights_digest"])
    if not led.model(key):
        return True
    phases = set(args.phases.split(","))
    for sname in args.servers.split(","):
        cfg = SERVERS[sname]
        if "sweep" in phases and not led.has_run(fingerprint(key, "ctx_sweep", sweep_settings(cfg))):
            return True
        for profile in c["profiles"] or ["small"]:
            snap = _snapshot_path(profile)
            if not snap:
                continue
            snap_id = json.loads(snap.read_text(encoding="utf-8"))["id"]
            if "perf" in phases and not led.has_run(fingerprint(
                    key, "perf", {**perf_base(cfg, profile, snap_id, args.reps),
                                  "tier_gb": "full", "num_gpu": 99})):
                return True
            if "perf" in phases and not args.no_tiers and not any(
                    r["settings"].get("tier_method") == matrix.TIER_METHOD
                    and r["settings"].get("server") == cfg.label
                    and r["settings"].get("profile") == profile
                    for r in led.runs(key, "perf")):
                return True
            if "quality" not in phases or c["role"] != "curator":
                continue
            if sname != "default":
                if not kv:
                    continue
                vram = {}
                for srv in (SERVERS["default"], cfg):
                    fp = fingerprint(key, "perf", {**perf_base(srv, profile, snap_id, args.reps),
                                                   "tier_gb": "full", "num_gpu": 99})
                    row = next((r for r in led.runs(key, "perf") if r["fingerprint"] == fp), None)
                    vram[srv.label] = (row or {}).get("metrics", {}).get("vram_model_mib")
                f16, mine = vram.get(SERVERS["default"].label), vram.get(cfg.label)
                if not getattr(args, "kv_quality", False) and not (
                        f16 and mine and f16 - mine >= KV_QUALITY_MIN_SAVING_MIB):
                    continue
            if not led.has_run(fingerprint(key, "quality", quality_settings(
                    cfg, profile, snap_id, args.items, sname == "default"))):
                return True
    return False


def tightest_tier(vram_model_mib: int, reserve_mib: int, total_mib: int) -> int | None:
    for t in matrix.TIERS_GB:
        if t * 1024 >= total_mib:
            break
        if vram_model_mib + reserve_mib + matrix.FIT_MARGIN_MIB <= t * 1024:
            return t
    return None


def bench_model(c: dict, args, led: Ledger, bench: server.BenchServer,
                store: models.Store, next_ref: str | None, reserve_mib: int,
                next_c: dict | None = None) -> None:
    ref = c["ref"]
    log(f"\n=== {ref} ({c['role']}) — {c['why']}")
    if store.is_installed(ref):
        local = {"weights_digest": registry.installed_weights(bench.endpoint, ref)}
        remote = registry.remote_manifest(ref) or {}
        if remote and local["weights_digest"] and remote["weights_digest"] != local["weights_digest"]:
            log("  installed copy differs from the registry — benching the INSTALLED weights; "
                "the bench never re-pulls an installed tag (it would replace them)")
    if not store.is_installed(ref) and not pending_work(c, args, led, kv=args.repull_for_kv):
        log("  [ledger] everything that justifies a download is known — not pulling"
            + ("" if args.repull_for_kv else " (KV-quality passes need --repull-for-kv)"))
        return
    if not store.ready(ref):
        log(f"  SKIP: could not pull {ref}")
        return
    info = registry.local_manifest(ref) or {}     # sizes + layer kinds
    weights = registry.installed_weights(bench.endpoint, ref) or info.get("weights_digest", "")
    info["weights_digest"] = weights
    remote = registry.remote_manifest(ref) or {}
    key = led.upsert_model(ref, weights,
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
    full_vram: dict = {}

    ctx_max = (led.model(key) or {}).get("context_max")
    for sname in args.servers.split(","):
        cfg = SERVERS[sname]
        bench.ensure(cfg)
        if "sweep" in phases:
            ss = sweep_settings(cfg)
            if led.has_run(fingerprint(key, "ctx_sweep", ss)) and not args.force:
                log(f"  [ledger] ctx sweep {cfg.label}: known")
            else:
                started = datetime.now().astimezone().isoformat(timespec="seconds")
                sw = perf.ctx_sweep(bench.endpoint, ref, context_max=ctx_max)
                led.add_run(key, "ctx_sweep", ss, env=environment(bench, reserve_mib, prefetched),
                            metrics=sw, started=started)
                log(f"  ctx sweep {cfg.label}: " + ", ".join(
                    f"{p['num_ctx'] // 1024}k={p.get('vram_model_mib', '?')}"
                    + ("" if p.get("gpu_share_pct") == 100 else f"({p.get('gpu_share_pct', 'err')}%)")
                    for p in sw["points"]) + f" MiB; KV {sw['kv_mib_per_1k_ctx']} MiB/1k")
        for profile in profiles:
            snap_path = _snapshot_path(profile)
            if not snap_path:
                log(f"  no evidence snapshot for profile {profile} — run snapshot_items first")
                continue
            snap = json.loads(snap_path.read_text(encoding="utf-8"))
            facts = _perf_facts(snap)
            ctx = PROFILE_CTX[profile]
            base = perf_base(cfg, profile, snap["id"], args.reps)

            def cell(tier, num_gpu):
                s = {**base, "tier_gb": tier, "num_gpu": num_gpu}
                if tier != "full":
                    # Settings stay those of the real server: the fit target
                    # depends on what is free at that moment, so it goes into
                    # the metrics, not the fingerprint.
                    s["tier_method"] = matrix.TIER_METHOD
                fp = fingerprint(key, "perf", s)
                if led.has_run(fp) and not args.force:
                    row = next(r for r in led.runs(key, "perf") if r["fingerprint"] == fp)
                    log(f"  [ledger] {cfg.label} {profile} tier {tier} gpu {num_gpu}: known")
                    return row["metrics"]
                started = datetime.now().astimezone().isoformat(timespec="seconds")
                sim = {}
                if tier != "full":
                    server.unload_all(bench.endpoint)
                    info = gpu.cuda_free()
                    target = gpu.fit_target_for(tier * 1024, reserve_mib, info)
                    bench.ensure(replace(cfg, fit_target_mib=target))
                    sim = {"method": matrix.TIER_METHOD, "card_mib": tier * 1024,
                           "desktop_mib": reserve_mib, "budget_mib": tier * 1024 - reserve_mib,
                           "cuda_free_mib": info["free_mib"], "cuda_ctx_mib": info["ctx_mib"],
                           "fit_target_mib": target}
                def attempt():
                    quiet = gpu.wait_quiet_cpu(max_pct=args.quiet_cpu_pct)
                    if quiet and not quiet["host_quiet"]:
                        log(f"  host CPU still at {quiet['host_cpu_idle_pct']} % after "
                            f"{quiet['host_quiet_wait_s']} s - measuring anyway")
                    with gpu.Sampler() as smp:
                        a = perf.run_cell(bench.endpoint, ref, num_ctx=ctx, num_gpu=num_gpu,
                                          profile=profile, facts=facts, reps=args.reps, sampler=smp)
                    a.update(quiet)
                    if gpu.is_remote():
                        a["runner_cmdline"] = runner_cmdline()
                    return a

                try:
                    attempts = [attempt()]
                    if matrix.borderline(attempts[0]):
                        log(f"  tier {tier}: {attempts[0].get('mono_gen_tok_s_median')} t/s / judge p90 "
                            f"{attempts[0].get('judge_wall_s_p90')} s is near a K.O. line - "
                            f"measuring {matrix.BORDERLINE_REPEATS} more times")
                        attempts += [attempt() for _ in range(matrix.BORDERLINE_REPEATS)]
                        m = matrix.settle(attempts)
                    else:
                        m = attempts[0]
                finally:
                    server.unload_all(bench.endpoint)
                    if sim:
                        bench.ensure(cfg)
                if sim:
                    m["tier_sim"] = sim
                    # What a real card of that size would NOT have had: on
                    # Windows the driver spills it into shared RAM, on Linux
                    # the load fails.
                    m["tier_overrun_mib"] = max(0, (m.get("vram_model_mib") or 0) - sim["budget_mib"])
                led.add_run(key, "perf", s, env=environment(bench, reserve_mib, prefetched),
                            metrics=m, started=started)   # a failed load IS the result
                over = m.get("tier_overrun_mib")
                log(f"  {cfg.label} {profile} ctx {ctx} tier {tier} gpu {num_gpu}: "
                    f"{m.get('status')} share {matrix.share(m)} %{' (SYSMEM FALLBACK)' if m.get('sysmem_fallback') else ''} "
                    f"vram {m.get('vram_model_mib')} MiB{f' (OVER the card by {over})' if over else ''} "
                    f"gen {m.get('mono_gen_tok_s_median')} t/s judge {m.get('judge_wall_s_median')} s"
                    f" cpu {m.get('cpu_mean_pct')} %"
                    + (f" | {len(m['attempts'])} attempts, spread {m['attempt_spread_pct']} %"
                       f"{' UNSTABLE' if m['speed_unstable'] else ''}" if m.get("attempts") else ""))
                return m

            if "perf" in phases:
                full = cell("full", 99)
                full_vram[(cfg.label, profile)] = full.get("vram_model_mib")
                if next_ref and not prefetched:
                    if next_c is None or pending_work(next_c, args, led, kv=args.repull_for_kv):
                        store.prefetch(next_ref)   # after the cold load: no disk contention there
                    prefetched = True
                total = gpu.query()["total_mib"]
            if "perf" in phases and not args.no_tiers:
                fit = None
                card = [x for x in matrix.TIERS_GB if x * 1024 < total]
                if matrix.share(full) == 100:
                    t = tightest_tier(full.get("vram_model_mib") or 0, reserve_mib, total)
                    for _ in range(2):
                        if t is None:
                            break
                        if matrix.tier_fits(cell(t, None)):
                            fit = t
                            break
                        ups = [x for x in card if x > t]
                        t = ups[0] if ups else None
                tier_by_server[(cfg.label, profile)] = fit
                below = [t for t in card if fit is None or t < fit]
                misses = 0
                for t in reversed(below):
                    m = cell(t, None)
                    ok, _ = matrix.perf_ok(m)
                    if m.get("status") == "load_failed" or (matrix.share(m) or 0) < 20:
                        break
                    misses += 0 if ok else 1
                    if misses >= 2:
                        break

            if "quality" in phases and c["role"] == "curator":
                default_fit = tier_by_server.get((SERVERS["default"].label, profile))
                this_fit = tier_by_server.get((cfg.label, profile))
                if getattr(args, "kv_quality", False):
                    pass        # --kv-quality: every KV server gets its pass
                elif args.no_tiers:
                    # No tier cells this run: a quantised KV cache earns its
                    # (cheap: gate + judge) quality pass when it saves real VRAM
                    # on the full card at the profile's context.
                    f16 = full_vram.get((SERVERS["default"].label, profile))
                    mine = full_vram.get((cfg.label, profile))
                    saves = bool(f16 and mine and f16 - mine >= KV_QUALITY_MIN_SAVING_MIB)
                    if sname != "default" and not saves:
                        log(f"  {cfg.label} {profile}: saves under {KV_QUALITY_MIN_SAVING_MIB} MiB "
                            f"against f16 — skipping KV quality")
                        continue
                elif sname != "default" and "perf" in phases and not (
                        this_fit and (default_fit is None or this_fit < default_fit)):
                    log(f"  {cfg.label} {profile}: no smaller tier than f16 — skipping KV quality")
                    continue
                qs = quality_settings(cfg, profile, snap["id"], args.items, sname == "default")
                qphases = qs["phases"]
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


class KeepAwake:
    """No standby while the bench runs (the owner's Balanced plan sleeps
    after 15 minutes on AC, which would end an overnight run). The display
    may still turn off. Windows only; elsewhere a no-op."""
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

    def __enter__(self):
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED)
        return self

    def __exit__(self, *exc):
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)
        return False


def cmd_run(args) -> None:
    global _LOG
    DATA.mkdir(parents=True, exist_ok=True)
    _LOG = DATA / f"run_{datetime.now():%Y-%m-%d_%H-%M}{'_' + args.remote_host if args.remote else ''}.log"
    if args.remote:
        bench_cm = remote_setup(args)
        problems = [] if args.force_busy else preflight_remote(bench_cm, args.busy_mib)
    else:
        server.reap_orphan_runners(log)      # a Ctrl+C'd run can leave one holding the card
        bench_cm = server.BenchServer(log_dir=DATA / "logs")
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
    with KeepAwake(), bench_cm as bench:
        bench.ensure(SERVERS["default"])
        store = models.Store(bench.endpoint, min_free_gb=args.min_free_gb, log=log, host=REMOTE_HOST,
                             disk_free_gb=remote_disk_free(args) if args.remote else None)
        store.cleanup_leftovers(keep={c["ref"] for c in cands})
        for i, c in enumerate(cands):
            nxt_c = cands[i + 1] if i + 1 < len(cands) else None
            nxt = nxt_c["ref"] if nxt_c else None
            try:
                bench_model(c, args, led, bench, store, nxt, reserve, nxt_c)
            except Exception as e:   # one model's crash must not end the night
                log(f"  CRASH on {c['ref']}: {type(e).__name__}: {e}")
                try:
                    server.unload_all(bench.endpoint)
                    store.cleanup(c["ref"])
                except Exception:
                    pass
    log("done.")


def remote_setup(args) -> server.RemoteServer:
    """--remote: measure the card of another host through its Ollama, with
    nvidia-smi / vmstat over ssh. Sweep and perf only - quality does not
    depend on the card and is measured here - and no simulated tiers: the
    remote card IS the tier."""
    global REMOTE_HOST
    if not args.remote_ssh:
        raise SystemExit("--remote needs --remote-ssh (the ssh command that reaches that host)")
    REMOTE_HOST = args.remote_host
    gpu.set_remote(shlex.split(args.remote_ssh, posix=False) + ["-o", "BatchMode=yes"])
    if set(args.phases.split(",")) - {"sweep", "perf"}:
        kept = [p for p in args.phases.split(",") if p in ("sweep", "perf")]
        log(f"remote: phases {args.phases} -> {','.join(kept)} (quality is measured locally)")
        args.phases = ",".join(kept) or "sweep,perf"
    if args.servers != "default":
        log(f"remote: servers {args.servers} -> default (the remote Ollama cannot be reconfigured)")
        args.servers = "default"
    args.no_tiers = True
    return server.RemoteServer(args.remote, REMOTE_HOST)


def remote_disk_free(args):
    path = args.remote_models_path

    def free_gb() -> float:
        out = gpu.remote_run(f"df -Pk '{path}' | tail -1").split()
        return int(out[3]) * 1024 / 1e9
    return free_gb


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
    p.add_argument("--phases", default="sweep,perf,quality")
    p.add_argument("--kv-quality", action="store_true",
                   help="run the quantised-KV quality passes regardless of the VRAM they save "
                        "on the full card (an MoE saves 0.3-0.5 GB there, but q8 is what "
                        "serves its 4 GB tier)")
    p.add_argument("--repull-for-kv", action="store_true",
                   help="re-download a removed model even when only its KV-quality passes are missing")
    p.add_argument("--no-tiers", action="store_true",
                   help="perf measures the full card only - no simulated smaller cards. "
                        "A tier-only pass is the opposite: --phases perf (known full-card "
                        "cells are skipped by the ledger)")
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
    p.add_argument("--quiet-cpu-pct", type=float, default=20.0,
                   help="wait (up to 2 min) for host CPU below this before each cell")
    p.add_argument("--remote", default="",
                   help="Ollama URL of another host to measure (e.g. http://nas:30068)")
    p.add_argument("--remote-host", default="",
                   help="name recorded for that host (default: the host of --remote)")
    p.add_argument("--remote-ssh", default="",
                   help='ssh command reaching it, e.g. "ssh -i ~/.ssh/key user@nas"')
    p.add_argument("--remote-models-path", default="/mnt/nvme_apps/configurations/ai_stack/ollama",
                   help="where the remote Ollama keeps its models (disk guard)")
    sub.add_parser("status")
    p = sub.add_parser("matrix"); p.add_argument("--export", action="store_true")
    p = sub.add_parser("score-sheets")
    p.add_argument("--refs", required=True); p.add_argument("--kind", default="mono", choices=["mono", "chat"])
    p.add_argument("--profile", default="")
    p = sub.add_parser("record-scores")
    p.add_argument("--csv", nargs="+", required=True); p.add_argument("--scorer", default="claude")
    args = ap.parse_args()
    if getattr(args, "remote", "") and not args.remote_host:
        # Named after the host in its URL, so no machine name is baked in here
        # and the cells of one remote stay under one name run after run.
        args.remote_host = httpx.URL(args.remote).host or "remote"
    {"plan": cmd_plan, "run": cmd_run, "status": cmd_status, "matrix": cmd_matrix,
     "score-sheets": cmd_score_sheets, "record-scores": cmd_record_scores}[args.cmd](args)


if __name__ == "__main__":
    main()
