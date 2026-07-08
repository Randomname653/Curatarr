"""
num_ctx VRAM/latency benchmark under REAL conditions (stdlib runner).

Question (2026-07-08 plan): how far can the curator's context window go on
the 24 GB 4090 with nomic-embed-text loaded alongside (the everyday state),
and where does Ollama start spilling to CPU? The Kill la Kill turn measured
33.5k chars of input (~8.4k tokens) before the history diet, ~19k after —
the baked num_ctx=8192 forces silent front-truncation.

num_ctx is passed as a REQUEST option (overrides the baked default), so no
model rebuild is needed — neither for the benchmark nor, if a bigger window
wins, for the fix.

Per (num_ctx, prompt-size) cell: load time, prefill t/s, generation t/s,
`ollama ps` VRAM split (the spill indicator), nvidia-smi usage.

Run from repo root (game CLOSED):  python tests/benchmarks/num_ctx_bench.py
"""
import datetime
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import httpx

from src.config import settings

CURATOR = settings.CURATOR_MODEL or "curatarr-curator"
EMBED = "nomic-embed-text"
NUM_CTXS = [8192, 12288, 16384, 20480]
# realistic = today's post-diet chat turn; heavy = the pre-diet worst case
PROMPT_SIZES = {"realistic_19k": 19000, "heavy_33k": 33000}
FILLER = ("The library holds titles across anime, film, series and music; "
          "the curator weighs resonance, custodianship and household value "
          "against storage cost, community reception and the owner's taste. ")


def sh(cmd: str) -> str:
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except Exception as e:
        return f"(failed: {e})"


def vram_mb() -> int:
    out = sh("nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits")
    try:
        return int(out.splitlines()[0])
    except Exception:
        return -1


def ps_line(model: str) -> str:
    for line in sh("ollama ps").splitlines():
        if model.split(":")[0] in line:
            return " ".join(line.split())
    return "(not loaded)"


def build_prompt(chars: int) -> str:
    body = (FILLER * (chars // len(FILLER) + 1))[:chars]
    return (body + "\n\nQUESTION: In two sentences, what trade-off governs "
            "keeping a low-rated but personally loved title?")


def main() -> None:
    base = settings.effective_ollama
    print(f"model={CURATOR}  embed={EMBED}  baseline VRAM={vram_mb()} MB\n")

    # everyday companion: nomic resident for the whole run
    r = httpx.post(f"{base}/api/embed", json={
        "model": EMBED, "input": "warmup", "keep_alive": "2h"}, timeout=120)
    print(f"nomic loaded (HTTP {r.status_code}), VRAM={vram_mb()} MB")

    results = []
    for num_ctx in NUM_CTXS:
        print(f"\n=== num_ctx {num_ctx} ===")
        sh(f"ollama stop {CURATOR}")
        time.sleep(2)
        for label, size in PROMPT_SIZES.items():
            prompt = build_prompt(size)
            t0 = time.time()
            try:
                resp = httpx.post(f"{base}/api/chat", json={
                    "model": CURATOR,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "keep_alive": "5m",
                    "think": False,
                    "options": {"num_ctx": num_ctx, "num_predict": 160,
                                "temperature": 0.3, "num_gpu": 99},
                }, timeout=600)
                wall = time.time() - t0
                d = resp.json()
            except Exception as e:
                print(f"  {label}: FAILED {e}")
                results.append({"num_ctx": num_ctx, "prompt": label, "error": str(e)})
                continue
            pe_n = d.get("prompt_eval_count") or 0
            pe_s = (d.get("prompt_eval_duration") or 0) / 1e9
            ev_n = d.get("eval_count") or 0
            ev_s = (d.get("eval_duration") or 0) / 1e9
            load_s = (d.get("load_duration") or 0) / 1e9
            ps = ps_line(CURATOR)
            row = {
                "num_ctx": num_ctx, "prompt": label, "wall_s": round(wall, 1),
                "load_s": round(load_s, 1),
                "prompt_tokens": pe_n,
                "prefill_tps": round(pe_n / pe_s, 0) if pe_s else None,
                "gen_tps": round(ev_n / ev_s, 1) if ev_s else None,
                "vram_mb": vram_mb(), "ollama_ps": ps,
            }
            results.append(row)
            print(f"  {label}: prompt_tokens={pe_n} load={load_s:.1f}s "
                  f"prefill={row['prefill_tps']} t/s gen={row['gen_tps']} t/s "
                  f"VRAM={row['vram_mb']} MB")
            print(f"    ps: {ps}")

    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    out = Path(__file__).parent / f"num_ctx_{stamp}.md"
    lines = ["# num_ctx benchmark — curator + nomic resident",
             f"Date: {stamp}  |  model: {CURATOR}  |  GPU: RTX 4090 24GB",
             "",
             "| num_ctx | prompt | tokens | load s | prefill t/s | gen t/s | VRAM MB | ollama ps |",
             "|---|---|---|---|---|---|---|---|"]
    for r_ in results:
        if "error" in r_:
            lines.append(f"| {r_['num_ctx']} | {r_['prompt']} | ERROR {r_['error'][:40]} |")
            continue
        lines.append(f"| {r_['num_ctx']} | {r_['prompt']} | {r_['prompt_tokens']} "
                     f"| {r_['load_s']} | {r_['prefill_tps']} | {r_['gen_tps']} "
                     f"| {r_['vram_mb']} | {r_['ollama_ps'][:60]} |")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nreport: {out}")


if __name__ == "__main__":
    main()
