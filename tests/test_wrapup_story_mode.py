"""Wrap-up v2, step 3: the story mode — one idea per screen, the identity last.

v1 put everything on one scroll: the archetype first (the conclusion before
the evidence), fifteen tiles of equal weight, the story under them. The
story mode (frontend/js/wrapup_story.js) tells the period as a paced
sequence and draws its charts as SVG with attributes and classes only.

The slide and chart builders run in node with the UI helpers stubbed (like
tests/test_rec_why.py); the wiring is checked in the source.

    python tests/test_wrapup_story_mode.py
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "frontend/js/wrapup_story.js").read_text(encoding="utf-8")
WRAPUP = (ROOT / "frontend/js/wrapup.js").read_text(encoding="utf-8")
APP = (ROOT / "frontend/js/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend/css/app.css").read_text(encoding="utf-8")

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


DATA = {
    "facts": {
        "period": {"label": "2026 in review", "start": "2026-01-01T00:00:00", "end": "2026-12-31T00:00:00"},
        "tempo": {"hours": 58.3, "plays": 90, "active_days": 40, "longest_streak_days": 9,
                  "biggest_day": {"date": "2026-03-14", "hours": 8.0},
                  "by_day_h": {"2026-03-14": 8.0, "2026-03-15": 1.0, "2026-03-16": 2.0,
                               "2026-06-01": 0.5, "2026-12-30": 3.0}},
        "top": [{"title": "Severance", "media_type": "show", "hours": 20.0},
                {"title": "Heat", "media_type": "movie", "hours": 5.6},
                {"title": "Dune", "media_type": "movie", "hours": 4.0}],
        "posters": {"Severance": "https://image.tmdb.org/t/p/w500/sev.jpg"},
        "court": {"decided": 9, "kept": 5, "deleted": 4, "overrides": 2, "won": 4, "lost": 1},
        "velocity": {"gb_in": 900, "gb_out": 300, "by_month": {"2026-02": {"in": 80, "out": 10},
                                                                 "2026-03": {"in": 20, "out": 290}}},
        "highbrow_comfort": {"highbrow": 0.34}, "discovery": {"new_share": 0.6},
        "completion": {"completion_rate": 0.9}, "eras": {"split": {"catalogue": 0.5}},
        "contrarian": {"gap": 2.6},
    },
    "story": {
        "version": 2, "archetype_title": "The Loyal Contrarian", "archetype_subhead": "Nine for The Bear.",
        "share_line": "A year of rituals",
        "slides": [{"id": "I01", "beat": "court", "headline": "Overruled", "text": "It stayed.", "item_refs": ["T9"]},
                   {"id": "I02", "beat": "balance", "headline": "In and out", "text": "A hoarder.", "item_refs": []},
                   {"id": "I03", "beat": "twist", "headline": "<b>The twist</b>", "text": "9 vs 6.4", "item_refs": []}],
        "roast_and_toast": {"roast": "A 9 for a 6.4.", "toast": "When it lands, it lands."},
        "look_ahead_hook": {"challenge": "Stalker is waiting.", "item_ref": "U1"},
        "refs": {"T9": {"title": "Grown Ups 2", "media_type": "movie", "poster": None},
                 "U1": {"title": "Stalker", "media_type": "movie", "poster": "https://x/stalker.jpg"}},
    },
    "insights": [],
}

node = shutil.which("node")
if node:
    body = re.sub(r"^import .*$", "", JS, flags=re.M).replace("export function", "function")
    harness = r"""
const window = {matchMedia: () => ({matches: true})};
const document = {};
const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const act = (name, ...args) => `data-action="${name}"` + (args.length ? ` data-args='${JSON.stringify(args)}'` : '');
const _posterImg = (url) => `<img src="${url}">`;
const countTo = () => {}; const haptic = () => {}; const loc = () => undefined;
""" + body + """
const data = %s;
const slides = build(data);
const quiet = build({facts: {period: {label: 'x', start: '2026-10-01', end: '2026-10-31'},
  tempo: {hours: 3, plays: 5, active_days: 2}, top: [{title: 'A', hours: 3}]}, story: null, insights: [
  {id: 'I01', slide: 'open', claim: 'Three hours.'}]});
