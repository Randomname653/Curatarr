"""The Wrap-Up's fallback texts (stage 3.7 of docs/UX_RETENTION_DESIGN.md):
a slide the writer could not write says its fact and a warm line for its
kind of card - not the editor's note about why the fact is interesting -
in the story's language, en or de; the stand-in story without the curator
too. Every title is invented.

    python tests/test_wrapup_fallback_texts.py
"""
from _harness import check  # noqa: I001 - first: temp stores

import asyncio

from src.services import llm_errors as le
from src.services import wrapup_story as st
from src.services import wrapup_writer as ww

WHY = "the widest gap between you and everyone else this period"


def card(cid, kind, slide, claim, refs=()):
    return {"id": cid, "kind": kind, "slide": slide, "score": 0.7, "claim": claim, "why": WHY,
            "values": {}, "say": {}, "refs": list(refs), "media_type": None}


twist = card("I01", "contrast", "twist", "You rated The Lantern Room 9; the crowd gives it 6.1.", ["The Lantern Room"])
head, text = ww._template("I01", twist, None, "en")
check("a slide's fallback: its fact and a warm line for its kind, not the editor's note",
      (head, text) == ("The twist", "You rated The Lantern Room 9; the crowd gives it 6.1. "
                                    "You and the crowd, happily apart."), (head, text))
check("...the note about why it is interesting stays with the editor", WHY.lower() not in text.lower())
head_de, text_de = ww._template("I01", twist, None, "de")
check("in German for a story told in German: the headline and the warm line",
      head_de == "Die Wendung" and text_de.endswith("Du und die anderen, angenehm verschieden."), (head_de, text_de))
check("the roast is said with love, the toast raises a glass",
      ww._template("ROAST", twist, None, "en") == ("", twist["claim"] + " Said with love.")
      and ww._template("TOAST", twist, None, "de") == ("", twist["claim"] + " Auf mehr davon."))
pick = {"title": "Starfall Archive", "reason": "In your favourite genre."}
check("the next pick is an offer, not a reminder of what was not played",
      ww._template("NEXT", None, pick, "en") == ("", "Next, if you feel like it: Starfall Archive. In your favourite genre.")
      and ww._template("NEXT", None, pick, "de") == ("", "Als Nächstes, wenn du magst: Starfall Archive."))
check("no pick, no next line", ww._template("NEXT", None, None, "de") == ("", ""))

KINDS = ("superlative", "streak", "rhythm", "moment", "identity", "court", "contrast", "confession", "balance")
check("every kind of card has its warm line in both languages",
      all(ww._template("I02", card("I02", k, "moment", "A fact."), None, lang)[1] != "A fact."
          for k in KINDS for lang in ("en", "de")))
check("a warm line never repeats its slide's headline",
      all(ww.beat_headline(b, lang).rstrip(".").lower() not in ww._template("I02", card("I02", k, b, "A fact."), None, lang)[1].lower()
          for k in KINDS for b in ww.BEAT_HEADLINES for lang in ("en", "de")))
check("every beat has a headline in both languages",
      all(ww.beat_headline(b, "en") and ww.beat_headline(b, "de") and ww.beat_headline(b, "en") != ww.beat_headline(b, "de")
          for b in ww.BEAT_HEADLINES), [(b, ww.beat_headline(b, "de")) for b in ww.BEAT_HEADLINES])
check("an unknown language falls back to English",
      ww._template("I01", twist, None, "fr") == ww._template("I01", twist, None, "en"))

# ── the archetype and the stand-in story ────────────────────────────────────

check("every archetype has its German name, and none names a person",
      set(st.TRAIT_TITLES) == set(st.TRAIT_TITLES_DE)
      and all(st.trait_title(k, "de") == st.TRAIT_TITLES_DE[k] for k in st.TRAIT_TITLES))
check("no trait: a plain review title, warm in both languages",
      st.trait_title(None, "en") == ("Your Period in Review", "What you played, told plainly.")
      and st.trait_title(None, "de") == ("Dein Rückblick", "Was lief, schlicht erzählt."))

FACTS = {"traits": [{"key": "night_owl"}],
         "top": [{"title": "Starfall Archive", "hours": 6.0, "plays": 6},
                 {"title": "The Lantern Room", "hours": 2.0, "plays": 1}]}
IDX = {"items": {"T1": {"title": "Starfall Archive"}, "T2": {"title": "The Lantern Room"}},
       "picks": {"U1": {"title": "Gull Point"}}}
de = st.fallback_story(FACTS, IDX, "de")
check("the stand-in without the curator speaks German for a German story",
      de["archetype_title"] == "Die Nachtschicht" and de["era_chapters"][0]["title"] == "Wohin die Stunden gingen"
      and de["era_chapters"][0]["commentary"] == "Starfall Archive: 6.0 Stunden in 6 Wiedergaben."
      and de["look_ahead_hook"]["challenge"] == "Als Nächstes, wenn du magst: Gull Point.", de)
en = st.fallback_story(FACTS, IDX)
check("...and English by default, the next pick an offer",
      en["archetype_title"] == "The Midnight Shift"
      and en["look_ahead_hook"]["challenge"] == "Next, if you feel like it: Gull Point.", en)

# ── write(): a writer lost, the story still told in its language ────────────

calls = []


async def _ask(messages, *, fmt=None, temperature, predict):
    calls.append(fmt is not None)
    if fmt is not None:                      # the editor answers nonsense: the rule-made plan
        return "no plan today"
    raise le.LLMCallError(le.LLMFailure.INFRA_OFFLINE, "gone")


cards = [twist, card("I02", "superlative", "reveal", "Starfall Archive took 6 hours.", ["Starfall Archive"]),
         card("I03", "confession", "balance", "Twelve first episodes, two second ones.")]
idx = {"items": IDX["items"], "picks": IDX["picks"], "traits": ["night_owl"], "clusters": {}}
_real = ww._ask
ww._ask = _ask
try:
    story, source, _ = asyncio.run(ww.write(FACTS, "", idx, cards, lang="de"))
finally:
    ww._ask = _real
heads = {s["id"]: s["headline"] for s in story["slides"]}
check("a lost writer in a German story: German headlines, the warm German line, the German archetype",
      source == "fallback" and heads == {"I01": "Die Wendung", "I02": "Wohin die Stunden gingen",
                                         "I03": "Rein und raus"}
      and story["slides"][0]["text"].endswith("Nichts anderes kam heran.")
      and story["archetype_title"] == "Die Nachtschicht", (source, story))

check.done()
