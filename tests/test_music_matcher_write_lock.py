"""The Spotify→Plex match must never hold SQLite's write lock across an await.

The old loop flushed every 500 plays into one long session — the flush opened
a transaction and took the write lock — and then yielded with
``await asyncio.sleep(0)``. A coroutine that wrote synchronously during that
yield (the game watcher's set_state, every 30 s) busy-waited on the very
event-loop thread the matcher needed to resume and commit: a whole-app
freeze for the busy_timeout, then "database is locked".

Runs match_spotify_to_plex against a temp file DB with a fake Plex, while a
concurrent coroutine writes synchronously on the loop with a short timeout.

    python tests/test_music_matcher_write_lock.py
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_tmp = Path(tempfile.mkdtemp())
DB_FILE = (_tmp / "curatarr.db").resolve()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"

from src.database.connection import get_db_session, init_db  # noqa: E402
from src.database.models import LibraryConfig, WatchHistoryEntry  # noqa: E402
from src.services import music_matcher as mm  # noqa: E402
from src.services.app_state import get_state, set_state  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


check("test DB is the temp file, not the real one",
      DB_FILE.as_posix() in os.environ["DATABASE_URL"])

init_db()
N_PLAYS = 60
with get_db_session() as db:
    db.add(LibraryConfig(plex_section_key="5", plex_section_title="Music",
                         plex_section_type="artist", media_category="music"))
    for i in range(N_PLAYS):
        db.add(WatchHistoryEntry(
            user_id=1, plex_user_id="1", plex_item_id=f"spotify:{i}",
            title=f"Track {i}", series_title=f"Artist {i}", media_type="music",
            source="spotify", viewed_at=datetime(2026, 1, 1),
            genres="kept" if i == 0 else None))

# Plex knows the even-numbered tracks.
TRACKS = [{"ratingKey": 1000 + i, "title": f"Track {i}",
           "grandparentTitle": f"Artist {i}", "Genre": [{"tag": "rock"}]}
          for i in range(0, N_PLAYS, 2)]


class FakeResp:
    status_code = 200

    def json(self):
        return {"MediaContainer": {"Metadata": TRACKS}}


class FakeClient:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *a, **kw):
        return FakeResp()


mm.httpx = SimpleNamespace(AsyncClient=FakeClient)
mm.settings = SimpleNamespace(effective_plex_url="http://plex", effective_plex_token="t",
                              PLEX_CLIENT_ID="test")
mm._MATCH_FLUSH = 5          # many chunks -> many chances to interleave


async def loop_writer(stop: asyncio.Event, waits: list, errors: list):
    """A synchronous write ON the event loop, like set_state in the watcher."""
    while not stop.is_set():
        con = sqlite3.connect(DB_FILE, timeout=2)
        t0 = time.monotonic()
        try:
            con.execute("INSERT INTO app_state (key, value, updated_at) "
                        "VALUES ('game_active', '0', 'x') ON CONFLICT(key) "
                        "DO UPDATE SET value = excluded.value")
            con.commit()
        except sqlite3.OperationalError as e:
            errors.append(str(e))
        finally:
            waits.append(time.monotonic() - t0)
            con.close()
        await asyncio.sleep(0)


async def main():
    stop, waits, errors = asyncio.Event(), [], []
    writer = asyncio.create_task(loop_writer(stop, waits, errors))
    await asyncio.sleep(0)
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(mm.match_spotify_to_plex(user_id=1), timeout=30)
    finally:
        stop.set()
        await writer
    return result, time.monotonic() - t0, waits, errors


result, elapsed, waits, errors = asyncio.run(main())

check("matcher reports every even play matched", result["matched"] == N_PLAYS // 2)
check("and every odd play unmatched", result["unmatched"] == N_PLAYS // 2)
check("concurrent loop-thread writer never hit 'database is locked'", not errors)
check("concurrent loop-thread writer never waited on the lock (< 0.5 s)",
      max(waits, default=0) < 0.5)
check("the run took no busy-timeout stall (< 2 s)", elapsed < 2)

with get_db_session() as db:
    rows = {r.id: (r.plex_item_id, r.genres) for r in db.query(
        WatchHistoryEntry.id, WatchHistoryEntry.plex_item_id, WatchHistoryEntry.genres)}
ids = sorted(rows)
check("matched plays carry the Plex ratingKey",
      all(rows[ids[i]][0] == str(1000 + i) for i in range(0, N_PLAYS, 2)))
check("unmatched plays keep their spotify id",
      all(rows[ids[i]][0] == f"spotify:{i}" for i in range(1, N_PLAYS, 2)))
check("existing genres are kept, missing ones filled from Plex",
      rows[ids[0]][1] == "kept" and rows[ids[2]][1] == "rock")
check("cursor advanced to the last play after a complete pass",
      get_state("music_match_cursor:1") == str(ids[-1]))

# Stop requested after the first chunk: that chunk stays committed, the rest
# is untouched, and the cursor does not move (the window is retried).
set_state("music_match_cursor:1", "0")
with get_db_session() as db:
    for n, e in enumerate(db.query(WatchHistoryEntry).order_by(WatchHistoryEntry.id)):
        e.plex_item_id = f"spotify:{n}"
calls = {"n": 0}


def stop_after_first_chunk():
    calls["n"] += 1          # call 1: the section fetch, call 2: chunk 1
    return calls["n"] > 2


mm._stop_requested = stop_after_first_chunk
stopped = asyncio.run(mm.match_spotify_to_plex(user_id=1))
with get_db_session() as db:
    done = db.query(WatchHistoryEntry).filter(
        ~WatchHistoryEntry.plex_item_id.like("spotify%")).count()
check("a stop between chunks keeps the first chunk's matches (3 of 5)",
      stopped["matched"] == 3 and done == 3)
check("and does not advance the cursor", get_state("music_match_cursor:1") == "0")

check("source: no flush-then-yield inside match_spotify_to_plex",
      "db.flush()" not in (ROOT / "src/services/music_matcher.py").read_text(encoding="utf-8")
      .split("async def match_spotify_to_plex", 1)[1].split("\nasync def ", 1)[0])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
