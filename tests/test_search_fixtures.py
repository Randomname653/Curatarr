"""Search ground-truth fixtures — the owner's two live review rounds as
ordering assertions against the DETERMINISTIC evidence scorer, using the
REAL tag arrays from the enrichment cache and REAL nomic embeddings.

    python tests/test_search_fixtures.py

Needs Ollama (CPU embeds only — no Chroma, the app may keep running).
Prints the full score/evidence matrix for threshold calibration.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.services.semantic_search as ss

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# Verbatim tag arrays (v2:raw:anime:* enrichment cache + chroma themes),
# captured 2026-08-16. These are the exact facts the live search sees.
FIXTURES = {
    "Gleipnir": ["Shapeshifting", "Death Game", "Urban Fantasy",
                 "Primarily Teen Cast", "Gore", "Body Horror", "Vore",
                 "Heterosexual", "Nudity", "Yandere", "Male Protagonist",
                 "Rural", "Seinen", "Female Protagonist", "Femdom"],
    "MAGICAL GIRL SITE": ["Bullying and revenge",
                          "Digital portal to supernatural powers",
                          "Body horror and gore", "Urban fantasy setting",
                          "Ensemble cast of magical girls", "Time manipulation"],
    "Puella Magi Madoka Magica": [
        "magical girl contract with a predatory entity",
        "time loop to prevent tragedy", "self-sacrifice as price of wish",
        "dark reinterpretation of innocence",
        "psychological unraveling of protagonist", "urban dystopia"],
    "Magical Destroyers": ["Otaku Culture", "Henshin", "Dystopian", "Urban",
                           "Magic", "Surreal Comedy", "War", "Heterosexual",
                           "Male Protagonist", "Female Protagonist",
                           "Slapstick", "Primarily Female Cast", "Parody",
                           "Urban Fantasy", "Video Games"],
    "Diabolik Lovers": ["sacrificial bride", "vampire harem",
                        "psychological captivity", "rape as narrative device",
                        "yandere antagonist", "gore and violence"],
    # raw AniList tags + the chroma themes phrases (the live search unions
    # both sources — the themes carry the tonal register the tags lack).
    "RIN Daughters of Mnemosyne": ["Female Protagonist", "Primarily Adult Cast",
                                   "Urban", "Guns", "Urban Fantasy", "Gore",
                                   "LGBTQ+ Themes", "Anti-Hero", "Nudity",
                                   "Bisexual", "Detective", "Episodic",
                                   "Large Breasts", "Virtual World",
                                   "Immortal detective", "Urban fantasy noir",
                                   "Gore-heavy action",
                                   "Adult ecchi with explicit nudity",
                                   "LGBTQ+ romance"],
    "Speed Grapher": ["Cult", "Super Power", "Photography", "Politics",
                      "Fugitive", "Economics", "Urban Fantasy", "Crime",
                      "Female Protagonist", "Terrorism", "Drugs",
                      "Conspiracy", "Anti-Hero", "Primarily Adult Cast",
                      "Noir", "ritualistic female antagonist",
                      "corporate espionage in Tokyo's red-light district"],
    "Magical Girl Lyrical Nanoha": ["Female Protagonist", "Urban Fantasy",
                                    "Magic", "Super Power", "Henshin",
                                    "Primarily Female Cast",
                                    "Primarily Child Cast"],
    "Jungle De Ikou": ["magical girl transformation (henshin)",
                       "fanservice-driven comedy",
                       "child protagonist with supernatural powers"],
    "Fabiniku": ["gender-bending reincarnation", "BL romance",
                 "slapstick humor", "salaryman to adventurer",
                 "Primarily Adult Cast"],
}

TITLES = list(FIXTURES.keys())


def _hits_for(retrieval=0.6):
    return [{"title": t, "score": retrieval} for t in TITLES]


async def score(constraints):
    cons_core = [ss._split_negation(c)[0] for c in constraints]
    all_tags = [t for tags in FIXTURES.values() for t in tags]
    await ss._texts_to_vectors(cons_core + all_tags)
    scored = ss._evidence_scores(constraints, list(FIXTURES.values()),
                                 _hits_for())
    return {t: s for t, s in zip(TITLES, scored)}


def show(header, scores):
    print(f"\n=== {header} ===")
    for t, (fit, note) in sorted(scores.items(), key=lambda x: -x[1][0]):
        print(f"  {fit:>2}  {t[:32]:<34} {note[:110]}")


async def main():
    # ── Round-2 query: tonal ─────────────────────────────────────────────────
    q2 = ["darker", "subversive fetish dynamics", "mature tone"]
    s2 = await score(q2)
    show("darker · subversive fetish dynamics · mature tone", s2)

    check("Gleipnir beats MAGICAL GIRL SITE (femdom tag vs gore-only)",
          s2["Gleipnir"][0] > s2["MAGICAL GIRL SITE"][0])
    check("Gleipnir fetish evidence cites a fetish-family tag",
          any(m in s2["Gleipnir"][1] for m in ("Femdom", "Vore", "Yandere")))
    check("SITE capped <=5 (no fetish tag)",
          s2["MAGICAL GIRL SITE"][0] <= 5)
    check("Madoka capped <=5 (no fetish tag — the hallucination case)",
          s2["Puella Magi Madoka Magica"][0] <= 5)
    check("Madoka note names the unevidenced constraint",
          "unbelegt" in s2["Puella Magi Madoka Magica"][1])
    check("Diabolik does not outrank Gleipnir (documented borderline)",
          s2["Diabolik Lovers"][0] <= s2["Gleipnir"][0])
    check("Mnemosyne outranks the child-cast fillers on the tonal query",
          s2["RIN Daughters of Mnemosyne"][0]
          > max(s2["Jungle De Ikou"][0],
                s2["Magical Girl Lyrical Nanoha"][0]))

    # ── Round-1 query: literal cast constraint ───────────────────────────────
    q1 = ["adult cast"]
    s1 = await score(q1)
    show("adult cast", s1)

    adult = ["RIN Daughters of Mnemosyne", "Speed Grapher", "Fabiniku"]
    child = ["Magical Girl Lyrical Nanoha", "Jungle De Ikou"]
    check("every Primarily-Adult-Cast title beats every child-cast title",
          min(s1[t][0] for t in adult) > max(s1[t][0] for t in child))
    check("child-cast titles capped <=5",
          all(s1[t][0] <= 5 for t in child))
    check("adult evidence cites the literal tag",
          "Primarily Adult Cast" in s1["Speed Grapher"][1])

    # ── negation: 'no gore' ─────────────────────────────────────────────────
    qn = ["no gore"]
    sn = await score(qn)
    show("no gore (negation)", sn)
    check("gore-tagged title capped at 2 under 'no gore'",
          sn["Gleipnir"][0] <= 2 and "violates" in sn["Gleipnir"][1])
    check("gore-free title passes the negation",
          sn["Magical Destroyers"][0] > 2)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
