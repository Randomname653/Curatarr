"""The deletion card's "Why?" — the score taken apart, and that it adds up.

2026-10 UI audit: each card showed "87%" with the tooltip "How sure the
judge is that this can go". It was neither a probability nor the judge's:
it is the shortlist score (taste mismatch × 80 + size − ratings + feedback
+ similarity to dropped titles − listening protection − learned keep
rules) divided by 100 and clamped. The card now says Cut / Your call, and
"Why?" lists the strongest terms of that score. This suite pins that the
terms are labelled honestly, add up to the score they explain, survive the
trip through the database, and that existing databases gain the column.

    python tests/test_score_factors.py
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.database.connection as conn_mod
import src.routers.recommendations as recs
import src.services.recommendations_engine as eng
from src.database.models import Base, DeletionProposal
from tests.frontend_files import everything

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


TERMS = dict(mismatch=0.82, taste_src="embedding", size_pts=9.4, size_gb=45.0,
             size_ratio=2.1, rating=8.0, rating_swing=12.0, user_rating=None,
             user_rating_swing=0.0, feedback_swing=15.0, drop_penalty=6.5,
             play_prot=0.0, plays=0)
KEEP = 18.0


def score_of(t, keep=0.0):
    """The engine's own formula (generate_deletion_proposals), restated."""
    return (t["mismatch"] * 80 + t["size_pts"] - t["rating_swing"] - t["user_rating_swing"]
            + t["feedback_swing"] + t["drop_penalty"] - t["play_prot"] - keep)


f = eng.deletion_score_factors(**TERMS, keep_value_pts=KEEP,
                               considerations=[{"content": "Keep anything my partner rewatches", "strength": .6}])
by = {x["key"]: x for x in f}

# ── labels and signs ─────────────────────────────────────────────────────────
check("the factors add up to the score they explain",
      abs(sum(x["points"] for x in f) - score_of(TERMS, KEEP)) < 0.5)
check("taste far from the history pushes toward deleting",
      by["taste"]["points"] > 60 and by["taste"]["label"] == "Far from your watch history")
check("an oversized file names its size and ratio",
      by["size"]["label"] == "45 GB, 2.1× typical for its format" and by["size"]["points"] > 0)
check("a good community rating pulls toward keeping",
      by["rating"]["points"] == -12.0 and by["rating"]["label"] == "Community rating 8.0/10")
check("recorded dislike is said in plain words", by["feedback"]["label"] == "You said you didn't like it")
check("similarity to dropped titles is named", "stopped watching" in by["dropped"]["label"])
check("a learned keep rule pulls toward keeping and quotes the rule",
      by["keep_rules"]["points"] == -18.0 and "partner rewatches" in by["keep_rules"]["label"])
check("terms that contributed nothing are left out", "your_rating" not in by and "plays" not in by)
check("no label leaks internal vocabulary",
      not any(re.search(r"judge|pillar|cosine|mismatch|embedding|_", x["label"]) for x in f))

g = eng.deletion_score_factors(**{**TERMS, "mismatch": 0.2, "taste_src": "genre", "size_ratio": None,
                                  "user_rating": 9.0, "user_rating_swing": 24.0,
                                  "feedback_swing": -15.0, "play_prot": 12.0, "plays": 140})
gb = {x["key"]: x for x in g}
check("a close genre match says so", gb["taste"]["label"] == "Close to the genres you watch")
check("plain size without a format norm", gb["size"]["label"] == "45 GB on disk")
check("the owner's own rating pulls toward keeping", gb["your_rating"]["points"] == -24.0
      and gb["your_rating"]["label"] == "You rated it 9/10")
check("praise pulls toward keeping", gb["feedback"]["points"] < 0 and gb["feedback"]["label"] == "You said you liked it")
check("listening depth pulls toward keeping", gb["plays"]["points"] == -12.0 and "140 times" in gb["plays"]["label"])
n = eng.deletion_score_factors(**{**TERMS, "taste_src": "none", "mismatch": 0.5})
check("no taste data is called neutral, not a mismatch", "neutral" in {x["key"]: x for x in n}["taste"]["label"])

