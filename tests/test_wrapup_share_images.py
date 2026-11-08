"""The Wrap-Up's share images per slide (stage 3.6 of
docs/UX_RETENTION_DESIGN.md): a slide's own ``share`` line drawn on a canvas
in the browser - the period's label, the line, the wordmark, nothing else.
No upload, no server name, no user name on the picture.

The story module runs in node with a recording canvas; every title and name
is invented.

    python tests/test_wrapup_share_images.py
"""
from _harness import check  # noqa: I001 - first: temp stores

import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
node = shutil.which("node")

DATA = {
    "facts": {
        "period": {"label": "Week 45, 2026", "start": "2026-11-02T00:00:00", "end": "2026-11-09T00:00:00"},
        "tempo": {"hours": 12.5, "plays": 14, "active_days": 6, "longest_streak_days": 4,
                  "peak_hour": 21, "busiest_weekday": 5},
        "top": [{"title": "Starfall Archive", "media_type": "anime", "hours": 8.0},
                {"title": "The Lantern Room", "media_type": "movie", "hours": 2.0}],
        # what must never reach the picture
        "user": {"name": "Member Fennick"}, "server_name": "Fennick Basement",
        "household": [{"name": "Housemate Orla", "hours": 3.0}],
    },
    "story": {
        "version": 2, "archetype_title": "The Night Archivist", "archetype_subhead": "Eight hours of one archive.",
        "share_line": "A week of lanterns",
        "slides": [{"id": "I01", "beat": "open", "headline": "One archive, all week", "text": "It was Starfall.",
                    "item_refs": []}],
        "refs": {},
    },
    "insights": [],
}

# the miner's own cards: a device card is private, the others are not
from src.services.wrapup_insights import mine  # noqa: E402
from src.services.wrapup_writer import assemble  # noqa: E402

INSIGHTS = mine({"devices": {"night": {"player": "Fennick Den TV", "share": 0.6, "hours": 12.0}},
                 "tempo": {"hours": 20.0, "plays": 20}, "top": [{"title": "Starfall Archive", "media_type": "anime",
                                                                  "hours": 8.0, "plays": 8, "share": 0.4}]})
device = next(c for c in INSIGHTS if "Fennick" in c["claim"])
check("the miner marks the card that names a device as private, and only that one",
      device.get("private") is True and not any(c.get("private") for c in INSIGHTS if c is not device), INSIGHTS)
story = assemble({"arc": [device["id"]], "archetype_title": "T", "archetype_subhead": "S", "share_line": "L"},
                 {device["id"]: ("Late hours", device["claim"]), "ROAST": ("", "r"), "TOAST": ("", "t")},
                 {device["id"]: "template"}, {device["id"]: device}, {"items": {}, "clusters": {}})
check("...and a written story keeps the mark on its slide", story["slides"][0].get("private") is True,
      story["slides"][0])