console.log(JSON.stringify({
  cls: slides.map(s => s.cls), html: slides.map(s => s.html),
  quiet: quiet.map(s => s.cls), quietHtml: quiet.map(s => s.html),
  heat: heatmap(data.facts.tempo.by_day_h, data.facts.period.start, data.facts.period.end),
  heatAll: heatmap({'2020-01-01': 1, '2026-12-01': 2}, '2015-01-01T00:00:00', '2026-12-31T00:00:00'),
  axes: radarAxes(data.facts), axesThin: radarAxes({tempo: {}}), radarThin: radar({tempo: {}}),
  dwellShort: dwellMs('<b>10h</b>'), dwellLong: dwellMs('<p>' + 'word '.repeat(30) + '</p>'),
  dwellHuge: dwellMs('word '.repeat(500)),
}));
""" % json.dumps(DATA)
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        print(r.stderr[:1500])
    res = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {}
    cls = res.get("cls") or []
    check("the period is told in order: number, prediction, reveal, rhythm, the curator's slides, "
          "roast and toast, who you were, the send-off",
          cls == ["open", "quiz", "reveal", "rhythm", "beat beat-court", "beat beat-balance",
                  "beat beat-twist", "pair", "identity", "end"])
    html = res.get("html") or []
    check("the identity comes last of the story, after the evidence",
          "The Loyal Contrarian" in html[8] and "The Loyal Contrarian" not in "".join(html[:8]))
    check("the court slide draws the debate bars, the balance slide the GB bars",
          "wus-ct-you" in html[4] and "wus-bal-in" in html[5])
    check("a slide's text is escaped", "&lt;b&gt;The twist&lt;/b&gt;" in html[6] and "<b>The twist" not in html[6])
    check("no artwork, no empty frame: a poster only when there is a picture",
          "<img" not in html[4] and '<img src="https://image.tmdb.org/t/p/w500/sev.jpg">' in html[2]
          and '<img src="https://x/stalker.jpg">' in html[9])
    check("the quiz offers the top titles; the end offers sharing and the details",
          all(t in html[1] for t in ("Severance", "Heat", "Dune")) and 'data-action="shareWrapup"' in html[9]
          and 'data-action="closeWrapupStory"' in html[9])
    check("a quiet period without a written story is short, told from the insight cards",
          res.get("quiet") == ["open", "reveal", "beat beat-open", "end"]
          and "Three hours." in res["quietHtml"][2])
    heat = res.get("heat", "")
    check("the heatmap is one cell per day with its hours, coloured by level classes",
          heat.count("<rect") == 365 and 'class="wus-hm l4"' in heat and "2026-03-14 · 8 h" in heat
          and "style=" not in heat)
    check("...a longer period shows its last 365 days", res.get("heatAll", "").count("<rect") == 365)
    check("the radar has six axes from the facts", [a[0] for a in res.get("axes", [])] == [
        "Acclaim", "Novelty", "Commitment", "Catalogue", "Contrarian", "Focus"])
    check("a slide stays as long as its text takes to read (seen live: 7 s cut slides off)",
          res.get("dwellShort") == 6000 and res.get("dwellLong") == 3000 + 30 * 330
          and res.get("dwellHuge") == 20000)
    check("...and is left out under four known axes", res.get("radarThin") == "" and len(res.get("axesThin", [])) < 4)
else:
    print("  (node not installed - render checked statically)")

check("charts are SVG coloured by class — no inline style anywhere in the story mode",
      'style="' not in JS and "data-style" not in JS)
check("the page offers the story and opens a curator-written one the first time it is seen",
      "export function playWrapupStory()" in WRAPUP and "if (story) { _markSeen(); playStory(wu.data); }" in WRAPUP
      and "wu.data?.story?.version !== 2" in WRAPUP and "act('playWrapupStory')" in WRAPUP)
check("the bento stays behind it as Explore", '<h2 class="wu-explore">Explore</h2>' in WRAPUP)
for name in ("playWrapupStory", "answerWrapupStory", "closeWrapupStory", "nextWrapupSlide", "prevWrapupSlide"):
    check(f"action {name} is registered", re.search(rf"^\s+{name},$", APP, re.M) is not None)
check("Escape closes it, the arrows move, holding pauses, a hold is not a tap",
      "e.key === 'Escape'" in JS and "ArrowRight" in JS and "_hold(true)" in JS and "_wasHold()" in JS)
check("auto-advance waits the slide's reading time, also after a hold",
      JS.count("setTimeout(nextWrapupSlide, dwellMs(") == 2 and "ADVANCE_MS" not in JS)
check("reduced motion: no slide animation and no auto-advance",
      "@media (prefers-reduced-motion: reduce) { .wus-stage.is-in { animation: none; } }" in CSS
      and "!REDUCED.matches) st.timer = setTimeout(nextWrapupSlide" in JS)
src = (ROOT / "src/services/wrapup_facts.py").read_text(encoding="utf-8")
check("the facts carry the hours of every active day for the heatmap", '"by_day_h":' in src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
