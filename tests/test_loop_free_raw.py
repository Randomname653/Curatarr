"""The app's other SQLite files stay off the event loop too.

PR #171 kept the main database off the loop; the enrichment cache,
plex_music.db and the offline indexes were opened with plain
``sqlite3.connect`` and invisible to the guard. They now open through
``loop_guard.sqlite_connect``, and the paths it found are moved:

  * sync_lyrics: every statement on a worker, and no write lock on
    plex_music.db held across a Plex listing or a lyric fetch (it was held
    across both: another writer waited out the whole run)
  * the anime mapping load, the Discogs freshness check and the Plex
    discography fallback read their files on a worker
  * external_ids.harvest walks the cache on a worker, with its own handle
    when none is passed (archive_backfill no longer opens one on the loop)

Each check fails on the code before this change.

    python tests/test_loop_free_raw.py
"""
import asyncio
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
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

from src.config import settings  # noqa: E402
settings.ENRICHMENT_CACHE = _TMP / "enrichment.db"

import src.database.connection as conn_mod  # noqa: E402
from src.database import loop_guard  # noqa: E402
from src.database.models import Base  # noqa: E402

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


def _sites(fragment: str) -> list:
    return [s["site"] for s in loop_guard.snapshot()["sites"] if fragment in s["site"]]


def _write_lock_free(db: Path) -> bool:
    """True when another connection can take the write lock right now."""
    c = sqlite3.connect(str(db), timeout=0, isolation_level=None)
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        c.close()


# ── 0. the guard sees a raw file ────────────────────────────────────────────
print("raw SQLite files under the guard")
loop_guard.reset()
probe = _TMP / "probe.db"


async def _raw_on_loop():
    con = loop_guard.sqlite_connect(str(probe))
    con.execute("SELECT 1").fetchone()
    con.close()


asyncio.run(_raw_on_loop())
check("a statement on a raw file from a coroutine is recorded, labelled with the file",
      any(s.endswith("[probe.db]") for s in _sites("probe.db")))
loop_guard.reset()


async def _raw_off_loop():
    await asyncio.to_thread(lambda: loop_guard.sqlite_connect(str(probe)).execute("SELECT 1"))


asyncio.run(_raw_off_loop())
check("the same statement on a worker thread is not", _sites("probe.db") == [])

# ── 1. sync_lyrics ──────────────────────────────────────────────────────────
print("sync_lyrics")
from src.services import lyrics  # noqa: E402

lyr_db = _TMP / "plex_music.db"


def _track(key: str) -> dict:
    return {"ratingKey": key, "title": f"T{key}", "grandparentTitle": "Band",
            "grandparentRatingKey": "a1", "parentRatingKey": "al1", "parentTitle": "Album",
            "Media": [{"Part": [{"size": 10, "Stream": [
                {"streamType": 4, "key": f"/library/streams/{key}", "codec": "txt"}]}]}]}


lock_free = {}


async def list_tracks(sec):
    if sec == "2":
        lock_free["listing"] = _write_lock_free(lyr_db)
    return [_track(f"{sec}{i}") for i in range(3)]


async def fetch_text(skey):
    await asyncio.sleep(0)
    if skey.endswith("12"):
        lock_free["fetch"] = _write_lock_free(lyr_db)
    return b"line one\nline two"


loop_guard.reset()
res = asyncio.run(lyrics.sync_lyrics([("1", "Music"), ("2", "More")], list_tracks, fetch_text,
                                     db_path=lyr_db, now=datetime(2026, 10, 19)))
check("sync_lyrics runs no statement on the loop", _sites("lyrics.py") == [])
check("plex_music.db's write lock is free while the next section is listed",
      lock_free.get("listing") is True)
check("... and while a lyric is fetched", lock_free.get("fetch") is True)
con = sqlite3.connect(str(lyr_db))
n_text = con.execute("SELECT COUNT(*) FROM track_lyrics WHERE lines = 2").fetchone()[0]
con.close()
check("the run's result and rows are unchanged (6 listed, 6 fetched, texts stored)",
      res["listed"] == 6 and res["fetched"] == 6 and res["complete"] and n_text == 6)

# ── 2. anime mapping, Discogs freshness, Plex discography ───────────────────
print("offline indexes")
from src.services import anime_mapping as am  # noqa: E402

am.MAPPING_DB_PATH = _TMP / "anime_mapping.db"
m = am.AnimeMapping()
m.tvdb_to_anidb[1] = 2
m.anidb_to_tvdb[2] = 1
m.total_entries = 1
am._save_to_db(m)
loop_guard.reset()
loaded = asyncio.run(am._load_or_download())
check("the anime mapping loads its cache file off the loop",
      loaded.total_entries >= 1 and _sites("anime_mapping") == [])

from src.services import discogs_offline as dg  # noqa: E402

dg.STYLES_DB_PATH = _TMP / "discogs_styles.db"
c = sqlite3.connect(str(dg.STYLES_DB_PATH))
c.execute("CREATE TABLE artist_styles (artist_norm TEXT PRIMARY KEY, styles TEXT)")
c.execute("INSERT INTO artist_styles VALUES ('band', '[]')")
c.commit()
c.close()
loop_guard.reset()
out = asyncio.run(dg.refresh_discogs_styles())
check("the Discogs freshness check reads its file off the loop",
      out == {"skipped": "fresh"} and _sites("discogs") == [])

from src.services import lidarr_discography as ld  # noqa: E402

settings.LIDARR_URL = ""
lyrics.LYRICS_DB_PATH = lyr_db
loop_guard.reset()
asyncio.run(ld.discography_summary(artist_name="Band"))
check("the Plex discography fallback reads plex_music.db off the loop",
      _sites("lidarr_discography") == [])

# ── 3. external ids harvest ─────────────────────────────────────────────────
print("external ids harvest")
from src.cache.metadata_cache import MetadataCache  # noqa: E402
from src.services import external_ids as ei  # noqa: E402

with MetadataCache() as mc:
    mc.set_cache("raw:movie:foo", {"title": "Foo", "media_type": "movie", "year": 2020}, days=30)


async def _index(failed):
    return {"movie": {"by_name": {}, "by_id": {"x": {}}}, "tv": {"by_name": {}, "by_id": {}}}


ei.build_arr_id_index = _index
loop_guard.reset()
try:
    hres = asyncio.run(ei.harvest(None))
except Exception as e:  # noqa: BLE001 - the old code crashed on a None handle; report it
    hres = {"crash": repr(e)}
check("harvest without a handle opens its own and walks the cache off the loop",
      hres.get("visited", 0) >= 1 and "crash" not in hres
      and _sites("external_ids") == [] and _sites("enrichment.db") == [])

src_ab = (ROOT / "src" / "services" / "archive_backfill.py").read_text(encoding="utf-8")
check("archive_backfill no longer opens a cache handle on the loop for harvest",
      "return await harvest(cache, limit=limit" in src_ab
      and "cache = MetadataCache()\n            return await harvest" not in src_ab)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
