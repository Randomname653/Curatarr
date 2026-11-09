"""The UI in the user's language, round two (UX design record 3.8): Swipe,
the Wrap-Up and its story, History.

* Drawn in German, the Swipe card, the deck's end, the story's slides and the
  Wrap-Up page say German words - and the same actions, arguments, reaction
  kinds and titles as in English.
* The story speaks the user's language: the setting (Settings → Account)
  beats the detected chat language.
* The page's server words follow it too: the insights' claims for the quiet
  path (no story yet), the traits' evidence, the milestones' phrases. The
  stored facts stay English: the curator reads them.

    python tests/test_i18n_round_b.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores
from _i18n_js import i18n_js

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "frontend" / "js"
node = shutil.which("node")

FACTS = {
    "eligible": True, "scope": "all",
    "period": {"key": "week:2026-W45", "label": "Week 45, 2026", "start": "2026-11-02", "end": "2026-11-09"},
    "tempo": {"hours": 14.5, "plays": 30, "active_days": 6, "longest_streak_days": 4, "peak_hour": 21,
              "peak_weekday": 5, "biggest_day": {"date": "2026-11-06", "hours": 4.2},
              "by_hour_h": [0] * 20 + [2, 5, 3, 1], "top_calendar": ["2026-11-03", "2026-11-04", "2026-11-06"],
              "dayparts": {"night": 0.45},
              "by_day_h": {f"2026-11-0{d}": 1.5 + d for d in range(2, 8)},
              "clock_h": [[0] * 20 + [1, 2, 1.5, 0.5] for _ in range(7)]},
    "top": [{"title": "Harbour Lights", "media_type": "show", "hours": 6.0},
            {"title": "The Glass Orchard", "media_type": "movie", "hours": 3.0},
            {"title": "Pale Engine", "media_type": "anime", "hours": 2.5},
            {"title": "Copper Fields", "media_type": "movie", "hours": 1.5}],
    "highbrow_comfort": {"highbrow": 0.6, "comfort": 0.2, "other": 0.2},
    "dismissal": {"title": "Salt Road", "media_type": "movie", "how": "dropped", "community": 8.4},
    "discovery": {"new_share": 0.7, "best_new": "Pale Engine"},
    "comebacks": [{"title": "Copper Fields", "gap_days": 120}],
    "curatarr": {"most_debated": {"title": "Salt Road", "messages": 9, "outcome": "deleted"}},
    "drift": {"label": "shifting", "clusters": [{"id": "C1", "share": 0.5, "trend": "rising",
                                                  "top_titles": ["Harbour Lights"]}]},
    "traits": [{"key": "night_owl", "evidence": "45% after 23:00", "strength": 0.4}],
    "consensus": {"duel": {"title": "Harbour Lights", "kind": "yours", "your_rank": 1, "crowd_rank": 7,
                           "hours": 6.0, "community": 6.9, "of": 9}, "yours_alone": ["Harbour Lights"]},
    "posters": {},
}


def _ids(html):
    """What must not change with the language: actions, their arguments,
    reaction kinds, titles in bold."""
    return (re.findall(r'data-action="(\w+)"', html), re.findall(r"data-args='([^']*)'", html),
            re.findall(r'data-kind="(\w+)"', html), re.findall(r"<b>([^<]*)</b>", html))


def _module(name):
    body = (JS / name).read_text(encoding="utf-8")
    body = re.sub(r"^import .*$", "", body, flags=re.M)
    return body.replace("export async function", "async function").replace("export function", "function")


def _run(js):
    r = subprocess.run([node, "-"], input=js, capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        print(r.stderr[:1200])
        return {}
    return json.loads(r.stdout.strip().splitlines()[-1])


STUBS = r"""
const window = {matchMedia: () => ({matches: true})};
const document = {createElement: () => ({dataset: {}, setAttribute() {}, querySelector: () => null,
  addEventListener() {}, set innerHTML(v) { this._h = v; }, get innerHTML() { return this._h; }}),
  getElementById: () => null};
