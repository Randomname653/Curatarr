"""A Wrap-Up story told in German is German all through (owner's read with
``wrapup_read --write`` on a real copy): the writer took English pieces of
its brief into the German text ("11.9 hours", "8 episodes", "735 days",
"2026-10-20") and the archetype and share line came out English. The
editor and the writer now get the cards in the story's language - claims
and renderings, German numbers - no English background sheet, and the
editor writes the archetype, subhead and share line in that language.
Every title is invented.

    python tests/test_wrapup_story_language.py
"""
from _harness import check  # noqa: I001 - first: temp stores

import asyncio
import re

from src.services import llm_errors as le
from src.services import wrapup_writer as ww
from src.services.wrapup_insights import mine

F = {
    "period": {"key": "month:2026-10", "label": "October 2026", "end": "2026-11-01T00:00:00"},
    "tempo": {"hours": 41.6, "prev_hours": 20.0, "plays": 60, "active_days": 22, "longest_streak_days": 9,
              "dayparts": {"night": 0.26}, "biggest_day": {"date": "2026-10-20", "hours": 11.9, "plays": 8}},
    "top": [{"title": "Starfall Archive", "media_type": "anime", "hours": 18.2, "plays": 20},
            {"title": "The Lantern Room", "media_type": "movie", "hours": 4.0, "plays": 2}],
    "binge": {"deepest_sitting": {"episodes": 8, "series": "Starfall Archive", "date": "2026-10-20"}},
    "contrarian": {"title": "Gull Point", "you": 9, "community": 6.1, "votes": 2100, "gap": 2.9, "basis": "rated"},
    "time_to_watch": {"longest": {"title": "The Lantern Room", "days": 735}, "median_days": 12},
    "ritual": {"weekday": "Thursday", "daypart": "evening", "title": "Starfall Archive", "media_type": "anime",
               "weeks": 4, "of_weeks": 5, "hours": 6.0},
}
# English units, ISO dates, an English decimal point ("11.9"; German "2.100" and "20.10.2026" are fine)
ENGLISH = re.compile(r"\b\d[\d.,]*\s+(?:hours?|days?|episodes?|plays?|votes?|weeks?|times|points)\b"
                     r"|\b\d{4}-\d{2}-\d{2}\b|(?<![\d.])\d+\.\d(?![\d.])")

en, de = mine(F), mine(F, lang="de")
check("the same cards in both languages: the same facts, the same order and ids",
      [(c["id"], c["key"]) for c in en] == [(c["id"], c["key"]) for c in de] and len(de) >= 5)
check("English cards read exactly as the editor always read them",
      any(c["claim"] == "Starfall Archive took 18.2 hours, 44% of the time." for c in en)
      and any("11.9 hours" in r for c in en for v in c["say"].values() for r in v))
de_text = " ".join([c["claim"] for c in de] + [r for c in de for v in c["say"].values() for r in v])
check("German cards: claims and renderings with German units, numbers and dates",
      not ENGLISH.search(de_text) and "11,9 Stunden" in de_text and "735 Tage" in de_text
      and "8 Folgen" in de_text and "20.10.2026" in de_text and "2.100 Stimmen" in de_text,
      ENGLISH.findall(de_text) or de_text[:600])

calls = []


def answers(*replies):
    queue = list(replies)

    async def ask(messages, *, fmt=None, temperature, predict):
        calls.append({"messages": messages, "fmt": fmt})
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    return ask


by_key = {c["key"]: c for c in de}
top, wait, binge = by_key["top_title_clear"], by_key["wait_longest"], by_key["binge"]
plan = ('{"arc": ["%s", "%s", "%s"], "roast": "%s", "toast": "%s", "archetype_trait": "night_owl", '
        '"archetype_title": "Die Nachtschicht", "archetype_subhead": "Das meiste lief spät.", '
        '"share_line": "Ein Oktober mit Starfall Archive 🐎", "next_pick": "", "cluster_names": []}'
        % (top["id"], binge["id"], wait["id"], wait["id"], top["id"]))
german = "\n".join([
    f"## {top['id']} | Ein Monat, ein Titel",
    f"Starfall Archive: 18,2 Stunden, 44 % der Zeit [{top['id']}].",
    f"## {binge['id']} | Noch eine Folge",
    f"Acht Folgen Starfall Archive am Stück, am 20.10.2026 [{binge['id']}].",
    f"## {wait['id']} | Die lange Wartezeit",
    f"The Lantern Room wartete 735 Tage, bevor du auf Play gedrückt hast [{wait['id']}].",
    "## ROAST", f"Über zwei Jahre im Regal [{wait['id']}].",
    "## TOAST", f"Fast 20 Stunden für eine Serie [{top['id']}]."])
