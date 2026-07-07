"""
Director-note prototype — TEST BEFORE BUILD (stdlib runner, no pytest).

Live-action counterpart to the validated studio-notes layer: what a DIRECTOR
is known for (style, reputation, signature works) as evidence. 4,303 distinct
directors across movie/show docs -> JIT-only (no bulk walker); this proto
measures Wikipedia coverage + homonym risk on a mix of famous and long-tail
directors from the user's real library, then condenses 3 samples.

Run from repo root:  python tests/director_proto.py
"""
import asyncio
import json
import random
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import httpx

from src.config import settings
from src.services.llm_utils import (clean_llm_text, ollama_options,
                                    strip_think_tags, SUMMARIZER_KEEP_ALIVE)

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_HEADERS = {"User-Agent": "Curatarr/1.0 (https://github.com/Randomname653/curatarr; "
                              "personal media curator) python-httpx"}

PERSON_HINT = re.compile(r"director|filmmaker|screenwriter|animator|producer", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def pick_directors(n_common: int = 8, n_tail: int = 8) -> list[str]:
    db = sqlite3.connect("data/cache/enrichment.db")
    c = Counter()
    for pat in ("v2:raw:movie:%", "v2:raw:show:%"):
        for (v,) in db.execute("SELECT response FROM api_cache WHERE cache_key LIKE ?", (pat,)):
            try:
                d = json.loads(v)
            except Exception:
                continue
            dr = d.get("director")
            if dr and isinstance(dr, str) and "," not in dr:
                c[dr] += 1
    common = [d for d, _ in c.most_common(n_common)]
    tail_pool = [d for d, n in c.items() if n == 1]
    random.seed(7)
    tail = random.sample(tail_pool, min(n_tail, len(tail_pool)))
    return common + tail


async def wiki_person_lead(client: httpx.AsyncClient, name: str) -> tuple[str, str]:
    want = _norm(name)
    for query in (name, f"{name} film director"):
        try:
            sr = await client.get(WIKI_API, params={
                "action": "query", "list": "search", "format": "json",
                "srsearch": query, "srlimit": 5})
            hits = (sr.json().get("query", {}).get("search", [])
                    if sr.status_code == 200 else [])
        except Exception:
            hits = []
        for h in hits[:4]:
            t = h.get("title") or ""
            if want not in _norm(t):
                continue
            ex = await client.get(WIKI_API, params={
                "action": "query", "prop": "extracts", "format": "json",
                "titles": t, "exintro": 1, "explaintext": 1, "redirects": 1})
            pages = (ex.json().get("query") or {}).get("pages") or {}
            extract = next(iter(pages.values()), {}).get("extract") or ""
            if PERSON_HINT.search(extract) and len(extract) > 200:
                return t, extract
    return "", ""


CONDENSE_SYS = (
    "You write a one-line DIRECTOR NOTE for a media curator's evidence file. "
    "Use ONLY the text given. State what the director is KNOWN FOR — style, "
    "reputation, signature works. Skip birth dates, family and biography. "
    "2 sentences maximum, plain prose. If the text documents no style or "
    "reputation, output exactly: NONE")


async def condense(client, name, extract):
    for model in (settings.SUMMARIZER_MODEL, settings.BASE_SUMMARIZER_MODEL):
        if not model:
            continue
        r = await client.post(f"{settings.effective_ollama}/api/chat", json={
            "model": model,
            "messages": [{"role": "system", "content": CONDENSE_SYS},
                         {"role": "user", "content": f"DIRECTOR: {name}\n\nTEXT:\n{extract[:2400]}"}],
            "stream": False, "keep_alive": SUMMARIZER_KEEP_ALIVE,
            **ollama_options(temperature=0.1, num_predict=200)})
        if r.status_code != 200:
            continue
        out = clean_llm_text(strip_think_tags(
            r.json().get("message", {}).get("content", "") or "")).strip()
        if out:
            return out
    return ""


async def main():
    names = pick_directors()
    print(f"Testing {len(names)} directors (8 common + {len(names)-8} long-tail)\n")
    found = 0
    leads = {}
    async with httpx.AsyncClient(timeout=30, headers=WIKI_HEADERS) as client:
        for name in names:
            t, extract = await wiki_person_lead(client, name)
            if extract:
                found += 1
                leads[name] = extract
                print(f"### {name} -> {t!r}")
                print(f"    {re.sub(r'[\\s]+', ' ', extract)[:150]}…")
            else:
                print(f"### {name} -> no matching article")
            await asyncio.sleep(0.4)
    print(f"\ncoverage: {found}/{len(names)}")

    print("\n" + "=" * 70)
    print("CONDENSATION SAMPLES")
    print("=" * 70)
    async with httpx.AsyncClient(timeout=180) as client:
        for name in list(leads)[:2] + list(leads)[-1:]:
            note = await condense(client, name, leads[name])
            print(f"\n### {name}\n    {note}")


if __name__ == "__main__":
    asyncio.run(main())
