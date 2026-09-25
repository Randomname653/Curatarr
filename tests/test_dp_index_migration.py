"""Pass-90c AUTOINCREMENT rebuild must keep deletion_proposals' indexes.

SQLite index names are global and ALTER TABLE RENAME keeps them on the
renamed-aside table. The rebuild's CREATE INDEX IF NOT EXISTS for
idx_dp_latest_activity was therefore a no-op, DROP TABLE then removed the
index, and idx_dp_user_status was never recreated at all — every migrated
DB ran without both. The repair step recreates them on such DBs.

Runs against an in-memory SQLite DB swapped in for the app engine.

    python tests/test_dp_index_migration.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import src.database.connection as conn_mod

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


WANT = {"idx_dp_user_status", "idx_dp_latest_activity"}

COLS = """
    user_id INTEGER NOT NULL, media_id VARCHAR(64) NOT NULL,
    title VARCHAR(512) NOT NULL, service VARCHAR(32) NOT NULL,
    arr_url VARCHAR(512), reason TEXT, confidence FLOAT, storage_mb FLOAT,
    status VARCHAR(32), user_comment TEXT, created_at DATETIME,
    resolved_at DATETIME, category TEXT, poster_url TEXT, synopsis TEXT,
    genres TEXT, latest_activity_at DATETIME, tvdb_id INTEGER,
    tmdb_id INTEGER, stagnant BOOLEAN
"""


def fresh_engine():
    eng = create_engine("sqlite://", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    conn_mod.engine = eng
    return eng


def indexes(eng, table="deletion_proposals"):
    with eng.connect() as c:
        return {r[0] for r in c.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=:t "
            "AND sql IS NOT NULL"), {"t": table})}


def ddl(eng):
    with eng.connect() as c:
        return c.execute(text("SELECT sql FROM sqlite_master WHERE type='table' "
                              "AND name='deletion_proposals'")).scalar() or ""


# ── a pre-90c DB: plain INTEGER PRIMARY KEY, both indexes present ───────────
eng = fresh_engine()
with eng.begin() as c:
    c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
    c.execute(text(f"CREATE TABLE deletion_proposals (id INTEGER NOT NULL PRIMARY KEY, {COLS})"))
    c.execute(text("CREATE INDEX idx_dp_user_status ON deletion_proposals (user_id, status)"))
    c.execute(text("CREATE INDEX idx_dp_latest_activity ON deletion_proposals (latest_activity_at)"))
    c.execute(text("INSERT INTO users (id) VALUES (1)"))
    c.execute(text("INSERT INTO deletion_proposals (id, user_id, media_id, title, service, status) "
                   "VALUES (41, 1, 'm', 'Kept Title', 'radarr', 'pending')"))
check("precondition: legacy table has both indexes", indexes(eng) == WANT)

conn_mod._migrate_deletion_proposals_autoincrement()
check("migration converts the table to AUTOINCREMENT", "AUTOINCREMENT" in ddl(eng).upper())
with eng.connect() as c:
    kept = c.execute(text("SELECT id, title FROM deletion_proposals")).all()
    old_left = c.execute(text("SELECT 1 FROM sqlite_master WHERE name="
                              "'deletion_proposals_old_pre90c'")).first()
check("rows survive with their ids", kept == [(41, "Kept Title")])
check("renamed-aside table is gone", old_left is None)
check("both indexes exist on the rebuilt table (the old rebuild lost both)",
      indexes(eng) == WANT)
with eng.connect() as c:
    plan = " ".join(str(r) for r in c.execute(text(
        "EXPLAIN QUERY PLAN SELECT * FROM deletion_proposals "
        "WHERE user_id = 1 AND status = 'pending'")))
check("the user/status filter uses its index again", "idx_dp_user_status" in plan)

# ── an already-migrated DB that lost them: the repair step restores ─────────
eng = fresh_engine()
with eng.begin() as c:
    c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
    c.execute(text(f"CREATE TABLE deletion_proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, {COLS})"))
check("precondition: migrated-back-then DB has no indexes", indexes(eng) == set())
conn_mod._migrate_deletion_proposals_autoincrement()   # already migrated -> no-op
check("the migration itself skips an already-migrated table", indexes(eng) == set())
conn_mod._ensure_deletion_proposal_indexes()
check("repair recreates both missing indexes", indexes(eng) == WANT)
conn_mod._ensure_deletion_proposal_indexes()
check("repair is idempotent", indexes(eng) == WANT)

# ── fresh install before create_all: nothing to repair, nothing raised ──────
eng = fresh_engine()
conn_mod._ensure_deletion_proposal_indexes()
check("repair on a DB without the table is a no-op", indexes(eng) == set())

check("init_db runs the repair after the migration",
      "_ensure_deletion_proposal_indexes()" in
      (Path(__file__).resolve().parents[1] / "src/database/connection.py")
      .read_text(encoding="utf-8").split("def init_db", 1)[1])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