const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const escAttr = esc;
const act = (name, ...args) => `data-action="${name}"` + (args.length ? ` data-args='${JSON.stringify(args)}'` : '');
const actOn = (ev, name, ...args) => act(name, ...args);
const EL = '$el';
const _posterImg = () => '<img>';
const countTo = () => {}; const haptic = () => {}; const toast = () => {};
const loc = () => (__LANG === 'de' ? 'de-DE' : 'en-US');
const serverLabel = () => 'Plex';
const state = {currentUser: {is_admin: false}};
const brandMark = () => ({}); const brandMarkHtml = () => '';
const api = async () => ({}); const proxyImg = u => u; const emptyHtml = () => ''; const activeDialog = () => null;
const _errHtml = () => ''; const _errMsg = e => String(e); const btnBusy = () => {}; const btnDone = () => {};
const _busyStreaming = () => false; const enterThread = async () => false; const sendMessage = () => {};
const _bar = (cls, pct) => `<i class="${cls}" data-pct="${pct}"></i>`; const renderLedger = () => {};
"""


def render(lang):
    story = "const S = (() => {\n" + _module("wrapup_story.js") + \
        "\nreturn {build, periodLabel, seasonLabel};\n})();\n"
    page = "const W = (() => {\nconst {periodLabel, seasonLabel} = S; const playStory = () => {};\n" + \
        _module("wrapup.js") + "\nreturn {_bento, _controls, _hero, wu};\n})();\n"
    swipe = "const SW = (() => {\n" + _module("swipe.js") + \
        "\nreturn {card: c => _cardNode(c).innerHTML, end: () => { _deck = {n: 10, wantOk: 2, loved: ['Harbour Lights']," \
        " chained: false, lifted: 0, start: Date.now() - 95000}; return _deckEndHtml(); }, REACT};\n})();\n"
    return _run(i18n_js(lang) + STUBS + story + page + swipe + """
const facts = %s;
const d = {facts, story: null, insights: [{id: 'I01', slide: 'open', claim: 'CLAIM'}], scopes: ['all', 'movie'],
           seasons: [], years: []};
const slides = S.build(d);
W.wu.data = d;
const card = {doc_id: 'x1', title: 'Harbour Lights', year: 2019, genres: ['Drama'], details: {seasons: 2, episodes: 16},
              anchor: {kind: 'loved', title: 'Copper Fields'}, fit: 0.8, synopsis: 'A lighthouse keeper.'};
