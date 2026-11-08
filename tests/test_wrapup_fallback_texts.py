"""The Wrap-Up's fallback texts (stage 3.7 of docs/UX_RETENTION_DESIGN.md):
a slide the writer could not write says its fact with the headline and
the warm line of THAT fact - none where none fits, never the editor's note
about why it is interesting - all in the story's language, en or de, numbers
the German way; the stand-in story without the curator too. Every title is
invented.

    python tests/test_wrapup_fallback_texts.py
"""
from _harness import check  # noqa: I001 - first: temp stores

import asyncio

from src.services import llm_errors as le
from src.services import wrapup_story as st
from src.services import wrapup_writer as ww

from src.services import wrapup_claims as wc  # noqa: E402
from src.services.wrapup_insights import _MINERS, mine  # noqa: E402

# A month of invented facts that sets off every miner.
F = {
    "period": {"key": "month:2026-10", "label": "October 2026", "end": "2026-11-01T00:00:00"},
    "tempo": {"hours": 41.6, "prev_hours": 80.0, "plays": 60, "active_days": 22, "longest_streak_days": 9,
              "dayparts": {"night": 0.26}, "biggest_day": {"date": "2026-10-14", "hours": 8.4, "plays": 7}},
    "top": [{"title": "Starfall Archive", "media_type": "anime", "hours": 18.2, "plays": 20},
            {"title": "The Lantern Room", "media_type": "movie", "hours": 4.0, "plays": 2}],
    "binge": {"deepest_sitting": {"episodes": 7, "series": "Starfall Archive", "date": "2026-10-14"}},
    "contrarian": {"title": "Gull Point", "you": 9, "community": 6.1, "votes": 2100, "gap": 2.9, "basis": "rated"},
    "dismissal": {"title": "Paper Moon Hotel", "how": "dropped", "community": 8.6},
    "comfort": {"title": "North Signal", "replays": 4},
    "court": {"boldest_keep": {"title": "Shop Window", "score": 92}, "won": 3, "lost": 1},
    "velocity": {"gb_in": 900.0, "gb_out": 120.5, "net_gb": 779.5, "verdict": "hoarder"},
    "time_to_watch": {"longest": {"title": "The Lantern Room", "days": 735}, "median_days": 12, "same_week_share": 0.7},
    "auteurs": {"director": {"name": "Ines Varga", "count": 3, "hours": 7.5, "titles": ["A", "B", "C"]}},
    "drift": {"label": "new direction"},
    "river": [{"month": "2026-10", "similarity": 0.2, "top_titles": ["Starfall Archive"]}],
    "discovery": {"new_share": 0.85},
    "music": {"on_repeat": {"track": "Tidewater", "artist": "The Quiet Pines", "plays": 9, "date": "2026-10-03"},
              "most_skipped": {"track": "Low Bridge", "artist": "Marlow", "skips": 8}},
    "devices": {"night": {"player": "Den TV", "share": 0.51, "hours": 10.0}},
    "eras": {"decades": {"1980s": 0.45, "2020s": 0.3}, "decade_top": {"1980s": ["A"]}},
    "ritual": {"weekday": "Thursday", "daypart": "evening", "title": "Starfall Archive", "media_type": "anime",
               "weeks": 4, "of_weeks": 5, "hours": 6.0},
    "comfort_genre": {"genre": "Fantasy", "plays": 6, "titles": ["Starfall Archive", "North Signal"]},
    "of_the_month": [{"month": "2026-10", "label": "October", "title": "Starfall Archive", "media_type": "anime",
                      "hours": 18.2, "share": 0.44}],
    "night_shift": {"title": "Starfall Archive", "media_type": "anime", "night_plays": 12, "plays": 20, "share": 0.6},
    "first_last": {"first": {"title": "The Lantern Room"}},
}
CARDS = [c for m in _MINERS for c in m(F)]
by_key = {c["key"]: c for c in CARDS}


def slide(key, lang):
    c = dict(by_key[key], id="I01")
    return ww._template("I01", c, None, lang)


check("every card the miner makes names its fact, and every fact is said in both languages",
      all(c.get("key") in wc.TEXTS for c in CARDS) and all(set(t) == {"en", "de"} for t in wc.TEXTS.values()),
      [c.get("key") for c in CARDS if c.get("key") not in wc.TEXTS])
check("the English claim is the miner's own sentence, the one the editor reads",
      all(wc.claim(c, "en") == c["claim"] for c in CARDS))

