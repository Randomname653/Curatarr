"""
Curatarr - Build Ollama Models
Pulls base models if not already present, then bakes system prompts
into curatarr-curator and curatarr-summarizer.

Run once after changing BASE_CURATOR_MODEL or BASE_SUMMARIZER_MODEL:
    python build_models.py
"""
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


async def main():
    endpoint        = settings.effective_ollama
    curator_base    = settings.BASE_CURATOR_MODEL
    summarizer_base = settings.BASE_SUMMARIZER_MODEL
    # Two-bake split: only build the pitcher when the split is enabled
    # (PITCHER_MODEL set in .env). Gating on BASE_PITCHER_MODEL instead
    # would pull 17 GB on every fresh install.
    pitcher_base    = settings.BASE_PITCHER_MODEL if (
        settings.PITCHER_MODEL or "").strip() else None

    print(f"\nOllama endpoint  : {endpoint}")
    print(f"Curator base     : {curator_base}  →  curatarr-curator")
    print(f"Summarizer base  : {summarizer_base}  →  curatarr-summarizer")
    if pitcher_base:
        print(f"Pitcher base     : {pitcher_base}  →  curatarr-pitcher")
    else:
        print("Pitcher          : disabled (PITCHER_MODEL empty) — skipping")
    print("\nMissing models will be pulled automatically.\n")
    print("─" * 60)

    results = await build_ollama_models(endpoint, curator_base, summarizer_base,
                                        base_pitcher=pitcher_base)

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

    if results.get("embedding"):
        print(f"✅  {settings.EMBEDDING_MODEL}  (embeddings) ready")
    else:
        print(f"❌  {settings.EMBEDDING_MODEL}  (embeddings) failed")
        print(f"    Could not pull '{settings.EMBEDDING_MODEL}' — without it, enrichment")
        print(f"    produces text but no vectors (every item stays vector_ready=0).")

    if all(results.values()):
        print("\n🎬  All models ready — restart Curatarr.\n")
    else:
        print("\n⚠️   Fix the errors above and run this script again.\n")
        sys.exit(1)


asyncio.run(main())
