"""One performance cell: model x num_ctx x num_gpu on a given server config
and (simulated) card size.

A cell, in order:
  1. unload everything, read the idle baseline (VRAM used, RAM available);
  2. cold load (num_predict 1) -> load time, /api/ps residency (size,
     size_vram, context) and the VRAM the model really took;
  3. JUDGE shape x reps: the production constitution for the profile +
     frozen evidence + the forced VERDICT_SCHEMA, temperature 0 — streamed,
     so time-to-first-token is measured too. Every rep uses a DIFFERENT
     item: Ollama caches a repeated prompt prefix, which would fake prefill;
  4. MONOLOGUE shape x reps: free prose, ~400 tokens;
  5. LONG-CONTEXT probe: a prompt filling ~85 % of num_ctx — where KV
     pressure shows (the qwen3.6 2 s -> 600 s escalation started there);
  6. escalation check: last judge rep vs first.

Hard per-call timeout; a cell that times out is recorded as such (that IS
the result for a card that is too small), not retried.
"""
from __future__ import annotations

import json
import statistics
import time

import httpx

from . import gpu, server

CALL_TIMEOUT_S = 300
FILLER = ("The library holds titles across anime, film, series and music; each "
          "entry carries a synopsis, genres, a watch history and a size on disk. ")


def _judge_system(profile: str) -> tuple[str, dict]:
    from src.services import pillars
    from src.services.llm_utils import UNTRUSTED_RULE
    law = pillars.PILLAR_CONSTITUTION_COMPACT if profile == "small" else pillars.PILLAR_CONSTITUTION
    return law + "\n\n" + UNTRUSTED_RULE, pillars.VERDICT_SCHEMA


def _stream_chat(endpoint: str, payload: dict) -> dict:
    """Streamed /api/chat -> timings. Never raises; failures come back as
    ``{"error": ...}`` with the wall time spent."""
    t0 = time.perf_counter()
    first = None
    text = []
    final = {}
    try:
        with httpx.stream("POST", f"{endpoint}/api/chat", json={**payload, "stream": True},
                          timeout=httpx.Timeout(30, read=CALL_TIMEOUT_S)) as r:
            if r.status_code != 200:
                return {"error": f"HTTP {r.status_code}: {r.read()[:200]!r}",
                        "wall_s": round(time.perf_counter() - t0, 2)}
            for line in r.iter_lines():
                if not line:
                    continue
                d = json.loads(line)
                if d.get("error"):
                    return {"error": d["error"], "wall_s": round(time.perf_counter() - t0, 2)}
                piece = (d.get("message") or {}).get("content") or ""
                if piece and first is None:
                    first = time.perf_counter()
                text.append(piece)
                if d.get("done"):
                    final = d
    except (httpx.HTTPError, ValueError) as e:
        return {"error": f"{type(e).__name__}: {e}", "wall_s": round(time.perf_counter() - t0, 2)}
    wall = time.perf_counter() - t0

    def rate(n, ns):
        return round(n / (ns / 1e9), 1) if n and ns else None

    return {
        "wall_s": round(wall, 2),
        "ttft_s": round(first - t0, 2) if first else None,
        "load_s": round((final.get("load_duration") or 0) / 1e9, 2),
        "prompt_tokens": final.get("prompt_eval_count"),
        "gen_tokens": final.get("eval_count"),
        "prefill_tok_s": rate(final.get("prompt_eval_count"), final.get("prompt_eval_duration")),
        "gen_tok_s": rate(final.get("eval_count"), final.get("eval_duration")),
        "text": "".join(text),
    }


def _residency(endpoint: str, model: str) -> dict:
    for m in server.loaded_models(endpoint):
        name = m.get("name") or m.get("model") or ""
        if name == model or name.startswith(model.split(":")[0] + ":"):
            size, vram = m.get("size") or 0, m.get("size_vram") or 0
            return {"size_mib": size // gpu.MIB, "size_vram_mib": vram // gpu.MIB,
                    "gpu_share_pct": round(100 * vram / size) if size else None,
                    "context_length": m.get("context_length")}
    return {}


def ps_implausible(res: dict, vram_model_mib: int | None) -> bool:
    """Ollama's /api/ps size far below what nvidia-smi saw the model take:
    its GPU-share figure cannot be trusted then (gemma4:31b 2026-10 weights
    on Ollama 0.40.4: ps 2.1 GB, nvidia-smi ~22 GB)."""
    size = res.get("size_mib")
    return bool(size and vram_model_mib and size < 0.8 * vram_model_mib)


CARD_FULL_SLACK_MIB = 384


