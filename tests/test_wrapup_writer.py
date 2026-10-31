"""Wrap-up v2, step 2: an editor picks the story, a writer tells it.

v1 asked for the whole story as one JSON form and checked it with rules that
were strict on style and loose on truth: an honest rounding, a title with an
"!" or a band called Journey sent everything back; one repair named only the
first fault; a second failure threw every good field away. A wrong number
that happened to appear elsewhere on the sheet passed, and so did a title
nobody had cited. v2 checks each sentence against the cards it cites, fixes
style instead of rejecting it, and repairs slide by slide.

    python tests/test_wrapup_writer.py
"""
import asyncio
import json
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy.orm import sessionmaker  # noqa: E402

import src.services.llm_errors as le  # noqa: E402
import src.services.llm_priority as lp  # noqa: E402
import src.services.ollama_hosts as oh  # noqa: E402
import src.services.wrapup_insights as wi  # noqa: E402
import src.services.wrapup_writer as ww  # noqa: E402
from src.database.models import Base, ConversationMessage, DeletionProposal  # noqa: E402
from src.services.wrapup_story import render_fact_sheet  # noqa: E402

from _checks import check  # noqa: E402


# ── style: fixed, never fatal ───────────────────────────────────────────────
names = ["Mamma Mia!", "Journey", "Haikyu!!"]
check("a title's own '!' survives, a sentence's becomes a full stop",
      ww.fix_style("You defended Mamma Mia! again!", names) == "You defended Mamma Mia! again.")
check("a band called Journey is a name, not a cliché",
      ww.fix_style("Journey got 40 hours on a long journey", names) == "Journey got 40 hours on a long run")
check("stock words are cut or swapped, never a rejection",
      ww.fix_style("It's clear that this was truly a tapestry", []) == "This was a mix")

# ── checks per sentence ─────────────────────────────────────────────────────
FACTS = {
    "scope": "all", "eligible": True, "facts_hash": "h", "period": {"label": "2026"},
    "tempo": {"hours": 58.3, "prev_hours": 30.0, "plays": 90, "active_days": 40, "peak_hour": 22,
              "longest_streak_days": 9, "dayparts": {"night": 0.34}, "weekend_share": 0.3,
              "biggest_day": {"date": "2026-03-14", "hours": 8.0, "plays": 9}},
    "top": [{"title": "Severance", "media_type": "show", "hours": 20.0, "plays": 18},
            {"title": "Blade Runner 2049", "media_type": "movie", "hours": 5.6, "plays": 2}],
    "contrarian": {"title": "The Bear", "media_type": "show", "you": 9.0, "community": 6.4,
                   "votes": 2100, "gap": 2.6, "basis": "rated"},
    "court": {"decided": 9, "kept": 5, "deleted": 4, "overrides": 2, "debated": 6, "won": 4, "lost": 1,
              "gb_saved": 1.0, "gb_freed": 3.0, "defiant": 2,
              "boldest_keep": {"title": "Mamma Mia!", "media_type": "movie", "score": 94, "verdict": "CUT"}},
    "look_ahead": [{"title": "Stalker", "media_type": "movie", "year": 1979, "reason": "patient and strange"}],
    "traits": [{"key": "contrarian", "strength": 0.5, "evidence": "9 vs 6.4"}],
}
sheet, idx = render_fact_sheet(FACTS)
CARDS = wi.mine(FACTS)
C = {c["refs"][0] if c["refs"] else c["slide"]: c for c in CARDS}
top, bear, court, tempo = C["Severance"], C["The Bear"], C["Mamma Mia!"], C["open"]
ev = ww.evidence(CARDS, idx["picks"])
known = [v["title"] for v in idx["items"].values()]


def faults(text, own=None, head=""):
    return ww.check_section(text, head, own or [], ev, known)


check("an honest rounding of the cited card's number passes",
      faults(f"Nearly 60 hours in all [{tempo['id']}].", [tempo["id"]]) == [])
f = faults(f"Severance took 30 hours [{top['id']}].", [top["id"]])
check("a number that is not on the cited card fails — even if it is elsewhere on the sheet",
      f and "30" in f[0] and "30" in ww.numerals(sheet))
check("an uncited sentence rests on the next cited one",
      faults(f"The judge was 94% sure. Mamma Mia! stayed [{court['id']}].", []) == [])
