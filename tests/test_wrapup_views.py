"""Wrap-Up stage-3 views: the rhythm clock, the taste river, consensus
(2026-10-25; docs/WRAPUP_V2_BLUEPRINT.md §4.3, visualizations 2, 7, 8).

The facts carry what they draw (weekday x hour, nanogenre hours by month,
your hours against the community's score with the judge's verdict); the
story mode draws them as SVG with classes only and adds a slide each when
there is enough to show.

    python tests/test_wrapup_views.py
"""
import json
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from src.database.models import (Base, JudgeVerdict, Nanogenre, TitleNanogenre, User,  # noqa: E402
                                 WatchHistoryEntry as W)
from src.services import wrapup_sections as ws  # noqa: E402
from src.services import wrapup_facts as wf  # noqa: E402

from _checks import check  # noqa: E402


engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
Base.metadata.create_all(engine)
S = sessionmaker(bind=engine)
db = S()
db.add(User(id=1, plex_user_id="1", plex_username="o", is_admin=True))
# Friday 2026-10-09 23:00 and Saturday 2026-10-10 10:00 (UTC, the user's clock here)
for i, (t, when, mins) in enumerate([("Heat", datetime(2026, 10, 9, 23, 0), 120),
                                     ("Heat", datetime(2026, 10, 10, 10, 0), 60)]):
    db.add(W(user_id=1, plex_user_id="1", plex_item_id=f"h{i}", title=t, media_type="movie",
             viewed_at=when, duration_ms=mins * 60_000, completed=True))
db.commit()

# ── the clock: weekday x hour, Monday first ─────────────────────────────────
ms = wf.played_ms_sql(W)
base = (W.user_id == 1, W.viewed_at >= datetime(2026, 10, 1), W.viewed_at < datetime(2026, 11, 1))
t = wf._tempo(db, W, ms, base, W.viewed_at, 180 * 60_000, 0, 2)
check("the tempo carries a weekday-by-hour grid, Monday first",
      len(t["clock_h"]) == 7 and all(len(r) == 24 for r in t["clock_h"])
      and t["clock_h"][4][23] == 2.0 and t["clock_h"][5][10] == 1.0
      and sum(map(sum, t["clock_h"])) == 3.0)

# ── the river: nanogenre hours by month ─────────────────────────────────────
db.add(Nanogenre(id=1, grp="video", idx=0, name="crime thriller", size=2, centroid=b"x", version=1))
db.add(Nanogenre(id=2, grp="video", idx=1, name="teen comedy", size=2, centroid=b"x", version=1))
db.add(TitleNanogenre(parent="p1", title="Heat", domain="movie", nanogenre_id=1))
db.add(TitleNanogenre(parent="p2", title="Superbad", domain="movie", nanogenre_id=2))
for i, (title, when, mins) in enumerate([("Heat", datetime(2026, 8, 3), 90), ("Superbad", datetime(2026, 9, 3), 120),
                                         ("Superbad", datetime(2026, 10, 3), 30)]):
    db.add(W(user_id=1, plex_user_id="1", plex_item_id=f"r{i}", title=title, media_type="movie",
             viewed_at=when, duration_ms=mins * 60_000, completed=True))
db.commit()
period = wf.Period("year:2026", datetime(2026, 1, 1), datetime(2027, 1, 1), "2026", False)
ng = ws.nanogenres(db, W, 1, period, ["movie"])
check("the nanogenres carry their hours month by month for the river",
      ng["by_month"]["2026-08"] == {"crime thriller": 1.5} and ng["by_month"]["2026-09"] == {"teen comedy": 2.0}
      and ng["by_month"]["2026-10"]["crime thriller"] == 3.0)

# ── consensus ───────────────────────────────────────────────────────────────
titles = [{"title": f"T{i}", "media_type": "movie", "hours": h} for i, h in enumerate([9, 7, 5, 3, 2, 1, 0.5])]
enriched = {f"T{i}": {"rating": r, "votes": 5000} for i, r in enumerate([5.9, 8.1, 7.0, 7.5, 6.2, 8.8, 7.1])}
enriched["T6"]["votes"] = 50               # too few votes: out
db.add(JudgeVerdict(user_id=1, category="movie", item_key="T0", title="T0", verdict="KEEP"))
db.add(JudgeVerdict(user_id=1, category="movie", item_key="T4", title="T4", verdict="CUT"))
db.commit()
c = ws.consensus(db, 1, titles, enriched)
check("consensus: the titles with a community score, your hours, the judge's last verdict",
      len(c["points"]) == 6 and {p["title"]: p["verdict"] for p in c["points"]}["T0"] == "kept"
      and {p["title"]: p["verdict"] for p in c["points"]}["T4"] == "cut")
