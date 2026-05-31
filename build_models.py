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

    print(f"\nOllama endpoint  : {endpoint}")
    print(f"Curator base     : {curator_base}  →  curatarr-curator")
    print(f"Summarizer base  : {summarizer_base}  →  curatarr-summarizer")
    print("\nMissing models will be pulled automatically.\n")
    print("─" * 60)

    results = await build_ollama_models(endpoint, curator_base, summarizer_base)

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
