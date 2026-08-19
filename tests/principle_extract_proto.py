#!/usr/bin/env python3
"""
Prototype: extract LEARNABLE CURATION PRINCIPLES from a REAL debate transcript —
reading BOTH sides, but only counting what the USER established or endorsed.
Tests whether "learn from the dialectic, gated by the user's agreement" actually
pulls the right rules from the conversations the user had with the curator.

    python tests/principle_extract_proto.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import httpx
from src.config import settings
from src.services.llm_utils import clean_llm_text

SRC = r"C:\path	o\debate_transcript.txt"   # local-only input, set before running

PROMPT = """You are analyzing a DEBATE between a USER (the OWNER of a media library — the FINAL authority on their own taste) and their AI curator, about whether to keep or delete titles.

Extract the GENERALIZABLE CURATION PRINCIPLES that emerged — lasting rules that should guide FUTURE judgments on OTHER titles.

STRICT RULES:
- Extract PRINCIPLES, never title verdicts. ("Don't mistake thematic darkness for genuine depth" = YES. "Delete Blazing Transfer Student" = NO.)
- A principle counts ONLY if the USER established it OR endorsed/conceded to it. The curator's OWN un-endorsed self-justifications do NOT count. If the user got rhetorically cornered but did NOT actually concede, that is NOT an endorsed principle.
- Each principle: generalizable, title-agnostic, ONE sentence.
- If nothing was genuinely settled or endorsed by the user, output an empty list [].

Output ONLY a JSON list, each item: {"principle": "...", "basis": "user_established | user_endorsed_curator | converged | unresolved"}.

DEBATE:
"""


def arc(lo: int, hi: int) -> str:
    with open(SRC, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    out = []
    for ln in lines[lo - 1:hi]:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("INFO:") or s.startswith("2026-") or "HTTP/1.1" in s:
            continue
        out.append(s)
    return "\n".join(out)


async def extract(name: str, text: str):
    payload = {
        "model": "gemma4:31b",
        "messages": [{"role": "user", "content": PROMPT + text}],
        "stream": False, "think": False, "keep_alive": "10m",
        "options": {"temperature": 0.2, "num_predict": 600, "num_gpu": 99, "num_ctx": 8192},
    }
    async with httpx.AsyncClient(timeout=400) as c:
        r = await c.post(f"{settings.effective_ollama}/api/chat", json=payload)
    r.raise_for_status()
    out = clean_llm_text((r.json().get("message") or {}).get("content", "") or "")
    print("=" * 72)
    print(f"### {name}   (input ~{len(text)} chars)")
    print(out + "\n")


async def run():
    await extract("BLAZING TRANSFER arc — curator holds Pillar II, user concedes",
                  arc(2982, 3176))
    await extract("BUTTERFLY EFFECT arc — contested / unresolved",
                  arc(4808, 5110))


if __name__ == "__main__":
    asyncio.run(run())
