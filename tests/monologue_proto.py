#!/usr/bin/env python3
"""
Prototype v3 of the monologue prompt: characterize the title on ITS OWN terms,
feed the pillar finding as INTERNAL reasoning only ("don't quote it"), and forbid
reciting the user's tastes back at them — the source of the "you demand… /
incompatible with your appetite for industrial rave" boilerplate.

Hand-constructed inputs (3 music + 2 video) so music — the worst repetition — is
tested with realistic facts instead of being starved by a title-only DB lookup.
Monologue-only (no build_evidence/adjudicate), fast.

    python tests/monologue_proto.py
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

_STANCE = {
    "CUT": "This title does NOT earn its place — make the sharp case for removing it",
    "HARD_KEEP": "This title earns its place — make the sharp case for keeping it",
    "KEEP_WITH_FLAG": "This title earns its place for its stature, but its file is a "
                      "bitrate outlier — keep it AND note it should be downscaled",
}


def _governing(v: dict) -> str:
    if v.get("verdict") in ("HARD_KEEP", "KEEP_WITH_FLAG"):
        return v.get("pillar_2_archive") or v.get("pillar_1_ego") or ""
    return v.get("pillar_1_ego") or ""


def new_prompt(facts: str, verdict: dict) -> str:
    v = verdict.get("verdict")
    no_size = "" if v == "KEEP_WITH_FLAG" else "; do not mention file size or storage"
    return (
        "You are the curator. In your uncompromising, opinionated voice, write the "
        "2-3 sentence note the user reads on this title's card. "
        f"{_STANCE.get(v, 'Assess this title')}. Characterize what this title concretely "
        "IS — its premise, style, what it actually does — and let the verdict land on "
        "ITS own specifics, sharp and fresh. Do NOT recite the user's tastes back at "
        "them (they already know what they like); do NOT open with a verdict label or "
        f"the genre{no_size}.\n\n"
        f"{facts}\n\n"
        f"(For your reasoning only — do NOT quote this: {_governing(verdict)})\n\n"
        "Write the verdict. No headers, no lists."
    )


async def _monologue(prompt: str) -> str:
    payload = {
        "model": settings.CURATOR_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False, "think": False, "keep_alive": "10m",
        "options": {"temperature": 0.7, "num_predict": 500, "num_gpu": 99},
    }
    async with httpx.AsyncClient(timeout=300) as c:
        r = await c.post(f"{settings.effective_ollama}/api/chat", json=payload)
    r.raise_for_status()
    return clean_llm_text((r.json().get("message") or {}).get("content", "") or "")


# (facts, verdict) — realistic inputs reconstructed from the live scan.
CASES = [
    ("TITLE: King 810 — music, Heavy Metal / Nu Metal\nOWNER: not watched.\n"
     "OTHER HOUSEHOLD USERS:\n  none.\nACCLAIM & METADATA:\n  Nu-metal / heavy band "
     "out of Flint, Michigan; bleak, violent street imagery, mid-tempo riff-driven.",
     {"verdict": "CUT", "pillar_1_ego": "Nu-metal's plodding heaviness clashes with "
      "the owner's taste for high-velocity European hardcore / industrial rave."}),

    ("TITLE: Sisqó — music, R&B / Contemporary R&B / Hip Hop / Pop\nOWNER: not watched.\n"
     "OTHER HOUSEHOLD USERS:\n  none.\nACCLAIM & METADATA:\n  Late-90s/early-2000s "
     "R&B-pop solo singer (ex-Dru Hill); glossy melismatic vocals, romantic ballads, "
     "the 'Thong Song' novelty hit.",
     {"verdict": "CUT", "pillar_1_ego": "Glossy late-90s R&B-pop clashes with the "
      "owner's appetite for abrasive industrial / underground rave."}),

    ("TITLE: Coconut Hen — music, Dance / Pop\nOWNER: not watched.\n"
     "OTHER HOUSEHOLD USERS:\n  none.\nACCLAIM & METADATA:\n  Swedish viral novelty "
     "dance-pop act; bright four-on-the-floor earworms, deliberately silly 'joyful' hooks.",
     {"verdict": "CUT", "pillar_1_ego": "Sunny viral pop-dance clashes with the owner's "
      "demand for industrial grit and underground rave textures."}),

    ("TITLE: Blazing Transfer Student — anime, Action / Comedy\nOWNER: not watched.\n"
     "OTHER HOUSEHOLD USERS:\n  none.\nACCLAIM & METADATA:\n  Early-90s 2-part OVA; "
     "parody of hot-blooded shōnen — a transfer student must settle every dispute by "
     "sports/combat, wins fights and the girl, races to make it to class.",
     {"verdict": "CUT", "pillar_1_ego": "Dated shōnen slapstick + a schoolyard-respect "
      "and romance plot offer none of the moral ambiguity or weight the owner wants."}),

    ("TITLE: Small Town Scandal — show, Comedy / Drama / Mystery\nOWNER: not watched.\n"
     "OTHER HOUSEHOLD USERS:\n  none.\nACCLAIM & METADATA:\n  A disgraced journalist "
     "turned podcast host returns to his rural hometown to investigate his millionaire "
     "uncle's death — killed, suspiciously, by an automatic lawnmower.",
     {"verdict": "CUT", "pillar_1_ego": "A whimsical lawnmower-death gimmick over "
      "substance; lacks the cerebral depth the owner demands from a mystery."}),
]


async def run():
    for facts, verdict in CASES:
        title = facts.splitlines()[0].replace("TITLE: ", "")
        mono = await _monologue(new_prompt(facts, verdict))
        print("=" * 72)
        print(f"### {title}")
        print(mono + "\n")


if __name__ == "__main__":
    asyncio.run(run())
