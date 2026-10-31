"""Two leftovers of the second sweep.

  * the Spotify genre top-up (Last.fm, phase 2) normalised every play still
    without genres on the event loop before batching — up to 200k plays,
    ~3 s in which no request was answered; the grouping now runs on the
    read worker together with the query
  * the taste vector dropped every failed embedding without a word, so a
    bug in embed_entry froze it at its old value behind "Got 0/N" at INFO;
    failures are now logged, and above a fifth the stored vector is kept
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

from _checks import check  # noqa: E402


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

# ── 2. taste vector: failed embeddings are counted, not dropped ─────────────
print("taste vector with failing embeddings")
import logging  # noqa: E402

import src.cache.metadata_cache as mc_mod  # noqa: E402
import src.services.taste_engine as te  # noqa: E402
from src.database.models import EnrichmentStatus  # noqa: E402

HOUR = 3_600_000
NOW = datetime.utcnow()
with conn_mod.get_db_session() as db:
    for i, series in enumerate(("Sitcom", "Miniseries", "Western", "Drama Show")):
        for ep in range(2):
            db.add(WatchHistoryEntry(user_id=1, plex_user_id="u1", plex_item_id=f"s{i}-{ep}",
                                     title=f"Ep {ep}", series_title=series, media_type="show",
                                     genres="Comedy", duration_ms=HOUR, view_offset_ms=HOUR,
                                     completed=True, viewed_at=NOW))
        db.add(EnrichmentStatus(plex_rating_key=f"sonarr:{i}", title=series,
                                media_category="show", enriched=True, enriched_at=NOW))


class _FakeCache:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def close(self): pass
    def set_cache(self, *a, **k): pass

    def get_cache(self, key):
        if key.startswith("enriched:"):
            return {"response": {"title": key, "genres": ["Comedy"], "embedding_text": "X"}}
        return None

    def get_cache_many(self, keys):
        return {k: self.get_cache(k) for k in keys if self.get_cache(k)}


def _item_vector(entry, profile):
    # a bug for every series but the sitcom: 3 of 4 (or 6 of 8) fail
    if entry.get("series_title") != "Sitcom":
        raise RuntimeError("embed_entry bug")
    return [1.0, 0.0, 0.0, 0.0]


class _Warnings(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)


_w = _Warnings()
logging.getLogger("src.services.taste_engine").addHandler(_w)
_saved_te = (te.MetadataCache, mc_mod.MetadataCache, te._chroma_item_vector)
te.MetadataCache = mc_mod.MetadataCache = _FakeCache
te._chroma_item_vector = _item_vector
try:
    res = asyncio.run(te.compute_taste_vector_for_user(1, category="show"))
finally:
    te.MetadataCache, mc_mod.MetadataCache, te._chroma_item_vector = _saved_te
    logging.getLogger("src.services.taste_engine").removeHandler(_w)
check(f"most embeddings failing leaves the stored vector alone (got {sorted(res)[:4]})",
      res == {})
tb = [r for r in _w.records if r.exc_info]
check(f"the failures are logged with a traceback, at most 3 ({len(tb)})", 1 <= len(tb) <= 3)
check("the run says how many failed",
      any("embeddings failed" in r.getMessage() for r in _w.records))

# ── 3. settings view: unreadable judgements and "no verdict" ────────────────
print("model-update review in the settings view")
js = (ROOT / "frontend" / "js" / "settings.js").read_text(encoding="utf-8")
rec = js[js.index("const MU_REC"):][:400]
check('"no verdict" has its own label', "'no verdict'" in rec)
ev = js[js.index("function _muEval"):][:3000]
check("the title tally shows unreadable judgements", "t.unreadable" in ev)
check("the chat tally shows unreadable judgements", "c.unreadable" in ev)

check.done()