def sysmem_fallback(cell: dict, idle_free_mib: int | None = None) -> dict:
    """The NVIDIA driver on Windows spills CUDA memory into shared system RAM
    when VRAM runs out ("Sysmem Fallback"), and Ollama then counts that as
    VRAM. Two signs, either is enough:

    * the card took less than Ollama claims — llama3.2:3b on a simulated
      4 GB card: 2974 MiB "resident", 2323 MiB taken (2026-10-16);
    * the card is FULL: less than CARD_FULL_SLACK_MIB left of what was free
      before the load. /api/ps does not count gemma4:31b's KV cache at all,
      so the first sign never fires for it; on a 20 GB tier it left 128 MiB
      free, ran 29 instead of 41 tok/s and took 28 instead of 7 s a verdict.
      Measured against FREE memory, not the nominal total: Windows keeps
      ~0.4-0.7 GB of a 24 GB card to itself, so "total - slack" was never
      reached.
    """
    vram, claimed = cell.get("vram_model_mib"), cell.get("size_vram_mib")
    size = cell.get("size_mib")
    under = bool(vram and claimed and vram < 0.9 * claimed)
    full = bool(vram is not None and idle_free_mib is not None
                and idle_free_mib - vram <= CARD_FULL_SLACK_MIB)
    if not (under or full):
        return {"sysmem_fallback": False, "card_full": False,
                "gpu_share_effective_pct": cell.get("gpu_share_pct")}
    eff = round(100 * min(vram, claimed) / size) if (under and size) else None
    # A full card hides how much spilled: below 100 is all that is known.
    return {"sysmem_fallback": True, "card_full": full,
            "gpu_share_effective_pct": min(99, eff) if eff is not None else 99}


def stalled(points: list[dict], ctx: int, vram: int) -> bool:
    """The sweep's second sign: the footprint stops growing with the window.
    The KV slope of the first windows (2k..8k) predicts the next point; a
    real growth under a quarter of that means the KV cache went elsewhere
    (gemma4:31b: ~80 MiB per 1k context up to 32k, then +48 MiB for 16k)."""
    base = [p for p in points if p.get("vram_model_mib") and not p.get("sysmem_fallback")]
    if len(base) < 3:
        return False
    first = base[0]
    third = base[2]
    slope = (third["vram_model_mib"] - first["vram_model_mib"]) / max(1, third["num_ctx"] - first["num_ctx"])
    prev = base[-1]
    expected = slope * (ctx - prev["num_ctx"])
    return expected > 64 and (vram - prev["vram_model_mib"]) < 0.25 * expected


def _agg(rows: list[dict], key: str) -> dict:
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return {}
    vals.sort()
    p90 = vals[min(len(vals) - 1, int(round(0.9 * (len(vals) - 1))))]
    return {f"{key}_median": round(statistics.median(vals), 2), f"{key}_p90": p90,
            f"{key}_max": vals[-1]}


SWEEP_CTX = (2048, 4096, 8192, 12288, 16384, 24576, 32768, 49152, 65536,
             98304, 131072, 196608, 262144)


def ctx_sweep(endpoint: str, model: str, *, context_max: int | None,
              ctxs=SWEEP_CTX) -> dict:
    """What the model really occupies at each context window: load it with
    num_ctx from 2k up to its maximum, all layers requested on the GPU, and
    read the VRAM it took (nvidia-smi delta) plus Ollama's own residency.

    Ollama allocates the KV cache for the whole window at load, so a load
    is enough — no generation needed, a point costs one load. The sweep
    stops at the first window that no longer fits fully on the card: every
    larger one spills too. Download size is NOT VRAM: the projector, the
    compute buffers and the KV cache come on top, and an MoE file is loaded
    whole although few experts run per token."""
    points = []
    for ctx in ctxs:
        if context_max and ctx > context_max:
            break
        server.unload_all(endpoint)
        time.sleep(1.0)
        q0 = gpu.query()
        idle, idle_free = q0["used_mib"], q0["free_mib"]
        r = _stream_chat(endpoint, {"model": model, "think": False, "keep_alive": "2m",
                                    "messages": [{"role": "user", "content": "ready"}],
                                    "options": {"num_ctx": ctx, "num_gpu": 99, "num_predict": 1,
                                                "temperature": 0}})
        if r.get("error"):
            points.append({"num_ctx": ctx, "error": r["error"][:200]})
            break
        res = _residency(endpoint, model)
        vram = gpu.query()["used_mib"] - idle
        point = {"num_ctx": ctx, "vram_model_mib": vram, "load_s": r.get("load_s"),
                 "ps_implausible": ps_implausible(res, vram), **res}
        point.update(sysmem_fallback(point, idle_free))
        if not point["sysmem_fallback"] and stalled(points, ctx, vram):
            point.update(sysmem_fallback=True, stalled=True, gpu_share_effective_pct=99)
        # A window that spilled into shared memory does not fit either, even
        # when Ollama reports it resident.
        if point["sysmem_fallback"]:
            point["gpu_share_pct"] = point["gpu_share_effective_pct"]
        points.append(point)
        if (point.get("gpu_share_pct") or 0) < 100:
            break
    server.unload_all(endpoint)
    fitted = [p for p in points if p.get("gpu_share_pct") == 100]
    return {"points": points,
            "max_resident_ctx": fitted[-1]["num_ctx"] if fitted else None,
            # MiB per 1k tokens of context: the KV slope between the
            # smallest and largest resident windows.
            "kv_mib_per_1k_ctx": (round((fitted[-1]["vram_model_mib"] - fitted[0]["vram_model_mib"])
                                        / ((fitted[-1]["num_ctx"] - fitted[0]["num_ctx"]) / 1024), 1)
                                  if len(fitted) >= 2 else None)}