# what was seen on real data: the line followed the kind of card, not the fact
check("fewer hours than before: a quieter stretch, and no line about the crowd",
      slide("tempo_down", "en") == ("A quieter stretch", "41.6 hours, against 80 in the same span before.")
      and slide("tempo_down", "de") == ("Ruhiger als zuletzt", "41,6 Stunden, gegenüber 80 im gleichen Zeitraum davor."),
      (slide("tempo_down", "en"), slide("tempo_down", "de")))
check("the long wait has its own headline and line, not 'in and out' and 'no judgement'",
      slide("wait_longest", "de") == ("Die lange Wartezeit", "The Lantern Room wartete 735 Tage auf dem Server, "
                                                             "bis du auf Play gedrückt hast. Gutes hält sich."),
      slide("wait_longest", "de"))
check("'it started with' says only that", slide("first_title", "de") == ("Wie es anfing", "Es fing an mit The Lantern Room.")
      and slide("first_title", "en") == ("How it began", "It started with The Lantern Room."))
check("the crowd line where the fact is about the crowd",
      slide("contrarian_rated", "en")[1].endswith("You and the crowd, happily apart."))
check("...and the editor's note about why a fact matters never reaches the slide",
      all(c["why"].lower() not in " ".join(ww._template("I01", dict(c, id="I01"), None, lang)).lower()
          for c in CARDS for lang in ("en", "de")))

# German throughout, numbers the German way
de_all = {c["key"]: ww._template("I01", dict(c, id="I01"), None, "de") for c in CARDS}
check("a German story's fallback is German: claim, headline and line, no English sentence glued on",
      all(c["claim"] not in " ".join(de_all[c["key"]]) for c in CARDS)
      and not any(w in " ".join(de_all[c["key"]]).split() for c in CARDS
                  for w in ("hours", "the", "of", "was", "with", "you")), de_all)
check("German numbers: decimal comma, thousands point, a space before %, dates day first",
      "18,2 Stunden" in de_all["top_title_clear"][1] and "2.100 Stimmen" in de_all["contrarian_rated"][1]
      and "44 %" in de_all["top_title_clear"][1] and de_all["big_day"][1].startswith("14.10.2026:"), de_all)
check("German words for what the facts carry in English",
      de_all["ritual_title"][1].startswith("Donnerstagabende gehörten Starfall Archive")
      and "(abgebrochen)" in de_all["dismissal"][1] and "Der Oktober gehörte" in de_all["of_the_month"][1]
      and "1980ern" in de_all["decade_titled"][1], de_all)
check("a warm line never repeats its headline",
      all(not h or h.lower() not in t.lower().split(". ", 1)[-1] for h, t in de_all.values()))

pick = {"title": "Starfall Archive", "reason": "In your favourite genre."}
twist = dict(by_key["contrarian_rated"], id="I01")
check("the roast is said with love, the toast raises a glass, in the story's language",
      ww._template("ROAST", twist, None, "en") == ("", twist["claim"] + " Said with love.")
      and ww._template("TOAST", twist, None, "de")[1].endswith("(2.100 Stimmen). Auf mehr davon."))
check("the next pick is an offer, not a reminder of what was not played",
      ww._template("NEXT", None, pick, "en") == ("", "Next, if you feel like it: Starfall Archive. In your favourite genre.")
      and ww._template("NEXT", None, pick, "de") == ("", "Als Nächstes, wenn du magst: Starfall Archive."))
check("no pick, no next line", ww._template("NEXT", None, None, "de") == ("", ""))
old = {"id": "I01", "kind": "contrast", "slide": "twist", "score": 0.7, "claim": "An older card.", "why": "w"}
check("a card from before the keys keeps its claim under its beat's headline",
      ww._template("I01", old, None, "de") == ("Die Wendung", "An older card."))
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
      and de["era_chapters"][0]["commentary"] == "Starfall Archive: 6 Stunden in 6 Wiedergaben."
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


cards = mine(F, limit=3)
idx = {"items": IDX["items"], "picks": IDX["picks"], "traits": ["night_owl"], "clusters": {}}
_real = ww._ask
ww._ask = _ask
try:
    story, source, _ = asyncio.run(ww.write(F, "", idx, cards, lang="de"))
finally:
    ww._ask = _real
want = {c["id"]: ww._template(c["id"], c, None, "de") for c in cards}
check("a lost writer in a German story: every slide its fact's German headline and text, the German archetype",
      source == "fallback" and story["archetype_title"] == "Die Nachtschicht"
      and all((s["headline"], s["text"]) == want[s["id"]] for s in story["slides"])
      and len(story["slides"]) == 3, (source, story))

check.done()