check("a sentence without a citation fails", faults("Severance took 20 hours.")[0].startswith("no citation"))
check("citing a card that does not exist fails", "not a card" in faults("Twenty hours [I99].")[0])
f = faults(f"You defended Mamma Mia! against everyone [{top['id']}].", [top["id"]])
check("naming a title the cited cards do not have fails", f and "Mamma Mia!" in f[0])
check("...and passes when the sentence cites the card that has it",
      faults(f"You defended Mamma Mia! against a 94% judge [{court['id']}].", [court["id"]]) == [])
c2049 = next(c for c in CARDS if "Blade Runner 2049" in c["refs"]) if any(
    "Blade Runner 2049" in c["refs"] for c in CARDS) else None
check("a numeral inside a cited title is the title's, not an invented number",
      c2049 is None or faults(f"Blade Runner 2049, twice [{c2049['id']}].", [c2049["id"]]) == [])

# ── spelled-out numbers are numbers ─────────────────────────────────────────
# A check that read only digits let "ninety hours" through. Each row: the
# sentence (citing the card in braces) and the number the fault must name, or
# None when it must pass.
I03, T0 = top["id"], tempo["id"]
SPELLED = [
    (f"Severance took 95 hours [{I03}].", "95"),
    (f"Severance took ninety hours [{I03}].", "90"),
    (f"Severance took ninety-five hours [{I03}].", "95"),
    (f"Severance took Ninety Five hours [{I03}].", "95"),
    (f"Severance took twenty-one hours [{I03}].", "21"),
    (f"Severance took a hundred hours [{I03}].", "100"),
    (f"Severance took two thousand hours [{I03}].", "2000"),
    (f"Severance took two dozen hours [{I03}].", "24"),
    (f"Severance took over a hundred hours [{I03}].", "over 100"),
    (f"Severance took hundreds of hours [{I03}].", "hundreds of"),
    (f"Severance took over 50 hours [{I03}].", "over 50"),
    (f"Severance took twenty hours [{I03}].", None),
    (f"Nearly sixty hours in all [{T0}].", None),
    (f"Over 50 hours in all [{T0}].", None),
    (f"Over fifty hours, dozens of them at night [{T0}].", None),
]
for sent, want in SPELLED:
    f = faults(sent, [I03])
    check(f"spelled numbers: {sent!r} → {want or 'passes'}",
          f == [] if want is None else (len(f) == 1 and want in f[0]))
check("'twenty' passes because it is the card's 20, not because it is invisible",
      ww.numerals("twenty hours") == {"20"} and faults(f"Severance took twenty hours [{I03}].", [I03]) == []
      and faults(f"Severance took twenty-two hours [{I03}].", [I03]) != [])
check("number words in a headline are checked too",
      faults(f"It happened [{I03}].", [I03], head="Ninety reasons") != []
      and faults(f"It happened [{I03}].", [I03], head="Twenty hours") == [])
check("multipliers and dozens read as numbers",
      ww.numerals("twice, tenfold, a dozen, half a dozen, two and a half days")
      == {"2", "10", "12", "6", "2.5"})

# A number word inside a cited title is the title's.
TITLED = [
    {"id": "X1", "claim": "Seven took 4 hours.", "values": {"hours": 4.0}, "say": {"hours": ["4 hours"]},
     "refs": ["Seven"]},
    {"id": "X2", "claim": "Thirteen Lives took 3 hours.", "values": {"hours": 3.0},
     "say": {"hours": ["3 hours", "three hours"]}, "refs": ["Thirteen Lives"]},
]
ev_t = ww.evidence(TITLED, {})


def tfaults(text, own):
    return ww.check_section(text, "", own, ev_t, ["Seven", "Thirteen Lives"])


f = tfaults("Seven took ninety-five hours [X1].", ["X1"])
check("a film called Seven: the invented ninety-five fails, the title does not", len(f) == 1 and "95" in f[0])
f = tfaults("Thirteen Lives took ninety hours [X2].", ["X2"])
check("a film called Thirteen Lives: only the invented ninety is named", len(f) == 1 and "90" in f[0])
check("...its title's number is not a licence to claim it: 'thirteen hours' fails, 'three hours' passes",
      any("13" in x for x in tfaults("Thirteen Lives took thirteen hours [X2].", ["X2"]))
      and tfaults("Thirteen Lives took three hours [X2].", ["X2"]) == [])
f = tfaults("Thirteen Lives took dozens of hours [X2].", ["X2"])
check("'dozens of' needs a value of at least 24 on the cited card", len(f) == 1 and "dozens of" in f[0])
check("the headline's numbers must be its own card's",
      faults(f"It happened [{top['id']}].", [top["id"]], head="77 reasons") != [])
