"""Deletion scoring: the household's taste, not only the owner's.

2026-10-21: the taste term measured distance from the OWNER's history alone,
so a series another household member would enjoy (and has not watched yet)
scored as "far from your watch history". A title close to a member's taste
now loses part of those points - scaled by the size of that member's profile,
so a thin profile barely moves anything - and a strong fit reaches the judge
as a household argument. An actual watch stays the only floor.

    python tests/test_household_taste.py
"""
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.services.recommendations_engine as eng  # noqa: E402
from src.database.models import Base, EncryptedTasteVector, User  # noqa: E402

from _checks import check  # noqa: E402


# ── the Why? line ────────────────────────────────────────────────────────────
f = eng.deletion_score_factors(mismatch=1.0, taste_src="embedding", size_pts=0, size_gb=1,
                               size_ratio=None, rating=None, rating_swing=0, user_rating=None,
                               user_rating_swing=0, feedback_swing=0, drop_penalty=0, play_prot=0,
                               household_taste_pts=12.3, household_name="member")
hh = [x for x in f if x["key"] == "household_taste"]
check("Why? names the member whose taste pulled the score down",
      hh == [{"key": "household_taste", "points": -12.3, "label": "Close to member's taste"}])
none = eng.deletion_score_factors(mismatch=1.0, taste_src="embedding", size_pts=0, size_gb=1,
                                  size_ratio=None, rating=None, rating_swing=0, user_rating=None,
                                  user_rating_swing=0, feedback_swing=0, drop_penalty=0, play_prot=0)
check("...and stays silent without household taste", not any(x["key"] == "household_taste" for x in none))

# ── the members' profiles ────────────────────────────────────────────────────
engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)


@contextmanager
def fake_session():
    db = Session()
    try:
        yield db
        db.commit()
    finally:
        db.close()


def blob(vec, items=10, **extra):
    b = {"embedding": vec, "cal_p10": 0.0, "cal_p90": 0.5, "embedding_items": items}
    b.update(extra)
    return json.dumps(b)


A, B = [1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]
with fake_session() as db:
    users = [User(id=1, plex_user_id="1", plex_username="owner", is_admin=True, is_active=True),
             User(id=2, plex_user_id="2", plex_username="member", is_admin=False, is_active=True),
             User(id=3, plex_user_id="3", plex_username="gone", is_admin=False, is_active=False),
             User(id=4, plex_user_id="4", plex_username="old", is_admin=False, is_active=True),
             User(id=5, plex_user_id="5", plex_username="flat", is_admin=False, is_active=True)]
    db.add_all(users)
    db.add_all([
        EncryptedTasteVector(user_id=1, media_category="show", salt="", encrypted_blob=blob(B)),
        EncryptedTasteVector(user_id=2, media_category="show", salt="", encrypted_blob=blob(A, items=5)),
        EncryptedTasteVector(user_id=3, media_category="show", salt="", encrypted_blob=blob(A)),
        EncryptedTasteVector(user_id=4, media_category="show", salt="", encrypted_blob=json.dumps({"version": 1})),
        EncryptedTasteVector(user_id=5, media_category="show", salt="",
                             encrypted_blob=blob(A, cal_p10=0.3, cal_p90=0.3)),
        EncryptedTasteVector(user_id=2, media_category="anime", salt="", encrypted_blob=blob(A)),
    ])

_real = eng.get_db_session
eng.get_db_session = fake_session
try:
    house = eng._load_household_tastes(1, "show")
finally:
    eng.get_db_session = _real
check("only other ACTIVE users with a calibrated, current profile count",
      [h["name"] for h in house] == ["member"])
check("...for this category only, with its size", house and house[0]["items"] == 5)

fits = dict((n, (fit, w)) for n, fit, w in eng._household_fits(np.array(A) * 13, house))
check("a title on the member's taste fits fully", round(fits["member"][0], 3) == 1.0)
check("the damping weight scales with the profile (5 of 20 titles -> 0.125)",
      round(fits["member"][1], 3) == round(eng.HOUSEHOLD_TASTE_MAX_DAMPING * 5 / 20, 3))
far = dict((n, fit) for n, fit, _ in eng._household_fits(np.array(B), house))
check("a title far from the member's taste fits not at all", far["member"] == 0.0)

# ── wired into the score and the judge ───────────────────────────────────────
src = (ROOT / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
check("the household term is subtracted from the score", "drop_penalty - play_prot - hh_pts" in src)
check("...and recorded for the Why? panel", "household_taste_pts=hh_pts, household_name=hh_name" in src)
check("a strong fit is handed to the judge on the item",
      'p["item"]["_household_taste"] = {"name": _sn, "fit": round(_sfit, 2)}' in src)
pil = (ROOT / "src/services/pillars.py").read_text(encoding="utf-8")
check("the judge reads it as a household reason weaker than a watch",
      "has NOT watched it, but their" in pil and 'flags["household_taste_fit"]' in pil
      and 'flags["other_user_engaged"]' in pil.split('item.get("_household_taste")')[0])
check("a taste fit is never the household floor",
      "household_taste" not in pil.split("def _household_claim")[1].split("\ndef ")[0])

check.done()