idx = {"items": {"T1": {"title": "Starfall Archive"}, "T2": {"title": "The Lantern Room"}}, "picks": {},
       "traits": ["night_owl"], "clusters": {}}
SHEET = "F1 Biggest day 2026-10-20: 11.9 hours."
_real = ww._ask
ww._ask = answers(plan, german)
try:
    story, source, _ = asyncio.run(ww.write(F, SHEET, idx, de, lang="de"))
finally:
    ww._ask = _real
editor_sys, editor_msg = calls[0]["messages"][0]["content"], calls[0]["messages"][1]["content"]
brief = calls[1]["messages"][1]["content"]
check("the editor is told to write the archetype, subhead and share line in German",
      "in German, the" in editor_sys and "{language}" not in editor_sys, editor_sys[-400:])
check("a German editor names a way of watching, not a person: no gendered noun, no pronoun in the line",
      "no gendered person noun" in editor_sys
      and "archetype_subhead uses no personal pronoun (er, sie)" in editor_sys, editor_sys[-500:])
check("no emoji in the share line, archetype or its line", story["share_line"] == "Ein Oktober mit Starfall Archive",
      story["share_line"])
check("the editor's cards and the writer's brief carry no English unit, decimal point or ISO date",
      not ENGLISH.search(editor_msg) and not ENGLISH.search(brief) and "Stunden" in brief,
      ENGLISH.findall(editor_msg + brief))
from src.services import wrapup_claims as wc  # noqa: E402
check("every fact has its German note on why it matters, and the editor reads that one",
      set(wc.TEXTS) == set(wc.WHY_DE) and "a usual day" not in editor_msg
      and "so viel wie an einem gewöhnlichen Tag" in editor_msg, editor_msg[:500])
check("...and no English background sheet for a German story", "BACKGROUND FACTS" not in brief
      and SHEET not in brief)
check("a German text with German numbers passes the number check as written",
      source == "llm" and [s["text"] for s in story["slides"]] == [
          "Starfall Archive: 18,2 Stunden, 44 % der Zeit.",
          "Acht Folgen Starfall Archive am Stück, am 20.10.2026.",
          "The Lantern Room wartete 735 Tage, bevor du auf Play gedrückt hast."], (source, story["slide_sources"]))

# the fixture the owner asked for: a German story - here the one written without the writer
calls.clear()
ww._ask = answers(plan, le.LLMCallError(le.LLMFailure.INFRA_OFFLINE, "gone"))
try:
    fb, fsource, _ = asyncio.run(ww.write(F, SHEET, idx, de, lang="de"))
finally:
    ww._ask = _real
text = " ".join([s["headline"] + " " + s["text"] for s in fb["slides"]]
                + [fb["archetype_title"], fb["archetype_subhead"], fb["share_line"],
                   fb["roast_and_toast"]["roast"], fb["roast_and_toast"]["toast"]])
check("a German story: no English units or ISO dates anywhere in its text",
      fsource == "fallback" and not ENGLISH.search(text) and "735 Tage" in text, ENGLISH.findall(text) or text)

# ── the roast teases a choice: never the amount watched ─────────────────────
cards_en = mine(dict(F, tempo=dict(F["tempo"], prev_hours=80.0)))          # fewer hours than before
drop = next(c for c in cards_en if c["key"] == "tempo_down")
other = [c for c in cards_en if c is not drop]
roast_plan = ('{"arc": ["%s", "%s", "%s"], "roast": "%s", "toast": "%s", "archetype_trait": "night_owl", '
              '"archetype_title": "The Late Archive", "archetype_subhead": "Most of it after dark.", '
              '"share_line": "An October of late nights", "next_pick": "", "cluster_names": []}'
              % (other[0]["id"], other[1]["id"], other[2]["id"], drop["id"], other[0]["id"]))
calls.clear()
ww._ask = answers(roast_plan, le.LLMCallError(le.LLMFailure.INFRA_OFFLINE, "gone"))
try:
    rs, _, _ = asyncio.run(ww.write(F, "", idx, cards_en, lang="en"))
finally:
    ww._ask = _real
check("an English editor gets no German note", "gendered" not in calls[0]["messages"][0]["content"])
check("a drop in hours is no choice to roast: the editor is told so, and its pick is replaced",
      "never one about\n  how much they watched" in calls[0]["messages"][0]["content"]
      and drop["claim"] not in rs["roast_and_toast"]["roast"], (rs["roast_and_toast"], drop["claim"]))
check("...the replacement is a roastable card, and the toast is untouched",
      rs["roast_and_toast"]["toast"].startswith(other[0]["claim"]), rs["roast_and_toast"])

check.done()
