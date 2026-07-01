#!/usr/bin/env python3
"""
Validate the REPETITION GATE idea: extract principles from many real threads,
embed them, cluster semantically, and count how many DISTINCT threads produced
each principle. A real principle should recur across threads (→ would pass the
gate); noise / redundant one-offs should stay singletons (→ gate holds them).

    python tests/repetition_gate_proto.py
"""
import asyncio
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import httpx
import numpy as np
from src.config import settings

SIM_THRESHOLD = 0.78   # nomic cosine: "same principle" — tunable
N_THREADS = 8

PROMPT = """You are analyzing a conversation between a USER (the OWNER of a media library — the FINAL authority on their taste) and their AI curator about keeping/deleting/recommending titles.

Extract the GENERALIZABLE CURATION PRINCIPLES that emerged — lasting rules for FUTURE judgments on OTHER titles.

STRICT RULES:
- PRINCIPLES, never title verdicts.
- A principle counts ONLY if the USER established or endorsed/conceded to it. The curator's own un-endorsed self-justifications do NOT count.
- Generalizable, title-agnostic, ONE sentence each.
- Be CONSERVATIVE — if nothing was genuinely settled, output [].

Output ONLY a JSON list of strings (the principles).

CONVERSATION:
"""


def pick_threads():
    con = sqlite3.connect("data/curatarr.db", timeout=10)
    rows = con.execute(
        "SELECT thread_id FROM conversation_messages GROUP BY thread_id "
        "HAVING COUNT(*) >= 6 ORDER BY MAX(created_at) DESC LIMIT ?", (N_THREADS,)
    ).fetchall()
    con.close()
    return [r[0] for r in rows]


def thread_text(tid: str) -> str:
    con = sqlite3.connect("data/curatarr.db", timeout=10)
    rows = con.execute(
        "SELECT role, content FROM conversation_messages WHERE thread_id = ? "
        "ORDER BY created_at, id", (tid,)).fetchall()
    con.close()
    return "\n\n".join(f"{'USER' if r[0]=='user' else 'CURATOR'}: {(r[1] or '').strip()}"
                       for r in rows)


async def extract(text: str) -> list[str]:
    payload = {"model": "gemma4:31b",
               "messages": [{"role": "user", "content": PROMPT + text}],
               "stream": False, "think": False, "keep_alive": "10m",
               "options": {"temperature": 0.2, "num_predict": 600, "num_gpu": 99, "num_ctx": 8192}}
    async with httpx.AsyncClient(timeout=400) as c:
        r = await c.post(f"{settings.effective_ollama}/api/chat", json=payload)
    r.raise_for_status()
    raw = (r.json().get("message") or {}).get("content", "") or ""
    raw = raw[raw.find("["): raw.rfind("]") + 1]
    try:
        items = json.loads(raw)
        return [str(x.get("principle") if isinstance(x, dict) else x) for x in items]
    except Exception:
        return []


async def embed(text: str):
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{settings.effective_ollama}/api/embeddings",
                         json={"model": settings.EMBEDDING_MODEL, "prompt": "search_document: " + text})
    return np.asarray(r.json()["embedding"], dtype=float)


def cos(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


async def run():
    tids = pick_threads()
    print(f"Threads: {tids}\n")
    items = []   # {text, thread, vec}
    for tid in tids:
        prins = await extract(thread_text(tid))
        print(f"[{tid}] {len(prins)} principle(s)")
        for p in prins:
            items.append({"text": p, "thread": tid, "vec": await embed(p)})

    # greedy single-link clustering
    clusters = []
    for it in items:
        for cl in clusters:
            if any(cos(it["vec"], m["vec"]) >= SIM_THRESHOLD for m in cl):
                cl.append(it)
                break
        else:
            clusters.append([it])

    clusters.sort(key=lambda cl: len({m["thread"] for m in cl}), reverse=True)
    print(f"\n{'='*72}\nCLUSTERS (reinforcement = # distinct threads, threshold {SIM_THRESHOLD})\n{'='*72}")
    for cl in clusters:
        threads = {m["thread"] for m in cl}
        gate = "PASS (reinforced)" if len(threads) >= 2 else "hold (one-off)"
        print(f"\n×{len(threads)} threads → {gate}   {sorted(threads)}")
        for m in cl:
            print(f"    - [{m['thread']}] {m['text']}")


if __name__ == "__main__":
    asyncio.run(run())