# ── the payload a proposal carries ───────────────────────────────────────────
sf = eng._score_factors({"score": score_of(TERMS, KEEP), "score_terms": TERMS, "keep_value_pts": KEEP})
check("a shortlisted candidate carries total + factors", sf and sf["total"] == round(score_of(TERMS, KEEP), 1) and sf["factors"])
check("no recorded terms → no breakdown", eng._score_factors({"score": 50}) is None)
check("broken terms never cost the proposal", eng._score_factors({"score": 50, "score_terms": {"bogus": 1}}) is None)

src = (ROOT / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
formula = re.search(r"del_score = (.+)", src).group(1)
recorded = src.split('"score_terms": dict(')[1].split('\n                ),')[0]
for term in ("mismatch", "size_pts", "rating_swing", "user_rating_swing", "feedback_swing", "drop_penalty", "play_prot"):
    check(f"score term {term} is in the formula AND recorded for Why?", term in formula and f"{term}=" in recorded)
check("both proposal builders (pillar + legacy) attach the breakdown",
      src.count('"score_factors": _score_factors(cand),') == 2)

# ── through the database ─────────────────────────────────────────────────────
db = sessionmaker(bind=create_engine("sqlite://"))()
Base.metadata.create_all(db.get_bind())
db.add(DeletionProposal(id=1, user_id=1, title="A", service="radarr", media_id="1",
                        score_factors=recs._dump_score_factors(sf)))
db.add(DeletionProposal(id=2, user_id=1, title="B", service="radarr", media_id="2"))
db.add(DeletionProposal(id=3, user_id=1, title="C", service="radarr", media_id="3", score_factors="{not json"))
db.commit()
d1, d2, d3 = (recs._proposal_dict(db.get(DeletionProposal, i)) for i in (1, 2, 3))
check("the API returns the stored breakdown", d1["score_factors"] == json.loads(json.dumps(sf)))
check("a row from before the column has no breakdown", d2["score_factors"] is None)
check("an unreadable breakdown is dropped, not a 500", d3["score_factors"] is None)
sched = (ROOT / "src/services/scheduler.py").read_text(encoding="utf-8")
check("the nightly scan stores the breakdown too", "score_factors=(json.dumps(p[\"score_factors\"])" in sched)
check("the manual analysis stores it", 'score_factors=_dump_score_factors(p.get("score_factors"))' in
      (ROOT / "src/routers/recommendations.py").read_text(encoding="utf-8"))

# ── existing databases gain the column ───────────────────────────────────────
mem = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
conn_mod.engine = mem
with mem.begin() as c:
    c.execute(text("CREATE TABLE deletion_proposals (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT)"))
conn_mod._migrate_columns()
with mem.connect() as c:
    cols = {r[1] for r in c.execute(text("PRAGMA table_info(deletion_proposals)"))}
check("_migrate_columns adds score_factors to an existing table", "score_factors" in cols)
upd = (ROOT / "update_db.py").read_text(encoding="utf-8")
check("update_db.py runs the column migrations, not create_all alone",
      "init_db()" in upd and "create_all(bind=engine)" not in upd)

# ── the card ─────────────────────────────────────────────────────────────────
ui = everything()
card = ui.split("export function _renderDeletionProposals")[1][:6000]
check("the card no longer shows the score as a percentage", "(p.confidence||0)*100" not in card)
check("the card says Cut or Your call", ">Cut</span>" in card and ">Your call</span>" in card)
check("Why? is a disclosure: aria-expanded + aria-controls", 'aria-expanded="false" aria-controls="why-${p.id}"' in ui)
check("toggleWhy is a registered action", re.search(r"\n  toggleWhy,\n", ui) is not None)
check("Recommendations never invents a 70% fit", "rec.confidence||0.7" not in ui)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
