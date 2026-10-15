"""Adopting a CONTRADICTION principle retires the rule it contradicts.

Found in the 2026-10 vector/memory audit: the novelty check recorded what a
candidate contradicted only as free text ("a few words"), so adopting the
contradicting rule — in the review chat or from the panel — left BOTH rules
active, and the judge was told to "apply" two opposite rules. The verdict now
names the related rule by ID (validated against the listed IDs), and
adoption marks the contradicted rule 'superseded'.

    python tests/test_principle_supersede.py
"""
import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.services.curator_principles as cp  # noqa: E402
from src.config import settings  # noqa: E402
from src.database.models import Base, CuratorPrinciple  # noqa: E402

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
Session = sessionmaker(bind=engine)


@contextmanager
def _session():
    db = Session()
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


cp.get_db_session = _session


async def _no_embed(text):
    return None


cp._embed = _no_embed


def add(text, status="active", user_id=1, novelty=None, related=None):
    with _session() as db:
        p = CuratorPrinciple(user_id=user_id, text=text, status=status,
                             novelty=novelty, related=related)
        db.add(p)
        db.flush()
        return p.id


def get(pid):
    with _session() as db:
        p = db.query(CuratorPrinciple).filter(CuratorPrinciple.id == pid).first()
        return SimpleNamespace(status=p.status, related=p.related,
                               times_reinforced=p.times_reinforced or 0, text=p.text)


# ── the novelty listing names rules by ID ────────────────────────────────────

listing = cp._build_novelty_user(["cand"], [(7, "Keep slow films only with craft.")], "")
check("existing principles are listed with their IDs",
      "- ID 7: Keep slow films only with craft." in listing)
check("plain-text existing rules still render (prototype scripts)",
      "- Keep it." in cp._build_novelty_user(["cand"], ["Keep it."], ""))
check("a valid related_id is stored as '#id: words'",
      cp._related_ref(7, "slow films", {7}) == "#7: slow films")
check("an id the model invented is not trusted",
      cp._related_ref(999, "slow films", {7}) == "slow films")
check("garbage ids and '-' words degrade cleanly",
      cp._related_ref("x", "-", {7}) is None and cp._related_ref(0, "w", {7}) == "w")
check("the stored ref parses back to its id", cp._related_id("#7: slow films") == 7
      and cp._related_id("slow films") is None and cp._related_id(None) is None)

# ── capture: CONTRADICTION / DUPLICATE / NEW with ids ────────────────────────

old_rule = add("The owner keeps every slow film.")
dup_rule = add("The owner values franchise completion.")
settings.PRINCIPLES_ENABLED = True
settings.PRINCIPLE_AUTO_ACTIVATE = False
cp._thread_text = lambda user_id, thread_id: ("USER: a\n\nCURATOR: b", 6)
_sent: dict = {}


async def _fake_curator(system, user, schema, num_predict=800):
    if system == cp._EXTRACT_SYS:
        return {"principles": [
            {"principle": "Slow films stay only when the craft carries them.",
             "basis": "user_established"},
            {"principle": "Franchise completion matters to the owner.",
             "basis": "user_established"},
            {"principle": "Comfort rewatches are protected.", "basis": "converged"}]}
    _sent["novelty"] = user
    return {"results": [
        {"n": 1, "verdict": "CONTRADICTION", "related": "slow film", "related_id": old_rule},
        {"n": 2, "verdict": "DUPLICATE", "related": "zzz no text match", "related_id": dup_rule},
        {"n": 3, "verdict": "NEW", "related": "-", "related_id": 999}]}


cp._curator_json = _fake_curator
stored = asyncio.run(cp.capture_principles_from_thread(1, "t1", "movie"))
by_nov = {s["novelty"]: s for s in stored}
check("the novelty check saw the rules' IDs", f"- ID {old_rule}: " in _sent["novelty"])
contra = by_nov.get("contradiction")
check("a contradiction is stored in shadow with the related rule's id",
      contra and contra["status"] == "shadow"
      and get(contra["id"]).related == f"#{old_rule}: slow film")
check("a duplicate reinforces the rule named by id (text match would have missed)",
      get(dup_rule).times_reinforced == 1)
check("an invented related id is not stored as a reference",
      by_nov.get("new") and not (get(by_nov["new"]["id"]).related or "").startswith("#"))
check("capturing never touches the contradicted rule", get(old_rule).status == "active")

# ── adopting it in the review chat supersedes the old rule ───────────────────


async def _adopt(system, user, schema, num_predict=800):
    return {"decision": "adopt", "revised_text": ""}


cp._curator_json = _adopt
asyncio.run(cp.detect_and_apply_principle_verdict(1, contra["id"], "yes, adopt it", "ok"))
check("the adopted contradiction is active", get(contra["id"]).status == "active")
check("the contradicted rule is superseded, pointing at its successor",
      get(old_rule).status == "superseded"
      and get(old_rule).related == f"superseded by #{contra['id']}")
active = asyncio.run(cp.retrieve_principles(1, category="movie"))
check("only the adopted rule reaches the judge",
      "The owner keeps every slow film." not in active
      and "Slow films stay only when the craft carries them." in active)

# ── what must NOT supersede ──────────────────────────────────────────────────

base = add("Keep documentaries with archival value.")
refine = add("Keep documentaries with archival value, even short ones.", status="shadow",
             novelty="refinement", related=f"#{base}: documentaries")
asyncio.run(cp.detect_and_apply_principle_verdict(1, refine, "adopt", "ok"))
check("adopting a REFINEMENT leaves its base rule active", get(base).status == "active")

legacy_base = add("Never delete concert films.")
legacy = add("Concert films may go when unwatched.", status="shadow",
             novelty="contradiction", related="concert films")
asyncio.run(cp.detect_and_apply_principle_verdict(1, legacy, "adopt", "ok"))
check("a free-text (unresolved) related rule is left alone",
      get(legacy_base).status == "active")

foreign = add("Another user's rule.", user_id=2)
cross = add("Contradicts someone else.", status="shadow", novelty="contradiction",
            related=f"#{foreign}: x")
asyncio.run(cp.detect_and_apply_principle_verdict(1, cross, "adopt", "ok"))
check("never supersedes another user's rule", get(foreign).status == "active")

# ── the panel's Activate button takes the same path ──────────────────────────

from src.routers.recommendations import update_principle  # noqa: E402

panel_base = add("Delete unwatched sequels.")
panel = add("Keep sequels of franchises the owner completes.", status="shadow",
            novelty="contradiction", related=f"#{panel_base}: sequels")
with _session() as db:
    out = asyncio.run(update_principle(panel, "activate", user=SimpleNamespace(id=1), db=db))
check("activating from the panel adopts the rule", out["new_status"] == "active")
check("…and supersedes the rule it contradicts", get(panel_base).status == "superseded")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