console.log(JSON.stringify({
  slides: slides.map(s => s.html).join('\\n'), shares: slides.map(s => s.share || ''),
  periods: ['week:2026-W45', 'month:2026-10', 'season:2026-fall', 'year:2025', '30d', 'all']
    .map(key => S.periodLabel({key, label: 'L'})),
  page: W._controls(d) + W._hero(facts, d) + W._bento(facts),
  card: SW.card(card), end: SW.end(),
  react: Object.fromEntries(Object.entries(SW.REACT).map(([k, r]) => [k, r.label])),
}));
""" % json.dumps(FACTS))


if node:
    en, de = render("en"), render("de")
    check("the rigs ran in both languages", bool(en.get("card") and de.get("card") and en.get("page")))
    check("Swipe in German: the card's ribbon and fit, the reactions' words",
          all(w in de.get("card", "") for w in ("Weil du", "geliebt hast", "2 Staffeln", "16 Folgen", "Geschmack"))
          and de.get("react", {}).get("notseen") == "Nicht gesehen" and de["react"]["loved"] == "Geliebt",
          (de.get("card", "")[:800], de.get("react")))
    check("...the deck's end in German: what went on the watchlist, another ten, done for tonight",
          all(w in de.get("end", "") for w in ("Plex-Watchlist", "Stapel fertig", "Noch 10", "Für heute fertig")), de.get("end", "")[:800])
    check("...the same actions, reaction kinds and titles as in English",
          en.get("card") and _ids(en["card"]) == _ids(de["card"]) and _ids(en["end"]) == _ids(de["end"])
          and set(en["react"]) == set(de["react"]), (_ids(en.get("card", "")), _ids(de.get("card", ""))))
    check("the story's slides in German: the hours, the reveal, the rhythm, the clock, yours alone",
          all(w in de.get("slides", "") for w in ("Stunden", "Deine Nummer eins", "Der Rhythmus", "Rund um die Uhr",
                                                     "am vollsten am Freitag", "Nur deins", "für dich", "Teilen"))
          and "hours" not in re.sub(r"<[^>]*>", "", de.get("slides", "")), de.get("slides", "")[:1500])
    check("...a slide's share image says German too",
          any(s.startswith("14,5 Stunden — KW 45, 2026") for s in de.get("shares", [])), de.get("shares"))
    check("...a quiet period's claim stays the server's (the server writes it in the user's language)",
          "CLAIM" in de.get("slides", ""))
    check("...with the same actions and titles as in English", _ids(en.get("slides", "")) == _ids(de.get("slides", "")))
    check("a period's name in German, from its key; the English the server's",
          de.get("periods") == ["KW 45, 2026", "Oktober 2026", "Saison Herbst 2026", "2025 im Rückblick",
                                "Letzte 30 Tage", "Gesamte Zeit"]
          and en.get("periods") == ["Week 45, 2026", "October 2026", "Fall 2026 season", "2025 in review",
                                    "Last 30 days", "All time"], (de.get("periods"), en.get("periods")))
    check("the Wrap-Up page in German: chips, tiles, a dropped title, a debate's outcome, the drift",
          all(w in de.get("page", "") for w in ("Diese Woche", "Stunden &amp; Rhythmus", "Entdecker", "abgebrochen",
                                                  "Ausgang: gelöscht", "im Wandel", "Das Comeback", "Die Nachtschicht"))
          and "Hours &amp; rhythm" not in de.get("page", ""), de.get("page", "")[:1500])
    check("...with the same actions and arguments as in English", _ids(en.get("page", "")) == _ids(de.get("page", "")),
          (_ids(en.get("page", ""))[:2], _ids(de.get("page", ""))[:2]))
else:
    print("  (node not installed - the German rigs are skipped)")

# ── the server: the story's language, the page's words ──────────────────────

conn = app_db()
from src.database.models import ConversationMessage, User  # noqa: E402
from src.services import user_language as ul  # noqa: E402
from src.services import wrapup_story  # noqa: E402

with conn.get_db_session() as db:
    u = User(plex_user_id="i18n-b", plex_username="Member")
    db.add(u)
    db.commit()
    UID = u.id
    for text in ("What should I watch tonight? Something calm would be good.",
                 "I liked the series, but the ending was not that good.", "And what about the film from yesterday?"):
        db.add(ConversationMessage(user_id=UID, role="user", content=text, thread_id="general"))
    db.commit()

check("Automatic: the story speaks the chats' language", wrapup_story._voice(UID, [])[0] == "en")
ul.set_setting(UID, "de")
check("German picked: the story is German, though the chats are English",
      wrapup_story._voice(UID, [])[0] == "de" and ul.detected_language(UID) == "en")

from src.routers import wrapup as wr  # noqa: E402
from src.services import milestones as ms  # noqa: E402
from src.services.wrapup_insights import mine  # noqa: E402

said = wr._in_language(FACTS, "de")
check("the traits' evidence in German on the page; the stored facts stay English",
      said["traits"][0]["evidence"] == "45% nach 23 Uhr" and FACTS["traits"][0]["evidence"] == "45% after 23:00"
      and wr._in_language(FACTS, "en") is FACTS, said["traits"])
check("a milestone's phrase in German, the English one unchanged for the curator",
      ms.phrase({"kind": "films", "value": 100}, "de") == "100. Film geschaut"
      and ms.phrase({"kind": "films", "value": 1}, "de") == "Erster Film geschaut"
      and ms.phrase({"kind": "hours", "value": 1000}, "de") == "1.000 Stunden abgespielt"
      and ms.phrase({"kind": "films", "value": 100}) == "100th film watched"
      and ms.phrase({"kind": "years", "value": 2}) == "2 years since the first play")


async def _page():
    async def facts(user, scope, period):
        return FACTS, {"story": {"lang": "en", "archetype_title": "x"}, "story_hash": None}
    saved = wr.current_facts, wr._milestones, wr._decades_prev
    wr.current_facts = facts
    wr._milestones = lambda uid, period, lang="en": {"period": [], "history": [
        {"kind": "films", "value": 100, "phrase": ms.phrase({"kind": "films", "value": 100}, lang)}]}
    wr._decades_prev = lambda *a: None
    try:
        return await wr.get_wrapup(scope="all", period="week:2026-W45",
                                   user=SimpleNamespace(id=UID, is_admin=False, timezone=None))
    finally:
        wr.current_facts, wr._milestones, wr._decades_prev = saved


page = asyncio.run(_page())
en_claims = [c["claim"] for c in mine(FACTS)]
de_claims = [c["claim"] for c in page["insights"]]
check("GET /api/wrapup for a German user: the insights' claims in German (the quiet path's slides)",
      de_claims and de_claims != en_claims and [c["id"] for c in page["insights"]] == [c["id"] for c in mine(FACTS)],
      (de_claims[:2], en_claims[:2]))
check("...the traits' evidence and the milestones in German",
      page["facts"]["traits"][0]["evidence"] == "45% nach 23 Uhr"
      and page["milestones"]["history"][0]["phrase"] == "100. Film geschaut")
check("...and it says the story is English while the user's language is German: the page offers a rewrite",
      page["story_lang"] == "en" and page["user_lang"] == "de")
ul.set_setting(UID, "auto")

from _i18n_js import dictionary  # noqa: E402
EN, DE = dictionary("en"), dictionary("de")
check("an archetype's title on the page is the one the server's story stand-in gives, in both languages",
      all(EN[f"wrapup.trait_title.{k}"] == wrapup_story.trait_title(k, "en")[0]
          and DE[f"wrapup.trait_title.{k}"] == wrapup_story.trait_title(k, "de")[0] for k in wrapup_story.TRAIT_TITLES))
check.done()
