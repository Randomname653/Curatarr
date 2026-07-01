#!/usr/bin/env python3
"""
Validate the LLM NOVELTY CHECK: given the EXISTING rule-set (the real taste
profile + a seed rule), classify each freshly-extracted candidate principle as
NEW / DUPLICATE / REFINEMENT / CONTRADICTION — WITHOUT rewriting it (decide only,
no drift). Tests whether the LLM correctly routes: Resonance = new, generic
taste = duplicate-of-profile, "Foundational Niche Media" = contradiction of the
conceded 'precursor != stature' rule.

    python tests/novelty_check_proto.py
"""
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

# Seed rule the user CONCEDED in the Blazing debate (the anti-gaming guard).
SEED_RULES = [
    "A title's status as a historical precursor or foundational piece does NOT "
    "automatically grant the objective stature required for archival preservation.",
]

# Freshly-extracted candidates from the earlier real-thread runs.
CANDIDATES = [
    "A distinction must be made between 'sublime observation' (kept), which evokes awe or a sense of scale, and 'generic filler'/tourism (deleted).",
    "The library must balance high-intensity friction with meditative depth to prevent burnout and allow cognitive decompression.",
    "Titles must provide intellectual friction and avoid blandness.",
    "Titles must possess philosophical weight, particularly existentialism or man vs machine.",
    "Titles may be preserved as 'Foundational Niche Media' if they serve as primary source documents for the evolution of a preferred aesthetic.",
    "Technical competence alone is insufficient for retention; a title must demonstrate artistic mastery or intentionality in its execution.",
    "High file size relative to the class median raises the bar for the psychological or intellectual justification required to keep a title.",
    "Household-protected titles kept for other users should be optimized for storage efficiency rather than kept at maximum fidelity.",
]


def taste_profile() -> str:
    con = sqlite3.connect("data/curatarr.db", timeout=10)
    row = con.execute(
        "SELECT summary_text FROM taste_vectors WHERE user_id = 1 LIMIT 1").fetchone()
    con.close()
    return (row[0] if row and row[0] else "")[:3500]


def build_prompt() -> str:
    existing = "TASTE PROFILE (existing knowledge):\n" + taste_profile() + \
        "\n\nEXISTING RULES:\n" + "\n".join(f"- {r}" for r in SEED_RULES)
    cands = "\n".join(f"{i+1}. {c}" for i, c in enumerate(CANDIDATES))
    return (
        "You maintain a media curator's RULE-SET. Below is the EXISTING knowledge "
        "(taste profile + rules). For each CANDIDATE principle, decide how it "
        "relates to what is ALREADY known. DO NOT rewrite the candidate — only "
        "classify it.\n\n"
        "verdict ∈ NEW (adds knowledge not present), DUPLICATE (already covered by "
        "the profile/rules), REFINEMENT (sharpens an existing point), CONTRADICTION "
        "(conflicts with an existing rule).\n\n"
        f"{existing}\n\nCANDIDATES:\n{cands}\n\n"
        'Output ONLY a JSON list, one per candidate: '
        '{"n": <num>, "verdict": "...", "related": "<few words of the existing '
        'rule/profile point, or ->", "reason": "<one line>"}.'
    )


def main():
    payload = {"model": "gemma4:31b",
               "messages": [{"role": "user", "content": build_prompt()}],
               "stream": False, "think": False, "keep_alive": "10m",
               "options": {"temperature": 0.1, "num_predict": 900, "num_gpu": 99, "num_ctx": 8192}}
    r = httpx.post(f"{settings.effective_ollama}/api/chat", json=payload, timeout=400)
    r.raise_for_status()
    out = clean_llm_text((r.json().get("message") or {}).get("content", "") or "")
    print("EXPECTED: 1,2,6,7,8=NEW/REFINEMENT · 3,4=DUPLICATE · 5=CONTRADICTION\n")
    print(out)


if __name__ == "__main__":
    main()