check("a pick's year is on the pick", faults("Stalker, from 1979, is waiting [U1].", ["U1"]) == [])
check("a citation after the full stop belongs to the sentence before it",
      ww.tuck_cites("Twenty hours. [I01] Then more [I02].") == "Twenty hours [I01]. Then more [I02].")
check("the page shows the text without its citations",
      ww.strip_cites("Twenty hours [I01, U1]. Then more [I02].") == "Twenty hours. Then more.")
check("sections are read by their headers",
      ww.parse_sections("## I01 | Head one\nText one [I01].\n## ROAST\nRoast [I02].")
      == {"I01": ("Head one", "Text one [I01]."), "ROAST": ("", "Roast [I02].")})

# ── the pipeline, against a fake curator ────────────────────────────────────
calls = []


async def fake_bg(owner, call, **kw):
    return await call()


def answers(*a):
    q = list(a)

    async def post(url, payload, *, read_timeout):
        calls.append(payload)
        x = q.pop(0)
        if isinstance(x, Exception):
            raise x
        return {"message": {"content": x}}
    return post


lp.background_curator_call = fake_bg
oh.host_for = lambda role: type("H", (), {"url": "http://x", "profile": None, "num_ctx": 16384, "gpu": {}})()
ARC = [top["id"], bear["id"], court["id"]]


def edit(**over):
    e = {"arc": ARC, "roast": bear["id"], "toast": top["id"], "archetype_trait": "contrarian",
         "archetype_title": "The Loyal Contrarian", "archetype_subhead": "Nine for The Bear.",
         "share_line": "A year of rituals", "next_pick": "U1", "cluster_names": []}
    e.update(over)
    return json.dumps(e)


def prose(bad_court=False, bad_bear=False):
    return "\n".join([
        f"## {top['id']} | Twenty hours of office dread",
        f"Severance took 20 hours, {round(top['values']['share'] * 100)}% of the period [{top['id']}].",
        f"## {bear['id']} | The Bear, your way",
        (f"You gave The Bear 77 stars [{bear['id']}]." if bad_bear
         else f"You gave The Bear a 9; the room gives it 6.4 [{bear['id']}]."),
        f"## {court['id']} | Overruled",
        (f"Mamma Mia! survived 15 judges [{court['id']}]." if bad_court
         else f"The judge was 94% sure about Mamma Mia!. It stayed [{court['id']}]."),
        "## ROAST", f"A 9 for a 6.4 [{bear['id']}].",
        "## TOAST", f"When something lands, it gets 20 hours [{top['id']}].",
        "## NEXT", "Stalker, from 1979, has been waiting [U1]."])


def run(*model_answers, quotes=(), lang="en"):
    calls.clear()
    le.post_chat = answers(*model_answers)
    return asyncio.run(ww.write(FACTS, sheet, idx, CARDS, quotes=list(quotes), lang=lang))


story, source, _ = run(edit(), prose())
check("a clean answer is the curator's story, start to finish", source == "llm" and len(calls) == 2)
check("the editor's choice is forced JSON; the writer writes free prose",
      calls[0].get("format") and not calls[1].get("format"))
check("slides follow the editor's arc, without their citations",
      [s["id"] for s in story["slides"]] == ARC and "[" not in story["slides"][0]["text"]
      and story["slides"][0]["text"].startswith("Severance took 20 hours"))
check("the v1 page still gets its chapters, roast and toast, and the hook",
      len(story["era_chapters"]) == 3 and story["roast_and_toast"]["roast"] == "A 9 for a 6.4."
      and story["look_ahead_hook"]["item_ref"] == "U1")
check("a slide's titles become the page's poster references",
      story["slides"][0]["item_refs"] == [k for k, v in idx["items"].items() if v["title"] == "Severance"])

fixed_court = f"## {court['id']} | Overruled\nThe judge was 94% sure about Mamma Mia!. It stayed [{court['id']}]."
story, source, _ = run(edit(), prose(bad_court=True, bad_bear=True),
                       fixed_court + f"\n## {bear['id']} | Still wrong\nThe Bear got 88 [{bear['id']}].")
ask = calls[2]["messages"][-1]["content"]
check("only the failing slides go back, every fault named",
      f"## {court['id']}" in ask and f"## {bear['id']}" in ask and f"## {top['id']}:" not in ask
      and "15" in ask and "77" in ask)
