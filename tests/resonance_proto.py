#!/usr/bin/env python3
"""
Phase-1 prototype: the RESONANCE 4-pillar constitution + 3-part litmus + STAGNANT
verdict, tested on 6 edge titles BEFORE touching pillars.py.

    python tests/resonance_proto.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import httpx
from src.config import settings
from src.services.llm_utils import parse_llm_json

CONSTITUTION = """You are the curation court for Curatarr, deciding whether ONE title stays on a shared home server. Judge it against FOUR pillars in STRICT priority — a higher pillar's protection can NEVER be overruled by a lower one. Base every word ONLY on the FACTS given; never invent data. Default to demanding EXCELLENCE: a title EARNS its place; it is never kept merely for "not being bad".

PILLAR III — HOUSEHOLD (highest, Sacred). If the facts show ANOTHER household user (not the owner) genuinely engaged with — above all COMPLETED — this title, it is protected for them regardless of the owner's taste. QUALITY FLOOR: this protects the title's EXISTENCE, not its fidelity — if such a household title is objectively mediocre, KEEP it but flag it for downscaling (don't hoard 4K space on mediocrity someone merely watched). A title another user only sampled and abandoned does NOT trigger this pillar.

PILLAR II — CUSTODIAN (Archive). A title of genuine OBJECTIVE stature — a landmark or masterwork of its form, or a rare work at real risk of being lost — is preserved even against the owner's taste. High critical acclaim (Rotten Tomatoes / Metacritic) and major awards are your evidence; use judgment, not a fixed number. Mere competence, popularity, or being a "precursor / foundational to a style" is NOT objective stature.

PILLAR I — RESONANCE (Expansion). This protects the QUIET intellect — sublime observation, meditative depth, patient exploration: works that hum rather than scream, offering awe and a mental reset rather than adrenaline. BUT to keep this from becoming a backdoor for boredom, a slow / low-friction title must PASS a 3-part LITMUS or it is Generic Filler:
  1. INTENT — Observation, not Tourism: does it capture the essence/weight of its subject and invite contemplation, rather than treat it as a pleasant checklist of attractions?
  2. AWE, not Comfort: does it evoke awe (fear + respect + wonder; feeling small in a stimulating way), rather than mere soothing comfort / the absence of tension?
  3. RIGOR — Mastery, not Competence: is its slowness intentional and masterful (pacing, craft, insight), rather than a generic formula any studio could produce?
A title that FAILS the litmus is filler and drops to Pillar 0.

PILLAR 0 — EGO (lowest, the Edge). The owner's elite, uncompromising taste: psychological friction, calculating "polite-monster" intelligence, taboo-breaking, stylistic/kinetic brilliance. OFFENSIVE, not defensive: a title must ACTIVELY provide intellectual or stylistic stimulation to survive here — not merely "not be bad". Beware PREMISE vs EXECUTION: a work whose premise CLAIMS depth (dark themes, high-concept) but whose EXECUTION is populist, manipulative, or generic does NOT pass — darkness is not depth. Lazy fan-service, sanitized kitsch, and crass novelty are CUT.

BITRATE is a SEPARATE axis from retention: a kept title that is a clear bitrate outlier may be flagged for downscaling; bitrate alone never deletes.

VERDICTS:
- HARD_KEEP — protected by III (sacred) / II (masterwork) / I (passes the Resonance litmus) at sane bitrate, or a strong Pillar-0 Edge match.
- KEEP_WITH_FLAG — kept, but a clear bitrate outlier worth downscaling (includes the Household Quality Floor).
- CUT — no pillar protects it: fails the Ego edge, fails the Resonance litmus, no stature, no household claim.
- STAGNANT — the gray zone: not bad enough to cut, but merely "fine" — it neither champions the owner's edge nor passes the Resonance litmus. Queue for the owner's review instead of silently keeping it.
- EVALUATE — the facts are genuinely insufficient to decide.

Keep each pillar analysis to ONE or TWO sentences. Fill every field."""

SCHEMA = {
    "type": "object",
    "properties": {
        "pillar_3_household":  {"type": "string"},
        "pillar_2_custodian":  {"type": "string"},
        "pillar_1_resonance":  {"type": "string"},
        "pillar_0_ego":        {"type": "string"},
        "bitrate_note":        {"type": "string"},
        "verdict": {"type": "string",
                    "enum": ["HARD_KEEP", "KEEP_WITH_FLAG", "CUT", "STAGNANT", "EVALUATE"]},
    },
    "required": ["pillar_3_household", "pillar_2_custodian", "pillar_1_resonance",
                 "pillar_0_ego", "verdict"],
}

TASTE = ("OWNER TASTE: craves psychological friction, calculating 'polite-monster' "
         "intelligence, taboo-breaking, stylistic/kinetic intensity, dystopian/cerebral "
         "sci-fi; rejects sanitized kitsch, sentimental comfort and 'narrative flaccidity'. "
         "Music: industrial / aggressive / underground.")

CASES = [
    ("Tokyo Story", "expect KEEP (II masterwork; bloated → FLAG)",
     f"""TITLE: Tokyo Story (1953) — movie, Drama
OWNER: not watched.
OTHER HOUSEHOLD USERS: none.
ACCLAIM & METADATA: Rotten Tomatoes 100%, Metacritic 100, 3 award wins; Ozu's magnum opus, voted the greatest film of all time (Sight & Sound 2012). Quiet domestic drama, intergenerational conflict, aging; melancholic, contemplative, static minimalist cinematography.
{TASTE}
TECH: 1080p h264, 23 GB, 173 MB/min — 2.4x class median (bloated)."""),

    ("America's National Parks", "expect CUT (fails litmus: tourism/comfort/competence)",
     f"""TITLE: America's National Parks — show, Documentary
OWNER: not watched.
OTHER HOUSEHOLD USERS: none.
ACCLAIM & METADATA: An epic scenic survey across US national parks (Yellowstone, Yosemite, Grand Canyon, Everglades). Visually competent, optimistic, broad-audience educational nature documentary about ecological interconnectivity; ~6.5/10, no major awards.
{TASTE}
TECH: 1080p h264, 19.7 GB, ~78 MB/min — normal for class."""),

    ("Butterfly Effect", "expect STAGNANT/CUT (Ego-fake: premise claims edge, execution populist)",
     f"""TITLE: The Butterfly Effect (2004) — movie, Sci-Fi / Thriller
OWNER: not watched.
OTHER HOUSEHOLD USERS: none.
ACCLAIM & METADATA: Ashton Kutcher sci-fi thriller; a man travels into his own memories to alter the present (causality / time-loop premise). Reception describes it as a populist, MTV-style teen thriller leaning on shock-value trauma; broad mainstream appeal; 7.5/10 aggregate, no major awards.
{TASTE}
TECH: 1080p h264, 8 GB, ~75 MB/min — normal."""),

    ("A2M", "expect STAGNANT/CUT (crass novelty ≠ intelligent subversion)",
     f"""TITLE: A2M — music, Hip Hop
OWNER: not watched.
OTHER HOUSEHOLD USERS: none.
ACCLAIM & METADATA: Novelty rap act known for the viral explicit track 'I Got Bitches'; crass, sexually explicit frat-boy humour; no critical stature or awards.
{TASTE}
TECH: no technical profile."""),

    ("Partner reality show", "expect KEEP_WITH_FLAG (III Quality Floor: watched-but-mediocre → downscale)",
     f"""TITLE: Flip This House — show, Reality
OWNER: not watched.
OTHER HOUSEHOLD USERS: user partner-account (the owner's partner) watched it to completion, all seasons, and rewatches.
ACCLAIM & METADATA: A formulaic home-renovation competition reality show; light, pleasant, no critical acclaim; ~5.8/10.
{TASTE}
TECH: 1080p h264, 40 GB, ~95 MB/min — high for the class."""),

    ("Generic competent thriller (borderline)", "expect STAGNANT (fine, not exceptional)",
     f"""TITLE: The Accountant (2016) — movie, Action / Thriller
OWNER: not watched.
OTHER HOUSEHOLD USERS: none.
ACCLAIM & METADATA: A competent, watchable mainstream action-thriller with a mild autism-savant hook; solid production and some tension, but conventional and formulaic; 7.3/10, no major awards. Genuinely 'fine' — neither a landmark, nor meditative, nor outright kitsch.
{TASTE}
TECH: 1080p h264, 9 GB, ~78 MB/min — normal."""),

    ("Mr. Robot", "expect HARD_KEEP (Ego edge)",
     f"""TITLE: Mr. Robot (2015) — show, Drama / Thriller
OWNER: watched all 4 seasons, rewatches; a favourite.
OTHER HOUSEHOLD USERS: none.
ACCLAIM & METADATA: Hacker thriller; mental illness, unreliable narrator, anti-capitalist, psychological friction, subversive structure. RT 93%, Emmy + Golden Globe wins.
{TASTE}
TECH: 1080p h265, 45 GB, ~70 MB/min — normal."""),
]


def judge(facts: str) -> dict:
    payload = {
        "model": settings.CURATOR_MODEL,
        "messages": [{"role": "system", "content": CONSTITUTION},
                     {"role": "user", "content": "FACTS:\n" + facts}],
        "format": SCHEMA, "stream": False, "think": False, "keep_alive": "10m",
        "options": {"temperature": 0.0, "num_predict": 800, "num_ctx": 8192, "num_gpu": 99},
    }
    r = httpx.post(f"{settings.effective_ollama}/api/chat", json=payload, timeout=400)
    r.raise_for_status()
    return parse_llm_json((r.json().get("message") or {}).get("content", "") or "")


def main():
    for name, expect, facts in CASES:
        print("=" * 72)
        print(f"### {name}   —   {expect}")
        try:
            v = judge(facts)
        except Exception as e:
            print(f"  FAILED: {e}\n")
            continue
        print(f"  VERDICT: {v.get('verdict')}")
        print(f"    III Household : {v.get('pillar_3_household','')}")
        print(f"    II  Custodian : {v.get('pillar_2_custodian','')}")
        print(f"    I   Resonance : {v.get('pillar_1_resonance','')}")
        print(f"    0   Ego       : {v.get('pillar_0_ego','')}")
        if v.get("bitrate_note"):
            print(f"    Bitrate       : {v.get('bitrate_note')}")
        print()


if __name__ == "__main__":
    main()
