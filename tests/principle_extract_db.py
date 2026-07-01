#!/usr/bin/env python3
"""
Principle-extraction test on REAL, THINNER chat threads from the DB — to see what
the learning loop would (or wouldn't) learn from everyday conversations, and
whether it OVER-extracts (invents rules from thin / non-principled chat).

    python tests/principle_extract_db.py
"""
import asyncio
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import httpx
from src.config import settings
from src.services.llm_utils import clean_llm_text

PROMPT = """You are analyzing a conversation between a USER (the OWNER of a media library — the FINAL authority on their own taste) and their AI curator, about keeping/deleting/recommending titles.

Extract the GENERALIZABLE CURATION PRINCIPLES that emerged — lasting rules that should guide FUTURE judgments on OTHER titles.

STRICT RULES:
- Extract PRINCIPLES, never title verdicts. ("The user values slow films only when they show technical mastery" = YES. "Keep America's National Parks" = NO.)
- A principle counts ONLY if the USER established it OR endorsed/conceded to it. The curator's OWN un-endorsed self-justifications do NOT count.
- Each principle: generalizable, title-agnostic, ONE sentence.
- Be CONSERVATIVE. Most everyday chats establish NO lasting principle — if nothing was genuinely settled or endorsed, output an empty list []. Do NOT invent principles to fill space.

Output ONLY a JSON list, each item: {"principle": "...", "basis": "user_established | user_endorsed_curator | converged | unresolved"}.

CONVERSATION:
"""


def thread_text(tid: str) -> str:
    con = sqlite3.connect("data/curatarr.db", timeout=10)
    rows = con.execute(
        "SELECT role, content FROM conversation_messages WHERE thread_id = ? "
        "ORDER BY created_at, id", (tid,)).fetchall()
    con.close()
    out = []
    for role, content in rows:
        who = "USER" if role == "user" else "CURATOR"
        out.append(f"{who}: {(content or '').strip()}")
    return "\n\n".join(out)


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
    print(f"### {name}   (transcript ~{len(text)} chars)")
    print(out + "\n")


async def run():
    for tid, label in [
        ("deletion_proposal:1946", "America's National Parks (RESONANCE case: sublime vs tourism)"),
        ("deletion_proposal:1961", "thin mainstream-classic question"),
        ("general", "recommendation chat (Better Call Saul) — not a deletion"),
    ]:
        await extract(f"{tid} — {label}", thread_text(tid))


if __name__ == "__main__":
    asyncio.run(run())
