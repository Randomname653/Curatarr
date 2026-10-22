"""
Curatarr - Build Ollama Models
Pulls base models if not already present, then bakes system prompts
into curatarr-curator and curatarr-summarizer.

Run once after changing BASE_CURATOR_MODEL or BASE_SUMMARIZER_MODEL:
    python build_models.py

A split install (Curatarr on a NAS, the gaming PC's card as a second host:
OLLAMA_PC_ENDPOINT) builds the same bake names on each machine, from a base
that fits its card:
    python build_models.py --host main     this install's Ollama (BASE_*)
    python build_models.py --host pc       the PC's (PC_BASE_*, its window)
    python build_models.py --suggest 16    what the bench recommends for a 16 GB card
    python build_models.py --check         both hosts, and the embedding weights
"""
import argparse
import asyncio
import sys
sys.path.insert(0, ".")

# Windows consoles default to cp1252, which crashes on the → / ✅ in our
# prints. Force UTF-8 so the rebuild never dies on a status line.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.services.setup_wizard import build_ollama_models
from src.config import settings


async def main(host: str = "main"):
    from src.services.model_check import host_plan
    plan = host_plan(host)
    endpoint        = plan["url"]
    curator_base    = plan["curator"]
    summarizer_base = plan["summarizer"]
    # Two-bake split: only build the pitcher when the split is enabled
    # (PITCHER_MODEL set in .env). Gating on BASE_PITCHER_MODEL instead
    # would pull 17 GB on every fresh install. On a split install it lives
    # on the PC only: deletion runs wait for the PC.
    pitcher_base    = plan["pitcher"]

    print(f"\nHost             : {host}  ({endpoint}, curator window {plan['num_ctx']})")
    print(f"Curator base     : {curator_base}  →  curatarr-curator")
    print(f"Summarizer base  : {summarizer_base}  →  curatarr-summarizer")
    if pitcher_base:
        print(f"Pitcher base     : {pitcher_base}  →  curatarr-pitcher")
    else:
        print("Pitcher          : not on this host — skipping")
    if not plan["embedding"]:
        print("Embeddings       : not on this host (a split install embeds on the main host)")
    print("\nMissing models will be pulled automatically.\n")
    print("─" * 60)

    results = await build_ollama_models(endpoint, curator_base, summarizer_base,
                                        base_pitcher=pitcher_base, num_ctx=plan["num_ctx"],
                                        with_embedding=bool(plan["embedding"]))

    print("─" * 60)
    if results.get("curator"):
        print("✅  curatarr-curator    ready")
    else:
        print(f"❌  curatarr-curator    failed")
        print(f"    Is '{curator_base}' available on Ollama Hub?")

    if results.get("summarizer"):
        print("✅  curatarr-summarizer ready")
    else:
        print(f"❌  curatarr-summarizer failed")
        print(f"    Is '{summarizer_base}' available on Ollama Hub?")

    if pitcher_base:
        if results.get("pitcher"):
            print("✅  curatarr-pitcher    ready")
        else:
            print(f"❌  curatarr-pitcher    failed")
            print(f"    Is '{pitcher_base}' available on Ollama Hub?")

    if plan["embedding"]:
        if results.get("embedding"):
            print(f"✅  {plan['embedding']}  (embeddings) ready")
        else:
            print(f"❌  {plan['embedding']}  (embeddings) failed")
            print(f"    Could not pull '{plan['embedding']}' — without it, enrichment")
            print(f"    produces text but no vectors (every item stays vector_ready=0).")

    if all(results.values()):
        print("\n🎬  All models ready — restart Curatarr.\n")
    else:
        print("\n⚠️   Fix the errors above and run this script again.\n")
        sys.exit(1)


def check() -> int:
    """``--check``: 0 = every model each host runs on is present, 1 = some
    are missing (named; start.bat builds them), 2 = an Ollama does not
    answer, 3 = the main host's embedding weights differ from the ones the
    stored vectors were made with (a build cannot fix that - start.bat only
    warns). Over HTTP, so it works in the container too. The first check
    records which embedding weights the vectors come from - run it on the
    old machine before a move."""
    from src.services.model_check import (curator_fit_warning, host_plan, missing_on,
                                          record_embedding_digest)
    from src.services.ollama_hosts import split
    rc = 0
    for host in (["main", "pc"] if split() else ["main"]):
        plan = host_plan(host)
        missing = missing_on(plan["url"], plan["models"])
        if missing is None:
            print(f"[{host}] Ollama at {plan['url']} is not answering - its models cannot be checked.")
            rc = max(rc, 2)
        elif missing:
            print(f"[{host}] Missing Ollama models: " + ", ".join(missing)
                  + f"   (python build_models.py --host {host})")
            rc = max(rc, 1)
        else:
            print(f"[{host}] Ollama models present: " + ", ".join(plan["models"]))
    ident = record_embedding_digest()
    if ident["status"] == "mismatch":
        print(f"Embedding weights DIFFER: {ident['model']} on the main host is "
              f"{(ident['host'] or '')[:12]}, the stored vectors were made with "
              f"{ident['recorded'][:12]}. New vectors would not compare with the old ones - "
              f"pull the same weights, or re-embed the library.")
        rc = 3 if rc == 0 else rc
    elif ident["status"] == "match":
        print(f"Embedding weights match the stored vectors ({ident['model']} "
              f"{ident['recorded'][:12]}).")
    warning = curator_fit_warning()
    if warning:
        # A warning, not a failure: the models ARE present, and an operator
        # who set CURATOR_NUM_GPU=-1 on purpose has chosen the slow path.
        print("Warning: " + warning)
    return rc


def suggest(vram_gb: float) -> int:
    """What the hardware bench recommends for a card of ``vram_gb``. Reads
    the bench's matrix only - talks to no Ollama."""
    from src.services.model_catalog import hardware_package
    pkg = hardware_package(vram_gb)
    rec = (pkg or {}).get("recommended")
    if not rec:
        print((pkg or {}).get("note") or "No bench data for this card.")
        return 1
    exp = rec.get("expected") or {}
    print(f"{vram_gb:g} GB card - the bench's package (tier {pkg['tier_gb']} GB):")
    print(f"  curator base     : {rec['model']}")
    print(f"  profile / window : {rec.get('profile')} / {rec.get('num_ctx')}"
          f"   (LLM_PROFILE / CURATOR_NUM_CTX, or PC_* for the PC)")
    print(f"  num_gpu          : {rec.get('num_gpu')}   (CURATOR_NUM_GPU)")
    if rec.get("server"):
        print("  Ollama server    : " + ", ".join(f"{k}={v}" for k, v in rec["server"].items()))
    if exp:
        print(f"  measured         : {exp.get('gen_tok_s')} tok/s, verdict {exp.get('judge_s')} s, "
              f"{exp.get('vram_gb')} GB VRAM")
    print("  The bench ran on a card of its own: a card shared with other apps "
          "(Plex transcodes, Immich) has less room than its size.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build or check Curatarr's Ollama bakes")
    ap.add_argument("--check", action="store_true", help="check every host's models")
    ap.add_argument("--host", choices=("main", "pc"), default="main",
                    help="which Ollama to build on (pc = OLLAMA_PC_ENDPOINT)")
    ap.add_argument("--suggest", type=float, metavar="VRAM_GB",
                    help="print the bench's recommendation for a card this size")
    args = ap.parse_args()
    if args.check:
        sys.exit(check())
    if args.suggest is not None:
        sys.exit(suggest(args.suggest))
    asyncio.run(main(args.host))
