"""
Curatarr — Ollama model throughput benchmark.

Times a small set of LLMs on a Curatarr-style recommendation prompt with
and without ``think`` reasoning enabled. Writes per-run TPS, token count,
and raw output to a timestamped file in the repo root.

Use this when picking a new ``BASE_CURATOR_MODEL`` / ``BASE_SUMMARIZER_MODEL``
to see how a candidate model compares against your current pick on your
own hardware.

Usage::

    python benchmark.py
"""

import time
from datetime import datetime

import requests

# Models tested in this run. Edit the list to add/remove candidates —
# anything that exists in your local ``ollama list`` will work.
MODELS_TO_TEST = [
    "huihui_ai/deepseek-r1-abliterated:latest",  # The 5GB (8B) model
    "huihui_ai/deepseek-r1-abliterated:32b",     # The 19GB (32B) model
]

PROMPT = """
Analyze the following premise and write a short, analytical 'embedding_text' for a recommendation vector database.
Focus on psychological themes, character tropes, and subversion. Output ONLY valid JSON with the key "embedding_text".

Premise: A seemingly friendly high school student secretly manipulates his classmates in a ruthless point-based academic system.
He views everyone as tools and will do whatever it takes to win, maintaining a polite, unassuming facade while orchestrating psychological breakdowns.
"""


def unload_model(model_name):
    """Actively evict the model from VRAM by setting keep_alive=0."""
    print(f"🧹 Unloading model '{model_name}' from VRAM...")
    try:
        requests.post(
            "http://localhost:11434/api/generate",
            json={"model": model_name, "keep_alive": 0},
            timeout=10,
        )
        time.sleep(2)  # short pause so VRAM actually gets freed
    except Exception as e:
        print(f"Warning while unloading: {e}")


def run_test(model, think_disabled, file):
    url = "http://localhost:11434/api/generate"
    payload = {
        "model": model,
        "prompt": PROMPT,
        "stream": False,
        # Keep the model in memory across both passes (think on / off) so the
        # second pass doesn't pay the cold-load cost.
        "keep_alive": "5m",
        "options": {
            "num_predict": 4000,
            "num_ctx": 8192,        # matches Curatarr's runtime context window
            "temperature": 0.3,
        },
    }

    if think_disabled:
        payload["think"] = False

    header = f"\n{'='*70}\nModel: {model} | Think Disabled: {think_disabled}\n{'='*70}\n"
    print(f"▶ Starting test: {model} (Think Disabled: {think_disabled})...")
    file.write(header)

    start_time = time.time()

    try:
        # Generous timeout — VRAM load + 8192 ctx + 32B reasoning takes a while.
        response = requests.post(url, json=payload, timeout=300)

        if response.status_code != 200:
            error_msg = f"API Error {response.status_code}: {response.text}\n"
            print(error_msg.strip())
            file.write(error_msg)
            return

        data = response.json()
        duration = time.time() - start_time

        eval_count = data.get("eval_count", 0)
        eval_duration_ns = data.get("eval_duration", 1)
        tps = (eval_count / eval_duration_ns) * 1e9 if eval_duration_ns > 0 else 0

        raw_text = data.get("response", "")

        metrics = (
            f"Time: {duration:.2f}s | "
            f"Speed: {tps:.2f} TPS | "
            f"Total Tokens: {eval_count}\n"
            f"{'-'*70}\n"
        )
        file.write(metrics)
        file.write(raw_text + "\n")

        print(f"  ✓ Done in {duration:.1f}s ({tps:.1f} TPS)")

    except Exception as e:
        error_msg = f"Exception occurred: {str(e)}\n"
        print(error_msg.strip())
        file.write(error_msg)


if __name__ == "__main__":
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"benchmark_results_{timestamp}.txt"

    print(f"Starting Curatarr benchmark. Results will be written to '{filename}'...\n")

    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"Benchmark Run: {timestamp}\n")
        f.write(f"Context Window (num_ctx): 8192\n")

        for model in MODELS_TO_TEST:
            # Pass 1: with reasoning (thinking)
            run_test(model, think_disabled=False, file=f)

            # Pass 2: without reasoning (JSON mode)
            run_test(model, think_disabled=True, file=f)

            # Free VRAM before the next model gets loaded
            unload_model(model)

    print(f"\n✅ Benchmark complete. Full results saved to: {filename}")
