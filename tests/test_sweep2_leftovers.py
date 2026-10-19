"""Two leftovers of the second sweep.

  * the Spotify genre top-up (Last.fm, phase 2) normalised every play still
    without genres on the event loop before batching — up to 200k plays,
    ~3 s in which no request was answered; the grouping now runs on the
    read worker together with the query
  * the model-update review counts unreadable judgements and gives "no
    verdict" when the judge did not answer, but the settings view showed
    neither: a dead judge read like a draw, and "no verdict" had no label

Each check fails on the code before this change.

    python tests/test_sweep2_leftovers.py
"""
import asyncio
import os
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import Base, User, WatchHistoryEntry  # noqa: E402

Base.metadata.create_all(conn_mod.engine)

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── 1. Spotify genre top-up: grouping off the loop ──────────────────────────
print("Spotify genre top-up")
import src.services.music_matcher as mm  # noqa: E402

with conn_mod.get_db_session() as db:
    db.add(User(id=1, plex_user_id="u1", plex_username="a"))
    db.flush()
    for i in range(6):
        db.add(WatchHistoryEntry(user_id=1, plex_user_id="u1", plex_item_id=f"spotify:{i}",
                                 title=f"Song {i % 3}", series_title="Artist",
                                 media_type="music", source="spotify",
                                 viewed_at=datetime(2026, 10, 1)))

_real_norm = mm._normalize
_threads: list[str] = []


def _spy(s):
    _threads.append(threading.current_thread().name)
    return _real_norm(s)


mm._normalize = _spy
mm.settings.LASTFM_API_KEY = "test-key"
try:
    # batch=0: nothing is sent to Last.fm, only the collection runs
    res = asyncio.run(mm.enrich_music_genres_lastfm(1, batch=0))
finally:
    mm._normalize = _real_norm
main = threading.main_thread().name
check(f"the plays were normalised ({len(_threads)} calls)", len(_threads) >= 6)
check("none of it ran on the event loop's thread", main not in _threads)
check(f"an empty batch still returns the counts ({res})",
      res.get("tracks_queried") == 0 and "error" not in res)

# ── 2. settings view: unreadable judgements and "no verdict" ────────────────
print("model-update review in the settings view")
js = (ROOT / "frontend" / "js" / "settings.js").read_text(encoding="utf-8")
rec = js[js.index("const MU_REC"):][:400]
check('"no verdict" has its own label', "'no verdict'" in rec)
ev = js[js.index("function _muEval"):][:3000]
check("the title tally shows unreadable judgements", "t.unreadable" in ev)
check("the chat tally shows unreadable judgements", "c.unreadable" in ev)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
