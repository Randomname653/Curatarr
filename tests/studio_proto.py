"""
Studio-profile prototype — TEST BEFORE BUILD (stdlib runner, no pytest).

The Princess Lover! debate flipped on OUTSIDE knowledge: "early GoHands,
chaotic experimental visual language" — the curator knew the studio only as
a name and had to take the user's word for it. A studio-profile layer
(Wikipedia lead -> 2-3 condensed sentences of style/reputation) closes that:
the curator can then VERIFY such claims instead of blindly folding.

This proto measures, on 20 studios from the user's real library:
  A. Wikipedia coverage — does a matching article exist? does the lead say
     anything about STYLE/reputation (not just founding dates)?
  B. Condensation — can the 8B summarizer distill a curator-usable studio
     note (2 samples incl. GoHands, the debate case)?

Run from repo root:  python tests/studio_proto.py
"""
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import httpx

from src.config import settings
from src.services.llm_utils import (clean_llm_text, ollama_options,
                                    strip_think_tags, SUMMARIZER_KEEP_ALIVE)

WIKI_API = "https://en.wikipedia.org/w/api.php"
# Wikipedia 403s generic UAs — same descriptive header the enricher uses.
WIKI_HEADERS = {"User-Agent": "Curatarr/1.0 (https://github.com/Randomname653/curatarr; "
                              "personal media curator) python-httpx"}

STUDIOS = [
    "GoHands", "MADHOUSE", "J.C.STAFF", "Sunrise", "Studio DEEN",
    "Production I.G", "Toei Animation", "A-1 Pictures", "LIDENFILMS",
    "GONZO", "Shaft", "bones", "MAPPA", "Gainax", "Doga Kobo",
    "SILVER LINK.", "P.A.WORKS", "Felix Film", "feel.", "project No.9",
]

STYLE_HINT = re.compile(
    r"known for|noted for|distinctive|visual style|reputation|acclaim"
    r"|signature|praised|criticized|style of", re.I)


async def wiki_studio_lead(client: httpx.AsyncClient, studio: str) -> tuple[str, str]:
    """(article_title, lead_extract) or ("", "")."""
    r = await client.get(WIKI_API, params={
        "action": "query", "list": "search", "format": "json",
        "srsearch": f"{studio} animation studio", "srlimit": 5,
    })
    hits = (((r.json().get("query") or {}).get("search")) or []) if r.status_code == 200 else []
    for h in hits[:3]:
        t = h.get("title") or ""
        r2 = await client.get(WIKI_API, params={
            "action": "query", "prop": "extracts", "format": "json",
            "titles": t, "exintro": 1, "explaintext": 1, "redirects": 1,
        })
        pages = ((r2.json().get("query") or {}).get("pages")) or {}
        extract = next(iter(pages.values()), {}).get("extract") or ""
        low = extract.lower()
        # must actually BE an animation studio article, not a homonym
        if ("studio" in low or "animation" in low) and len(extract) > 200:
            return t, extract
    return "", ""


CONDENSE_SYS = (
    "You write a one-line STUDIO NOTE for a media curator's evidence file. "
    "Use ONLY the text given. State what the studio is KNOWN FOR — its visual "
    "style, reputation, signature works. Skip founding dates, corporate "
    "history and ownership. 2 sentences maximum, plain prose. If the text "
    "documents no style or reputation, output exactly: NONE")


async def condense(client: httpx.AsyncClient, studio: str, extract: str) -> str:
    for model in (settings.SUMMARIZER_MODEL, settings.BASE_SUMMARIZER_MODEL):
        if not model:
            continue
        try:
            r = await client.post(f"{settings.effective_ollama}/api/chat", json={
                "model": model,
                "messages": [{"role": "system", "content": CONDENSE_SYS},
                             {"role": "user",
                              "content": f"STUDIO: {studio}\n\nTEXT:\n{extract[:2400]}"}],
                "stream": False,
                "keep_alive": SUMMARIZER_KEEP_ALIVE,
                **ollama_options(temperature=0.1, num_predict=200),
            })
            if r.status_code != 200:
                continue
            out = clean_llm_text(strip_think_tags(
                r.json().get("message", {}).get("content", "") or "")).strip()
            if out:
                return out
        except Exception as e:
            print(f"    condense failed via {model}: {e}")
    return ""


async def main():
    found = style = 0
    leads: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=30, headers=WIKI_HEADERS) as client:
        for s in STUDIOS:
            title, extract = await wiki_studio_lead(client, s)
            if extract:
                found += 1
                hint = bool(STYLE_HINT.search(extract))
                style += hint
                leads[s] = extract
                first = re.sub(r"\s+", " ", extract)[:180]
                print(f"### {s} -> {title!r}  style-signal: {'YES' if hint else 'no'}")
                print(f"    {first}…")
            else:
                print(f"### {s} -> NO ARTICLE")
            await asyncio.sleep(0.4)
    print(f"\ncoverage: {found}/{len(STUDIOS)} with article, "
          f"{style}/{len(STUDIOS)} with explicit style/reputation language")

    print("\n" + "=" * 70)
    print("CONDENSATION SAMPLES")
    print("=" * 70)
    async with httpx.AsyncClient(timeout=180) as client:
        for s in ("GoHands", "Shaft", "MADHOUSE"):
            if s not in leads:
                continue
            note = await condense(client, s, leads[s])
            print(f"\n### {s}\n    {note}")


if __name__ == "__main__":
    asyncio.run(main())