def run_cell(endpoint: str, model: str, *, num_ctx: int, num_gpu: int | None,
             profile: str, facts: list[str], reps: int = 3,
             long_probe: bool = True, sampler: gpu.Sampler | None = None) -> dict:
    """Measure one cell. ``num_gpu`` None = let Ollama split (CURATOR_NUM_GPU=-1)."""
    opts = {"num_ctx": num_ctx}
    if num_gpu is not None:
        opts["num_gpu"] = num_gpu
    base = {"model": model, "think": False, "keep_alive": "10m"}
    server.unload_all(endpoint)
    time.sleep(1.0)
    idle = gpu.query()
    if sampler:
        sampler.mark()

    out: dict = {"idle_vram_used_mib": idle["used_mib"], "idle_vram_free_mib": idle["free_mib"]}
    load = _stream_chat(endpoint, {**base, "messages": [{"role": "user", "content": "ready"}],
                                   "options": {**opts, "num_predict": 1, "temperature": 0}})
    if load.get("error"):
        out.update(status="load_failed", error=load["error"], load_wall_s=load["wall_s"])
        return out
    out["cold_load_s"] = load["load_s"]
    out.update(_residency(endpoint, model))
    loaded = gpu.query()
    out["vram_model_mib"] = loaded["used_mib"] - idle["used_mib"]
    out["ps_implausible"] = ps_implausible(out, out["vram_model_mib"])
    out.update(sysmem_fallback(out, idle["free_mib"]))

    system, schema = _judge_system(profile)
    judge = []
    for i in range(reps):
        f = facts[i % len(facts)]
        judge.append(_stream_chat(endpoint, {
            **base, "format": schema,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": "FACTS:\n" + f}],
            "options": {**opts, "temperature": 0.0, "seed": 7, "repeat_penalty": 1.0,
                        "num_predict": 800}}))
        if judge[-1].get("error"):
            break
    mono = []
    for i in range(reps):
        f = facts[(i + 1) % len(facts)]
        mono.append(_stream_chat(endpoint, {
            **base, "messages": [{"role": "user", "content":
                "You are the curator. Write the 2-3 sentence card note for this "
                "title in your own voice. No headers, no lists.\n\n" + f[:4000]}],
            "options": {**opts, "temperature": 0.7, "num_predict": 400}}))
        if mono[-1].get("error"):
            break
    longp = {}
    if long_probe:
        # ~4 chars per token; leave room for the answer.
        target_chars = int((num_ctx * 0.85 - 300) * 4)
        filler = (FILLER * (target_chars // len(FILLER) + 1))[:target_chars]
        longp = _stream_chat(endpoint, {
            **base, "messages": [{"role": "user", "content": filler +
                                  "\n\nSummarise the paragraph above in one sentence."}],
            "options": {**opts, "temperature": 0.0, "num_predict": 128}})

    after = _residency(endpoint, model)
    errors = [r["error"] for r in judge + mono + ([longp] if longp else []) if r.get("error")]
    ok_j = [r for r in judge if not r.get("error")]
    ok_m = [r for r in mono if not r.get("error")]
    out.update({
        "status": "timeout" if any("Timeout" in e for e in errors) else ("error" if errors else "ok"),
        "errors": errors,
        "judge_n": len(ok_j), "mono_n": len(ok_m),
        **{f"judge_{k}": v for k, v in _agg(ok_j, "wall_s").items()},
        **{f"judge_{k}": v for k, v in _agg(ok_j, "ttft_s").items()},
        **{f"judge_{k}": v for k, v in _agg(ok_j, "prefill_tok_s").items()},
        **{f"mono_{k}": v for k, v in _agg(ok_m, "wall_s").items()},
        **{f"mono_{k}": v for k, v in _agg(ok_m, "gen_tok_s").items()},
        "judge_prompt_tokens_max": max((r.get("prompt_tokens") or 0 for r in ok_j), default=None),
        "judge_escalation_ratio": (round(ok_j[-1]["wall_s"] / ok_j[0]["wall_s"], 2)
                                   if len(ok_j) >= 2 and ok_j[0]["wall_s"] else None),
        "long_prompt_tokens": longp.get("prompt_tokens"),
        "long_prefill_tok_s": longp.get("prefill_tok_s"),
        "long_gen_tok_s": longp.get("gen_tok_s"),
        "long_wall_s": longp.get("wall_s"),
        "long_error": longp.get("error"),
        "gpu_share_after_pct": after.get("gpu_share_pct"),
    })
    if sampler:
        out.update(sampler.summary())
    return out
