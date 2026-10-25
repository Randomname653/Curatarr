"""The household in a wrap-up: opt-in, only among those who said yes.

Owner decision: anything that reaches other people needs consent. A member
who turns the comparison on is compared with the other members who turned
it on, and with nobody else: a rank by hours, the household median on the
taste radar (two others at least, so the line is never one person's
shape), and a top title no other of them ever played. Nothing is stored,
so turning it off takes a member out of every comparison at once.

    python tests/test_household_compare.py
"""
import json
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.services.household_compare as hc  # noqa: E402
import src.services.app_state as app_state  # noqa: E402
import src.services.wrapup_store as store  # noqa: E402
from src.database.models import Base, User, WatchHistoryEntry as W  # noqa: E402
from src.services.wrapup_facts import FACTS_VERSION  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def fake_db_session():
    s = Session()
    try:
        yield s
        s.commit()
    finally:
        s.close()


hc.get_db_session = fake_db_session
state: dict = {}
app_state.get_state = lambda k: state.get(k)
app_state.set_state = lambda k, v: state.__setitem__(k, v) or True

with fake_db_session() as db:
    for i, name in enumerate(("me", "anna", "ben", "cleo", "dan"), 1):
        db.add(User(id=i, plex_user_id=str(i), plex_username=name, is_active=(name != "dan")))


def facts(hours, top, acclaim, novelty):
    return {"facts_version": FACTS_VERSION, "eligible": True,
            "tempo": {"hours": hours}, "top": [{"title": t, "hours": hours / 4} for t in top],
            "highbrow_comfort": {"highbrow": acclaim}, "discovery": {"new_share": novelty},
            "completion": {"completion_rate": 0.8}, "eras": {"split": {"catalogue": 0.4}}}


STORED = {2: facts(80, ["Heat", "Dune"], 0.2, 0.3), 3: facts(10, ["Heat"], 0.6, 0.7),
          4: facts(30, ["Alien"], 0.4, 0.5), 5: facts(99, ["Dune"], 0.9, 0.9)}
store.load = lambda uid, scope, period: {"facts": STORED.get(uid)}
with fake_db_session() as db:
    for uid, title in ((2, "Heat"), (3, "Dune"), (4, "Alien"), (5, "Severance")):
        db.add(W(user_id=uid, plex_user_id=str(uid), plex_item_id=f"k{uid}", title=title,
                 media_type="movie", viewed_at=datetime(2026, 3, 1)))
ME = facts(50, ["Dune", "Severance", "Heat"], 0.5, 0.4)

# ── 1. consent ───────────────────────────────────────────────────────────────
print("\n-- nobody is compared without saying yes --")
check("off until the member turns it on", hc.consented(1) is False
      and hc.compare(1, "all", "2026", ME) == {"consented": False})
hc.set_consent(1, True)
check("on, but nobody else said yes: nothing to compare", hc.compare(1, "all", "2026", ME)
      == {"consented": True, "members": 1})
hc.set_consent(2, True)
one = hc.compare(1, "all", "2026", ME)
check("one other: a rank, but no median (it would be that person's shape)",
      one["members"] == 2 and one["rank_hours"] == 2 and "radar_median" not in one)
hc.set_consent(3, True)
hc.set_consent(5, True)               # an inactive member is never compared
two = hc.compare(1, "all", "2026", ME)
check("only active members who said yes are compared", two["members"] == 3)
check("rank by hours among them", two["rank_hours"] == 2)
check("the median of the others on each axis",
      two["radar_median"]["Acclaim"] == 0.4 and two["radar_median"]["Novelty"] == 0.5)
check("'only you': the first top title none of them ever played",
      two["only_you"] == "Severance")
check("no other member's name or title leaves the block",
      not any(x in json.dumps(two) for x in ("anna", "ben", "Heat", "Alien")))
hc.set_consent(3, False)
check("turning it off takes a member out at once",
      hc.compare(1, "all", "2026", ME)["members"] == 2)
STORED[2] = {**STORED[2], "facts_version": FACTS_VERSION - 1}
check("a member's wrap-up built on older facts is not compared",
      hc.compare(1, "all", "2026", ME) == {"consented": True, "members": 1})
check("radar axes as the story draws them",
      hc.radar_axes({"tempo": {"hours": 10}, "top": [{"hours": 4}], "contrarian": {"gap": 1.5}})
      == {"Contrarian": 0.5, "Focus": 0.8})

# ── 2. nothing is stored, the story never sees it ────────────────────────────
print("\n-- worked out on open, never stored --")
router = (ROOT / "src/routers/wrapup.py").read_text(encoding="utf-8")
story = (ROOT / "src/services/wrapup_story.py").read_text(encoding="utf-8")
check("the page gets it next to the facts, not inside them",
      '"household": await run_read(compare, user.id, scope, period, facts)' in router
      and 'facts["household"]' not in router)
check("the curator's story is not given it", "household_compare" not in story)
check("the prefs read and set the consent",
      '"household" in body' in router and "set_consent" in router)

# ── 3. the page ─────────────────────────────────────────────────────────────
print("\n-- the page --")
JS = (ROOT / "frontend/js/wrapup_story.js").read_text(encoding="utf-8")
WRAPUP = (ROOT / "frontend/js/wrapup.js").read_text(encoding="utf-8")
APP = (ROOT / "frontend/js/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend/css/app.css").read_text(encoding="utf-8")
check("the toggle is a registered action", "toggleWrapupHousehold," in APP
      and "act('toggleWrapupHousehold')" in WRAPUP)
check("the median line is styled by class, dashed", ".wus-rd-median" in CSS and "dasharray" in CSS)
check("the identity slide passes the household median", "radar(f, d.household?.radar_median)" in JS)
node = shutil.which("node")
if node:
    body = re.sub(r"^import .*$", "", JS, flags=re.M).replace("export function", "function")
    harness = r"""
const window = {matchMedia: () => ({matches: true})}; const document = {};
const esc = s => String(s ?? ''); const act = () => ''; const _posterImg = () => '';
const countTo = () => {}; const haptic = () => {}; const loc = () => undefined;
""" + body + """
const f = {tempo: {hours: 10}, top: [{hours: 4}], highbrow_comfort: {highbrow: 0.5},
  discovery: {new_share: 0.4}, completion: {completion_rate: 0.8}, eras: {split: {catalogue: 0.3}}};
const all = {Acclaim: 0.4, Novelty: 0.5, Commitment: 0.7, Catalogue: 0.2, Contrarian: 0.1, Focus: 0.6};
const part = {Acclaim: 0.4};
console.log(JSON.stringify({with: radar(f, all), without: radar(f), partial: radar(f, part)}));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True, encoding="utf-8")
    res = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {}
    if r.returncode:
        print(r.stderr[:800])
    check("the radar draws the household median as a second, dashed shape",
          "wus-rd-median" in res.get("with", ""))
    check("...not without one, and not when it lacks an axis the shape has",
          "wus-rd-median" not in res.get("without", "x wus-rd-median")
          and "wus-rd-median" not in res.get("partial", "x wus-rd-median"))
else:
    print("  SKIP  node not installed: radar rendering not checked")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