check("...and the ones you loved more than the crowd did",
      c["yours_alone"] == ["T0"])
check("too few scored titles: no scatter", ws.consensus(db, 1, titles[:3], enriched) == {})
db.close()
check("the facts are a new generation", wf.FACTS_VERSION >= 11)

# ── drawn in node ───────────────────────────────────────────────────────────
JS = (ROOT / "frontend/js/wrapup_story.js").read_text(encoding="utf-8")
node = shutil.which("node")
if node:
    body = re.sub(r"^import .*$", "", JS, flags=re.M).replace("export function", "function")
    clock_rows = [[0] * 24 for _ in range(7)]
    for d in range(7):
        for h in (8, 20, 21, 22, 23):
            clock_rows[d][h] = 0.5 + d * 0.1 + h * 0.01
    harness = r"""
const window = {matchMedia: () => ({matches: true})};
const document = {};
const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const act = (name) => `data-action="${name}"`;
const _posterImg = (url) => `<img src="${url}">`;
const countTo = () => {}; const haptic = () => {}; const loc = () => undefined;
""" + body + """
const rows = %s;
const byMonth = {"2026-08": {"crime thriller": 1.5, "teen comedy": 0.2}, "2026-09": {"teen comedy": 2.0, "crime thriller": 1},
                 "2026-10": {"crime thriller": 3.0, "teen comedy": 0.5}};
const cons = %s;
const facts = {period: {label: '2026', start: '2026-01-01', end: '2026-12-31'},
  tempo: {hours: 40, plays: 50, active_days: 30, peak_hour: 22, peak_weekday: 5, clock_h: rows},
  nanogenres: {top: [{name: 'crime thriller', hours: 5.5}], new_to_you: ['teen comedy'], by_month: byMonth},
  consensus: cons, top: []};
const slides = build({facts, story: null, insights: []});
console.log(JSON.stringify({clock: clock(rows), clockThin: clock([[1]]), river: river(byMonth),
  riverThin: river({"2026-10": {"a": 1}}), cons: consensus(cons), consThin: consensus({points: []}),
  cls: slides.map(s => s.cls), html: slides.map(s => s.html)}));
""" % (json.dumps(clock_rows), json.dumps(c))
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        print(r.stderr[:1500])
    res = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {}
    check("the clock is 7 x 24 ring sectors, coloured by level classes, with hour marks",
          res.get("clock", "").count("<path") == 168 and 'class="wus-hm l4"' in res.get("clock", "")
          and ">18</text>" in res.get("clock", "") and res.get("clockThin") == "")
    check("the river is one stream per nanogenre, the biggest the strongest shade, named in-stream",
          res.get("river", "").count('class="wus-rv s') == 2 and 'class="wus-rv s0"><title>crime thriller' in res.get("river", "")
          and ">crime thriller</text>" in res.get("river", "") and res.get("riverThin") == "")
    check("consensus marks a kept title with a triangle and a cut one with a cross",
          res.get("cons", "").count("wus-cs-kept") == 1 and res.get("cons", "").count("wus-cs-cut") == 1
          and res.get("cons", "").count("<circle") == 4 and res.get("consThin") == "")
    cls = res.get("cls", [])
    check("the story adds the clock after the opening, river and consensus before the end",
          "clock" in cls and "river" in cls and "consensus" in cls
          and cls.index("river") < cls.index("consensus") < cls.index("end"))
    html = dict(zip(cls, res.get("html", [])))
    check("...with their own words: the peak hour and day, the leader and what is new, yours alone",
          "Most at 22:00, busiest on Fridays" in html.get("clock", "")
          and "crime thriller</h2>" not in html.get("river", "") and "crime thriller led" in html.get("river", "")
          and "New to you: teen comedy" in html.get("river", "") and "Yours alone: T0" in html.get("consensus", ""))
else:
    print("  (node not installed - checked statically)")
check("no inline style in the story mode", 'style="' not in JS and ".style." not in JS)

check.done()
