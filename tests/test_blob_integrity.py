"""Tests for taste-blob integrity (evaluation package 1, commit C):
optimistic CAS writes, v0 decrypt fix, version gates, dead schema removal.

    python tests/test_blob_integrity.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database.models import Base, EncryptedTasteVector
from src.services.taste_vectors import cas_update_blob

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── CAS semantics against a real (in-memory) SQLite ──────────────────────────

engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()

db.add(EncryptedTasteVector(user_id=1, media_category="movie",
                            salt="unencrypted",
                            encrypted_blob=json.dumps({"version": 0, "a": 1})))
db.commit()
etv_id = db.query(EncryptedTasteVector).first().id

check("CAS write succeeds when rev matches (missing rev reads as 0)",
      cas_update_blob(db, etv_id, 0, {"version": 0, "a": 2}))
db.commit()
blob = json.loads(db.query(EncryptedTasteVector).get(etv_id).encrypted_blob)
check("rev incremented to 1", blob.get("rev") == 1 and blob["a"] == 2)

check("stale writer (still thinks rev=0) is rejected",
      not cas_update_blob(db, etv_id, 0, {"version": 0, "a": 99}))
db.rollback()
blob = json.loads(db.query(EncryptedTasteVector).get(etv_id).encrypted_blob)
check("stale write left no trace", blob["a"] == 2)

check("writer with the current rev succeeds",
      cas_update_blob(db, etv_id, 1, {"version": 0, "a": 3}))
db.commit()
blob = json.loads(db.query(EncryptedTasteVector).get(etv_id).encrypted_blob)
check("second write lands, rev=2", blob["a"] == 3 and blob["rev"] == 2)


# The decrypt-path checks that lived here tested the cipher that was
# removed as unshippable theater (taste_vectors.py's docstring has the
# reasoning) — the blob is, and always was, plain JSON.

# ── wiring asserts ───────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
tv = (root / "src/services/taste_vectors.py").read_text(encoding="utf-8")
check("dead empty_taste_vector removed", "def empty_taste_vector" not in tv)
check("dead detect_binges_from_history removed",
      "def detect_binges_from_history" not in tv)

em = (root / "src/services/episodic_memory.py").read_text(encoding="utf-8")
check("feedback merge uses CAS with one retry",
      "for attempt in (1, 2):" in em and "cas_update_blob" in em)

te = (root / "src/services/taste_engine.py").read_text(encoding="utf-8")
check("recompute writer uses CAS + takes merger's fresh feedback on miss",
      te.count("cas_update_blob") >= 2 and "Commit PER category" in te)

re_src = (root / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
check("v1 version gates degrade loudly in both engine readers",
      re_src.count('_blob.get("version") == 1') == 2)
pl = (root / "src/services/pillars.py").read_text(encoding="utf-8")
check("pillars reader gated too", 'blob.get("version") == 1' in pl)

models = (root / "src/database/models.py").read_text(encoding="utf-8")
check("misnamed taste columns documented", "COLUMN NAMES LIE" in models)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
