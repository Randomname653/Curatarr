"""Pass-90c rebuild must never drop a column the model declares.

The rebuild once used a hard-coded column list that predated score_factors,
soulsync_ban, returned_at and returned_info. The model also lacked
``sqlite_autoincrement``, so create_all built a plain INTEGER PRIMARY KEY on
a fresh install and the rebuild ran on its FIRST boot — dropping those four
columns, after _migrate_columns had already run. Every DeletionProposal query
then failed with "no such column" until the next restart.

Runs against an in-memory SQLite DB swapped in for the app engine.

    python tests/test_dp_rebuild_columns.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from _checks import memory_engine  # noqa: E402
import src.database.connection as conn_mod
from src.database.models import Base, DeletionProposal

from _checks import check  # noqa: E402


MODEL_COLS = {c.name for c in DeletionProposal.__table__.columns}


def fresh_engine():
    eng = memory_engine()
    conn_mod.engine = eng
    return eng


def columns(eng):
    with eng.connect() as c:
        return {r[1] for r in c.execute(text("PRAGMA table_info(deletion_proposals)"))}


def ddl(eng):
    with eng.connect() as c:
        return c.execute(text("SELECT sql FROM sqlite_master WHERE type='table' "
                              "AND name='deletion_proposals'")).scalar() or ""


def orm_query_ok(eng):
    db = sessionmaker(bind=eng)()
    try:
        db.query(DeletionProposal).first()
        return True
    except Exception as e:
        print(f"        ORM query failed: {e}")
        return False
    finally:
        db.close()


# ── fresh install: create_all already emits AUTOINCREMENT ────────────────────
eng = fresh_engine()
Base.metadata.create_all(bind=eng)
check("create_all builds deletion_proposals with AUTOINCREMENT",
      "AUTOINCREMENT" in ddl(eng).upper())
conn_mod._migrate_columns()
conn_mod._migrate_deletion_proposals_autoincrement()
check("first boot keeps every model column", columns(eng) == MODEL_COLS)
check("first boot: DeletionProposal queries work", orm_query_ok(eng))

# ── legacy pre-90c DB that predates the late columns ─────────────────────────
eng = fresh_engine()
with eng.begin() as c:
    c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
    c.execute(text(
        "CREATE TABLE deletion_proposals (id INTEGER NOT NULL PRIMARY KEY, "
        "user_id INTEGER NOT NULL, media_id VARCHAR(64) NOT NULL, "
        "title VARCHAR(512) NOT NULL, service VARCHAR(32) NOT NULL, "
        "status VARCHAR(32), score_factors TEXT)"))
    c.execute(text("INSERT INTO users (id) VALUES (1)"))
    c.execute(text(
        "INSERT INTO deletion_proposals (id, user_id, media_id, title, service, "
        "status, score_factors) VALUES "
        "(7, 1, 'm7', 'Seven', 'radarr', 'pending', '{\"size\": 1}'), "
        "(41, 1, 'm41', 'Forty-One', 'sonarr', 'deleted', NULL)"))
conn_mod._migrate_deletion_proposals_autoincrement()
check("legacy rebuild converts to AUTOINCREMENT", "AUTOINCREMENT" in ddl(eng).upper())
check("legacy rebuild ends with every model column", columns(eng) == MODEL_COLS)
with eng.connect() as c:
    rows = c.execute(text("SELECT id, title, score_factors FROM deletion_proposals "
                          "ORDER BY id")).all()
    aside = c.execute(text("SELECT 1 FROM sqlite_master WHERE name LIKE "
                           "'deletion_proposals_old%'")).first()
    seq = c.execute(text("SELECT seq FROM sqlite_sequence "
                         "WHERE name='deletion_proposals'")).all()
check("rows survive with ids and values of shared columns",
      rows == [(7, "Seven", '{"size": 1}'), (41, "Forty-One", None)])
check("renamed-aside table is gone", aside is None)
check("sqlite_sequence holds exactly one row at the max id", seq == [(41,)])
with eng.begin() as c:
    c.execute(text("DELETE FROM deletion_proposals WHERE id = 41"))
    c.execute(text("INSERT INTO deletion_proposals (user_id, media_id, title, service) "
                   "VALUES (1, 'm', 'New', 'radarr')"))
    new_id = c.execute(text("SELECT id FROM deletion_proposals WHERE title='New'")).scalar()
check("a freed id is never reused (next id continues past 41)", new_id == 42)
check("legacy rebuild: DeletionProposal queries work", orm_query_ok(eng))

# ── a failure mid-rebuild rolls back to the untouched old table ──────────────
eng = fresh_engine()
with eng.begin() as c:
    c.execute(text("CREATE TABLE deletion_proposals (id INTEGER NOT NULL PRIMARY KEY, "
                   "title VARCHAR(512))"))   # lacks NOT NULL model columns -> copy fails
    c.execute(text("INSERT INTO deletion_proposals (id, title) VALUES (3, 'x')"))
conn_mod._migrate_deletion_proposals_autoincrement()
with eng.connect() as c:
    rows = c.execute(text("SELECT id, title FROM deletion_proposals")).all()
check("failed rebuild leaves the original table and rows", rows == [(3, "x")]
      and "AUTOINCREMENT" not in ddl(eng).upper())

check.done()