check("a repaired slide is kept, the good ones stay as written",
      story["slide_sources"][court["id"]] == "repaired" and story["slide_sources"][top["id"]] == "llm")
check("a slide that fails twice gets its card's template — only that slide",
      story["slide_sources"][bear["id"]] == "template"
      and story["slides"][1]["text"].startswith(bear["claim"]) and source == "llm_repaired")

story, source, _ = run("not json at all", prose(), "")
check("an unusable editor answer: the rule-made plan, the writer still writes, gaps from templates",
      source == "llm_repaired" and story["archetype_title"] == "The Contrarian" and len(calls) == 3
      and "llm" in story["slide_sources"].values() and "template" in story["slide_sources"].values())

story, source, _ = run(edit(archetype_title="42 Shades of Dread"), prose(), "")
check("an invented number in the archetype falls back to the earned title",
      story["archetype_title"] == "The Contrarian")

story, source, _ = run(le.LLMCallError(le.LLMFailure.INFRA_OFFLINE, "down"))
check("an offline curator: no story, a reason, no exception",
      story is None and "unavailable" in source)

story, source, _ = run(edit(), le.LLMCallError(le.LLMFailure.INFRA_OFFLINE, "gone mid-way"))
check("a writer lost mid-way: every slide from its template (labelled so), the editor's plan kept",
      source == "fallback" and set(story["slide_sources"].values()) == {"template"}
      and story["archetype_title"] == "The Loyal Contrarian")

# ── the user's own words, and their language ────────────────────────────────
story, source, _ = run(edit(), prose(), quotes=[{"title": "Mamma Mia!", "text": "It is my sister's film."}],
                       lang="de")
brief, system = calls[1]["messages"][1]["content"], calls[1]["messages"][0]["content"]
check("the writer gets the user's words to quote back, and writes in their language",
      "YOUR WORDS" in brief and "my sister's film" in brief and "in German" in system)

from _checks import memory_engine, with_users  # noqa: E402
engine = memory_engine()
Base.metadata.create_all(engine)
with_users(engine, 1, 2)
Session = sessionmaker(bind=engine)


@contextmanager
def session():
    db = Session()
    try:
        yield db
    finally:
        db.close()


with session() as db:
    db.add(DeletionProposal(id=7, user_id=1, media_id="7", title="Mamma Mia!", service="radarr"))
    for role, text in (("user", "No."), ("user", "It is my sister's film and it stays on the server."),
                       ("assistant", "Fair."), ("user", "Why would I delete it?")):
        db.add(ConversationMessage(user_id=1, role=role, content=text, thread_id="deletion_proposal:7"))
    for text in ("ich habe das nicht gesehen und es ist gut", "das ist nicht mein Film",
                 "the show was not good"):
        db.add(ConversationMessage(user_id=2, role="user", content=text, thread_id="general"))
    db.commit()
    q = ww.user_quotes(db, 1, ["Mamma Mia!", "Heat"])
    lang = ww.main_language(db, 2)
check("the quote is the user's newest statement in that title's debate — not a question, not a one-word answer",
      q == [{"title": "Mamma Mia!", "text": "It is my sister's film and it stays on the server."}])
check("the language is the one they write to the curator in most", lang == "de")
check("English when nothing tips it", ww.detect_language([]) == "en")

import src.services.app_state as aps  # noqa: E402

state = {}
aps.get_state = lambda k: state.get(k)
aps.set_state = lambda k, v: state.__setitem__(k, v)
check("quotes are on until the user turns them off", ww.quotes_allowed(1))
ww.set_quotes_allowed(1, False)
check("...and off for them alone afterwards", not ww.quotes_allowed(1) and ww.quotes_allowed(2))

r = (ROOT / "src/routers/wrapup.py").read_text(encoding="utf-8")
app = (ROOT / "frontend/js/app.js").read_text(encoding="utf-8")
js = (ROOT / "frontend/js/wrapup.js").read_text(encoding="utf-8")
check("the page shows the choice and can change it",
      '@router.post("/prefs")' in r and '"quotes": await run_read(_quotes_allowed' in r
      and "toggleWrapupQuotes," in app and "'/api/wrapup/prefs'" in js)
st_src = (ROOT / "src/services/wrapup_story.py").read_text(encoding="utf-8")
check("the generation task writes through the writer, with the user's voice",
      "await write(facts, sheet, idx, cards, quotes=quotes, lang=lang)" in st_src
      and "quotes_allowed(user_id)" in st_src and "def write_story" not in st_src)

check.done()
