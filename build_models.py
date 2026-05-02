"""
Curatarr - Build Ollama Models
Bakes system prompts into curatarr-curator and curatarr-summarizer.
Run once: python build_models.py
"""
import asyncio
import sys
sys.path.insert(0, ".")

from src.services.setup_wizard import build_ollama_models, CURATOR_SYSTEM_PROMPT, SUMMARIZER_SYSTEM_PROMPT
from src.config import settings


async def main():
    endpoint = settings.effective_ollama
    curator_base = settings.BASE_CURATOR_MODEL
    summarizer_base = settings.BASE_SUMMARIZER_MODEL

    print(f"\nOllama endpoint: {endpoint}")
    print(f"Building curatarr-curator   ← {curator_base}")
    print(f"Building curatarr-summarizer ← {summarizer_base}")
    print("\nThis may take a moment...\n")

    results = await build_ollama_models(endpoint, curator_base, summarizer_base)

    if results.get("curator"):
        print("✅ curatarr-curator created successfully")
    else:
        print(f"❌ curatarr-curator failed — is '{curator_base}' pulled in Ollama?")
        print(f"   Run: ollama pull {curator_base}")

    if results.get("summarizer"):
        print("✅ curatarr-summarizer created successfully")
    else:
        print(f"❌ curatarr-summarizer failed — is '{summarizer_base}' pulled in Ollama?")
        print(f"   Run: ollama pull {summarizer_base}")

    if all(results.values()):
        print("\n🎬 Models ready. Restart Curatarr.")
    else:
        print("\n⚠️  Fix the errors above and run this script again.")


asyncio.run(main())
