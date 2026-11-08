"""scripts/wrapup_read.py reads a database copy taken before a merge: it
brings the copy to this checkout's schema (the app's own start-up
migration) and prints the member's Wrap-Up texts in the language asked.
It stored nothing of the Wrap-Up before, and stores nothing now. Every title
is invented.

    python tests/test_wrapup_read_script.py
"""
from _harness import TMP, check  # noqa: I001 - first: temp stores

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COPY = TMP / "copy-before-merge.db"
env = dict(os.environ, DATABASE_URL=f"sqlite:///{COPY.as_posix()}", PYTHONIOENCODING="utf-8")

seed = """
from datetime import datetime, timedelta
from src.database.connection import init_db, get_db_session
from src.database.models import User, WatchHistoryEntry
init_db()
with get_db_session() as db:
    u = User(plex_user_id="rs-1", plex_username="Reader", timezone="Europe/Berlin")
    db.add(u); db.commit()
    for i in range(8):
        db.add(WatchHistoryEntry(user_id=u.id, plex_user_id="rs-1", plex_item_id=f"k{i}", title=f"Episode {i}",
                                 series_title="Starfall Archive", media_type="anime", season=1, episode=i + 1,
                                 duration_ms=3_600_000, view_offset_ms=3_600_000, completed=True,
                                 viewed_at=datetime(2026, 10, 2, 19) + timedelta(days=3 * i)))
    db.commit()
"""
r = subprocess.run([sys.executable, "-"], input=seed, cwd=ROOT, env=env, capture_output=True, text=True,
                   encoding="utf-8")
check("a copy can be seeded", r.returncode == 0, r.stderr[-800:])
# the copy is from before a merge: the lift columns are not there yet
con = sqlite3.connect(COPY)
cols = [c[1] for c in con.execute("PRAGMA table_info(cached_recommendations)")]
keep = [c for c in cols if c not in ("lifted_at", "lifted_by", "lift_source")]
con.execute(f"CREATE TABLE cr_old AS SELECT {', '.join(keep)} FROM cached_recommendations")
con.execute("DROP TABLE cached_recommendations")
con.execute("ALTER TABLE cr_old RENAME TO cached_recommendations")
con.commit()
before = con.execute("SELECT COUNT(*) FROM wrapups").fetchone()[0]
con.close()

out = subprocess.run([sys.executable, "scripts/wrapup_read.py", "--user", "Reader", "--period", "month:2026-10",
                      "--lang", "de"], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")
check("an older copy is read: migrated first, then the texts", out.returncode == 0 and "# Wrap-Up read: user" in out.stdout,
      (out.returncode, out.stderr[-1200:]))
check("...the fallback texts in the language asked", "fallback: **" in out.stdout and "Stunden" in out.stdout,
      out.stdout[-800:])
con = sqlite3.connect(COPY)
cols_after = {c[1] for c in con.execute("PRAGMA table_info(cached_recommendations)")}
after = con.execute("SELECT COUNT(*) FROM wrapups").fetchone()[0]
con.close()
check("the one write is the schema: the new columns are there, no wrap-up was stored",
      {"lifted_at", "lifted_by", "lift_source"} <= cols_after and before == after == 0, (cols_after, before, after))

check.done()
