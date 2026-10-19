"""Foreign keys are enforced, after a boot-time repair (connection.py).

The models declare 18 foreign keys, all to users.id, but SQLite enforces
none of them until ``PRAGMA foreign_keys=ON``, which was never set. So
a row could name a user that no longer exists.

  * _repair_foreign_keys: an orphaned nullable reference is set to NULL;
    an orphaned row that needs its user is kept in fk_orphans as JSON and
    removed; valid rows are untouched; a clean database is left alone
  * every connection of both engines enforces the keys afterwards
  * the deletion_proposals rebuild runs with the keys off for its copy,
    checks them before committing, and leaves its connection enforcing
  * init_db repairs before any migration copies a table

Runs against a temp file database (DATABASE_URL set before the app is
imported).

    python tests/test_foreign_keys.py
"""
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
_DB = (_TMP / "curatarr.db").resolve()
os.environ["DATABASE_URL"] = f"sqlite:///{_DB.as_posix()}"

from sqlalchemy.exc import IntegrityError  # noqa: E402

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import Base, LibraryConfig, User, WatchHistoryEntry  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


Base.metadata.create_all(conn_mod.engine)

# The unenforced past: rows written through a connection with the keys off.
raw = sqlite3.connect(str(_DB))
raw.execute("PRAGMA foreign_keys=OFF")
raw.execute("INSERT INTO users (id, plex_user_id, plex_username) VALUES (1, 'u1', 'owner')")
now = datetime(2026, 10, 1).isoformat()
for uid, title in ((1, "kept"), (99, "orphan")):
    raw.execute("INSERT INTO watch_history (user_id, plex_user_id, plex_item_id, title, viewed_at) "
                "VALUES (?, ?, ?, ?, ?)", (uid, str(uid), f"k{uid}", title, now))
raw.execute("INSERT INTO library_config (plex_section_key, plex_section_title, plex_section_type, "
            "media_category, configured_by) VALUES ('1', 'Films', 'movie', 'movie', 99)")
raw.commit()
raw.close()

# ── 1. the repair ───────────────────────────────────────────────────────────
print("repair")
repaired = conn_mod._repair_foreign_keys()
con = sqlite3.connect(str(_DB))
titles = [r[0] for r in con.execute("SELECT title FROM watch_history ORDER BY id")]
orphans = con.execute("SELECT table_name, row_json, reason FROM fk_orphans").fetchall()
cfg_by = con.execute("SELECT configured_by FROM library_config").fetchone()[0]
left = con.execute("PRAGMA foreign_key_check").fetchall()
con.close()
check("the orphaned watch-history row is removed, the valid one kept", titles == ["kept"])
check("... and kept in fk_orphans as JSON with the reason",
      len(orphans) == 1 and orphans[0][0] == "watch_history"
      and json.loads(orphans[0][1])["title"] == "orphan" and "user_id -> users" in orphans[0][2])
check("an orphaned nullable reference (library_config.configured_by) is set to NULL",
      cfg_by is None)
check("nothing violates a foreign key afterwards", left == [])
check("the repair reports what it changed per table",
      repaired == {"watch_history": 1, "library_config": 1})
check("a clean database is left alone (no write)", conn_mod._repair_foreign_keys() == {})

# ── 2. enforcement ──────────────────────────────────────────────────────────
print("enforcement")
with conn_mod.engine.connect() as c:
    on_main = c.exec_driver_sql("PRAGMA foreign_keys").scalar()
with conn_mod.get_write_engine().connect() as c:
    on_write = c.exec_driver_sql("PRAGMA foreign_keys").scalar()
check("both engines enforce the keys on every connection", on_main == 1 and on_write == 1)


def _insert_orphan(session_factory):
    try:
        with session_factory() as db:
            db.add(WatchHistoryEntry(user_id=1234, plex_user_id="x", plex_item_id="x",
                                     title="ghost", viewed_at=datetime(2026, 10, 19)))
        return False
    except IntegrityError:
        return True


check("a row for a user that does not exist is refused (read engine)",
      _insert_orphan(conn_mod.get_db_session))
check("... and through write_session", _insert_orphan(conn_mod.write_session))
with conn_mod.get_db_session() as db:
    db.add(LibraryConfig(plex_section_key="2", plex_section_title="Shows",
                         plex_section_type="show", media_category="show", configured_by=1))
with conn_mod.get_db_session() as db:
    n_cfg = db.query(LibraryConfig).count()
check("a row for an existing user is written as before", n_cfg == 2)

# ── 3. the deletion_proposals rebuild ───────────────────────────────────────
print("table rebuild")
raw = sqlite3.connect(str(_DB))
raw.execute("DROP TABLE deletion_proposals")
raw.execute("""CREATE TABLE deletion_proposals (
    id INTEGER NOT NULL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
    media_id VARCHAR(64), title VARCHAR(512), service VARCHAR(16), status VARCHAR(16))""")
raw.execute("INSERT INTO deletion_proposals (id, user_id, media_id, title, service, status) "
            "VALUES (5, 1, 'm', 'A film', 'radarr', 'pending')")
raw.commit()
raw.close()
conn_mod.engine.dispose()
conn_mod._migrate_deletion_proposals_autoincrement()
con = sqlite3.connect(str(_DB))
ddl = con.execute("SELECT sql FROM sqlite_master WHERE name='deletion_proposals'").fetchone()[0]
row = con.execute("SELECT id, title FROM deletion_proposals").fetchall()
con.close()
check("the rebuild completes with the keys enforced elsewhere",
      "AUTOINCREMENT" in ddl.upper() and row == [(5, "A film")])
with conn_mod.engine.connect() as c:
    still_on = c.exec_driver_sql("PRAGMA foreign_keys").scalar()
check("its pooled connection enforces the keys again afterwards", still_on == 1)

src = (ROOT / "src" / "database" / "connection.py").read_text(encoding="utf-8")
init = src[src.index("def init_db():"):]
check("init_db repairs before any migration copies a table",
      init.index("_repair_foreign_keys()") < init.index("_migrate_deletion_proposals_autoincrement()"))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
