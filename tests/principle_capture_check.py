#!/usr/bin/env python3
"""P3 check: the WIRED principle-capture pipeline (extract + novelty-check via the
curator model, format-forced) on REAL deletion-debate threads. DRY-RUN — does NOT
write to curator_principles (this validates the LLM logic; the store is trivial).

    python tests/principle_capture_check.py
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

from src.services import curator_principles as cp


def taste() -> str:
    con = sqlite3.connect("data/curatarr.db", timeout=10)
    row = con.execute(
        "SELECT summary_text FROM taste_vectors WHERE user_id=1 LIMIT 1").fetchone()
    con.close()
    return (row[0] if row and row[0] else "")[:3500]


async def run():
    for tid, label in [
        ("deletion_proposal:1946", "America's National Parks (RESONANCE case)"),
        ("deletion_proposal:1961", "thin mainstream-classic question"),
    ]:
        convo, n = cp._thread_text(1, tid)
        print("=" * 72)
        print(f"### {label}  ({n} messages)")
        if n < cp._MIN_THREAD_MESSAGES:
            print("  too thin — skipped\n")
            continue
        ex = await cp._curator_json(cp._EXTRACT_SYS, "DEBATE:\n" + convo,
                                    cp._EXTRACT_SCHEMA, 700)
        cands = [c for c in (ex.get("principles") or [])
                 if c.get("principle") and c.get("basis") != "unresolved"]
        print(f"  EXTRACTED {len(cands)} principle(s):")
        for c in cands:
            print(f"    - [{c.get('basis')}] {c['principle']}")
        if not cands:
            print()
            continue
        # novelty vs a seeded rule-set (one near-duplicate) + the real taste blob
        seeded = ["The user rejects slow films that are mere scenic tourism without rigor."]
        texts = [c["principle"].strip() for c in cands]
        nv = await cp._curator_json(
            cp._NOVELTY_SYS, cp._build_novelty_user(texts, seeded, taste()),
            cp._NOVELTY_SCHEMA, 900)
        print("  NOVELTY vs 1 seeded rule + taste profile:")
        for r in (nv.get("results") or []):
            print(f"    {r.get('n')}. {r.get('verdict')} — rel:{(r.get('related') or '')[:36]!r}"
                  f"  {(r.get('reason') or '')[:64]}")
        print()


if __name__ == "__main__":
    asyncio.run(run())