if node:
    js = (ROOT / "frontend/js/wrapup_story.js").read_text(encoding="utf-8")
    body = re.sub(r"^import .*$", "", js, flags=re.M).replace("export function", "function") \
        .replace("export async function", "async function")
    harness = r"""
const drawn = [];
const fakeCtx = {fillRect() {}, fillText(t) { drawn.push(String(t)); },
  createRadialGradient: () => ({addColorStop() {}})};
const downloads = [];
const document = {
  documentElement: {},
  createElement: tag => tag === 'canvas'
    ? {getContext: () => fakeCtx, toBlob: res => res({png: true})}
    : {click() { downloads.push(this.download); }, remove() {}},
  body: {appendChild() {}},
};
const getComputedStyle = () => ({getPropertyValue: () => ''});
const window = {matchMedia: () => ({matches: true})};
const shared = [];
const navigator = {};
class File { constructor(parts, name, o) { this.name = name; this.type = o.type; } }
const URL = {createObjectURL: () => 'blob:x', revokeObjectURL() {}};
const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const act = (name, ...args) => `data-action="${name}"`;
const _posterImg = (url) => `<img src="${url}">`;
const countTo = () => {}; const haptic = () => {}; const loc = () => undefined;
""" + body + """
(async () => {
  const data = %s;
  const slides = build(data);
  const label = data.facts.period.label;
  const perSlide = [];
  for (const sl of slides.filter(s => s.share)) {
    drawn.length = 0;
    await _slideImage(sl, label);
    perSlide.push({share: sl.share, drawn: drawn.slice(), expect: shareCardLines(sl.share, label)});
  }
  st.data = data; st.slides = slides; st.i = 0; st.el = null;
  await shareWrapupSlide();                                  // no share sheet: a download
  navigator.canShare = () => true;
  navigator.share = async o => { shared.push({files: o.files.map(f => [f.name, f.type]), text: o.text}); };
  await shareWrapupSlide();                                  // the share sheet, with the file
  const plain = slides.findIndex(s => !s.share);
  // told from the cards alone (no story yet): a device card names what the household named
  const cardsOnly = build({facts: data.facts, story: null, insights: %s});
  // a written slide without a headline: its body text may name anything
  const bare = build({facts: data.facts, story: {version: 2, slides: [
    {id: 'I01', beat: 'rhythm', headline: '', text: 'After 23:00 it was the Fennick Den TV.', item_refs: []}], refs: {}},
    insights: []});
  console.log(JSON.stringify({
    perSlide, downloads, shared, paused: st.paused, noShareSlide: plain,
    cardsOnly: cardsOnly.filter(x => x.cls.startsWith('beat')).map(x => x.share),
    bare: bare.filter(x => x.cls.startsWith('beat')).map(x => x.share),
    wrap: shareCardLines('a very long line that goes on and on past what fits on one row', 'Label', 22),
    many: shareCardLines('word '.repeat(80), 'L').lines.length,
    names: [shareFileName('Week 45, 2026', 0), shareFileName('', 2), shareFileName('Oktober 2026 · Rückblick', 1)],
  }));
})();
""" % (json.dumps(DATA), json.dumps(INSIGHTS))
    r = subprocess.run([node, "-"], input=harness, capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        print(r.stderr[:2000])
    res = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {}
    per = res.get("perSlide") or []
    check("every slide with a share line can become a picture", len(per) >= 4, len(per))
    check("a picture says the period, the slide's line and the wordmark - nothing else",
          per and all(p["drawn"] == [p["expect"]["eyebrow"].upper(), *p["expect"]["lines"], "Curatarr"] for p in per),
          per)
    every = " ".join(" ".join(p["drawn"]) for p in per)
    check("no user name, no server name, no household name on any picture",
          per and not any(n.lower() in every.lower() for n in ("Fennick", "Orla", "Basement")), every)
    check("the line wraps at the width and never runs past nine lines",
          res.get("wrap", {}).get("lines") and all(len(x) <= 22 for x in res["wrap"]["lines"])
          and " ".join(res["wrap"]["lines"]) == "a very long line that goes on and on past what fits on one row"
          and res.get("many") == 9, res.get("wrap"))
    check("without a share sheet the picture is saved, named after the period and the slide",
          res.get("downloads") == ["curatarr-week-45-2026-1.png"], res.get("downloads"))
    check("with a share sheet the PNG goes to it with the slide's line, nothing uploaded",
          res.get("shared") == [{"files": [["curatarr-week-45-2026-1.png", "image/png"]], "text": per[0]["share"]}]
          if per else False, res.get("shared"))
    check("the story is paused while sharing and runs on after", res.get("paused") is False)
    check("file names are plain: an empty label falls back, accents drop, other signs become dashes",
          res.get("names") == ["curatarr-week-45-2026-1.png", "curatarr-wrap-up-3.png",
                               "curatarr-oktober-2026-ruckblick-2.png"], res.get("names"))
    check("a card that names a device gives no share line, told from the cards alone; the others do",
          res.get("cardsOnly") and "" in res["cardsOnly"] and not any("Fennick" in x for x in res["cardsOnly"])
          and any(res["cardsOnly"]), res.get("cardsOnly"))
    check("a written slide without a headline offers no share line: its text is not a share line",
          res.get("bare") == [""], res.get("bare"))
else:
    print("  (node not installed - share images not run)")

check.done()
