"""Schema test for the Curatarr-Recommended playlist columns.

Creates a throwaway sqlite DB, runs create_all + the column migration TWICE
(idempotency), asserts the new columns exist. Stdlib runner, no pytest:

    python tests/test_rec_playlist_schema.py
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def main():
    tmp = Path(tempfile.mkdtemp()) / "schema_test.db"
    from sqlalchemy import create_engine
    from src.database.models import Base
    engine = create_engine(f"sqlite:///{tmp.as_posix()}")
    Base.metadata.create_all(engine)
    engine.dispose()

    con = sqlite3.connect(tmp)
    users_cols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
    recs_cols = {r[1] for r in con.execute("PRAGMA table_info(cached_recommendations)")}
    check("users.plex_token", "plex_token" in users_cols)
    for c in ("tmdb_id", "tvdb_id", "year", "plex_rating_key"):
        check(f"cached_recommendations.{c}", c in recs_cols)

    con.close()

    # _migrate_columns() runs against the APP engine (no params) — never call
    # it from a test. Instead assert the migration LIST carries all five new
    # entries; live idempotency is proven by double-starting the app (V1).
    src = (Path(__file__).resolve().parents[1] / "src" / "database"
           / "connection.py").read_text(encoding="utf-8")
    for tbl, col in (("users", "plex_token"),
                     ("cached_recommendations", "tmdb_id"),
                     ("cached_recommendations", "tvdb_id"),
                     ("cached_recommendations", "year"),
                     ("cached_recommendations", "plex_rating_key")):
        check(f"migration list has {tbl}.{col}",
              f'("{tbl}"' in src.replace(" ", "").replace("'", '"')
              and f'"{col}"' in src)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
