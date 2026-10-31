"""Imports, the music matcher, the AniDB snapshot and the music-source probe
keep their heavy work off the event loop.

  * match_spotify_to_plex parses the all-tracks listing (64 MB at 50k
    tracks) and builds the normalised index in a worker thread
  * the Spotify upload unzips and writes in a thread; the import runs on its
    own worker (it commits per buffer; on the one writer thread every other
    write would queue behind the whole import), as a tracked task
  * ensure_offline_db parses the ~60 MB dump and rebuilds the snapshot in a
    thread, and opens the snapshot SQLite there too
  * sync_editions looks AniDB flags up in a thread
  * music_service() asks plex_music.db one LIMIT 1 question instead of
    lyrics_coverage()'s full-table aggregates

Each "off the loop" check fails on the code before this change.

    python tests/test_loop_free_s2b.py
"""
import asyncio
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

from src.config import settings  # noqa: E402
settings.ENRICHMENT_CACHE = _TMP / "enrichment.db"

import httpx  # noqa: E402

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import Base, LibraryConfig, SeriesEdition, User, WatchHistoryEntry  # noqa: E402

Base.metadata.create_all(conn_mod.engine)

from _checks import check  # noqa: E402


def _on_loop_thread() -> bool:
    # asyncio.run drives the loop on the main thread
    return threading.current_thread() is threading.main_thread()


# ── 1. music_matcher.match_spotify_to_plex ──────────────────────────────────
print("music_matcher.match_spotify_to_plex")
from src.services import music_matcher as mm  # noqa: E402

with conn_mod.get_db_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="owner"))
    db.flush()
    db.add(LibraryConfig(plex_section_key="5", plex_section_title="Music", plex_section_type="artist",
                         media_category="music"))
    for i in range(6):
        db.add(WatchHistoryEntry(
            user_id=1, plex_user_id="1", plex_item_id=f"spotify:{i}",
            title=f"Track {i} (feat. X)", series_title=f"Artist {i}", media_type="music",
            source="spotify", viewed_at=datetime(2026, 1, 1)))
    db.commit()

TRACKS = [{"ratingKey": 1000 + i, "title": f"Track {i}",
           "grandparentTitle": f"Artist {i}", "Genre": [{"tag": "rock"}]}
          for i in range(0, 6, 2)]
_parse_threads, _norm_threads = [], []


class FakeResp:
    status_code = 200

    def json(self):
        _parse_threads.append(_on_loop_thread())
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


_real_normalize = mm._normalize


def _spy_normalize(s):
    _norm_threads.append(_on_loop_thread())
    return _real_normalize(s)


_saved_mm = (mm.httpx, mm.settings)
mm.httpx = SimpleNamespace(AsyncClient=FakeClient)
mm.settings = SimpleNamespace(effective_plex_url="http://plex", effective_plex_token="t",
                              PLEX_CLIENT_ID="test")
# Count only the index build: the match loop normalises the plays per chunk.
_real_load = mm._load_unmatched_plays


def _load_and_stop_spying(*a):
    mm._normalize = _real_normalize
    return _real_load(*a)


mm._normalize = _spy_normalize
mm._load_unmatched_plays = _load_and_stop_spying
try:
    res = asyncio.run(mm.match_spotify_to_plex(1))
finally:
    mm.httpx, mm.settings = _saved_mm
    mm._normalize = _real_normalize
    mm._load_unmatched_plays = _real_load
with conn_mod.get_db_session() as db:
    keys = sorted(r.plex_item_id for r in db.query(WatchHistoryEntry).all())
check("the section body is parsed off the loop", _parse_threads == [False])
check("the index is normalised off the loop", _norm_threads and not any(_norm_threads))
check("the matches are unchanged (even tracks, feat. stripped)",
      res == {"matched": 3, "unmatched": 3, "skipped": 0}
      and keys == ["1000", "1002", "1004", "spotify:1", "spotify:3", "spotify:5"])


# ── 2. imports.spotify_upload / spotify_run ─────────────────────────────────
print("imports.spotify_upload / spotify_run")
from src.routers import imports as imp  # noqa: E402
from src.services import bg_tasks  # noqa: E402
from src.services import spotify_import as si  # noqa: E402

si.IMPORT_DIR = _TMP / "imports"
_threads = {}
_real_save, _real_pending, _real_run = si.save_upload, si.pending_files, si.run_import


def _spy(key, fn):
    def inner(*a, **kw):
        _threads.setdefault(key, []).append(threading.current_thread().name
                                            if not _on_loop_thread() else "loop")
        return fn(*a, **kw)
    return inner


class _Upload:
    def __init__(self, name, data):
        self.filename, self._data = name, data

    async def read(self, n=-1):
        return self._data if n < 0 else self._data[:n]


buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as zf:
    zf.writestr("Spotify Extended/Streaming_History_Audio_2024_1.json", json.dumps([{
        "ts": "2024-01-02T03:04:05Z", "ms_played": 200_000, "reason_end": "trackdone",
        "master_metadata_track_name": "Song", "master_metadata_album_artist_name": "Band",
        "spotify_track_uri": "spotify:track:abc"}]))
    zf.writestr("Spotify Extended/StreamingHistory0.json", "[]")

si.save_upload, si.pending_files = _spy("save", _real_save), _spy("pending", _real_pending)
si.run_import = _spy("run", _real_run)


async def _upload_and_run():
    up = await imp.spotify_upload([_Upload("my_spotify_data.zip", buf.getvalue())], None)
    started = await imp.spotify_run({"user_id": 1}, None)
    tasks = [t for t in bg_tasks._BG_TASKS if t.get_name() == "spotify_import"]
    held = bool(tasks)
    for t in tasks:
        await t
    return up, started, held


try:
    up, started, held = asyncio.run(_upload_and_run())
finally:
    si.save_upload, si.pending_files, si.run_import = _real_save, _real_pending, _real_run
check("the zip is unpacked and written off the loop",
      _threads.get("save") and "loop" not in _threads["save"])
check("the pending-directory scans run off the loop",
      _threads.get("pending") and "loop" not in _threads["pending"])
check("the upload result is unchanged",
      up["saved"] == ["Streaming_History_Audio_2024_1.json"]
      and [r["name"] for r in up["rejected"]] == ["StreamingHistory0.json"]
      and [p["name"] for p in up["pending"]] == ["Streaming_History_Audio_2024_1.json"])
check("the import is a tracked task", started == {"started": True} and held)
check("the import runs off the loop, on its own worker (not the writer thread)",
      _threads.get("run") and not any(n == "MainThread" or n.startswith("db-writer")
                                      for n in _threads["run"]))
check("the import landed and the running flag is cleared",
      imp._last_result.get("imported") == 1 and not imp._running)


# ── 3. anime_offline.ensure_offline_db ──────────────────────────────────────
print("anime_offline.ensure_offline_db")
from src.services import anime_offline as ao  # noqa: E402

DUMP = {"data": [{"title": "Cowboy Bebop", "animeSeason": {"year": 1998}, "tags": ["space"],
                  "sources": ["https://anidb.net/anime/23", "https://anilist.co/anime/1"]}]}
_ao_threads = {}
_real_ao = {n: getattr(ao, n) for n in ("_fresh", "_usable", "_rebuild")}


def _ao_spy(name):
    fn = _real_ao[name]

    def inner(*a, **kw):
        _ao_threads.setdefault(name, []).append(_on_loop_thread())
        return fn(*a, **kw)
    return inner


class _JsonSpy:
    @staticmethod
    def loads(*a, **kw):
        _ao_threads.setdefault("json.loads", []).append(_on_loop_thread())
        return json.loads(*a, **kw)

    dumps = staticmethod(json.dumps)


def _client(**kw):
    return httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=json.dumps(DUMP).encode())))


_saved_ao = (ao.OFFLINE_DB_PATH, ao.httpx, ao.json)
ao.OFFLINE_DB_PATH = _TMP / "offline.db"
ao.httpx = SimpleNamespace(AsyncClient=_client)
ao.json = _JsonSpy
for _n in _real_ao:
    setattr(ao, _n, _ao_spy(_n))
try:
    ok = asyncio.run(ao.ensure_offline_db())
    # 503: the snapshot exists but is "stale" (under 10k rows) → _usable answers
    ao.httpx = SimpleNamespace(AsyncClient=lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(503))))
    ok503 = asyncio.run(ao.ensure_offline_db())
finally:
    ao.httpx, ao.json = _saved_ao[1:]
    for _n, _f in _real_ao.items():
        setattr(ao, _n, _f)
hit = ao.lookup(anilist_id=1)
ao.OFFLINE_DB_PATH = _saved_ao[0]
check("the dump is parsed off the loop", _ao_threads.get("json.loads") == [False])
check("the snapshot is rebuilt off the loop", _ao_threads.get("_rebuild") == [False])
check("the freshness/usability opens run off the loop",
      _ao_threads.get("_fresh") and not any(_ao_threads["_fresh"])
      and _ao_threads.get("_usable") and not any(_ao_threads["_usable"]))
check("the result is unchanged (rebuilt, then usable after a failed refresh)",
      ok is True and hit and hit["title"] == "Cowboy Bebop" and ok503 is True)


# ── 4. editions.sync_editions ───────────────────────────────────────────────
print("editions.sync_editions")
from src.services import editions as ed  # noqa: E402


class _Sonarr:
    async def get_series(self):
        return [{"id": 3, "title": "Disc Show", "seriesType": "anime", "year": 2010}]

    async def get_episode_files(self, sid):
        return [{"quality": {"quality": {"source": "bluray"}},
                 "relativePath": "Disc Show S01E01 [BD 1080p].mkv"}]


_lk_threads = []


def _lookup(**kw):
    _lk_threads.append(_on_loop_thread())
    return {"tags": ["tv censoring", "censored uncensored version"]}


_state = {}
out = asyncio.run(ed.sync_editions(_Sonarr(), budget=5, now=datetime(2026, 10, 1), lookup=_lookup,
                                   get_state=_state.get, set_state=_state.__setitem__))
with conn_mod.get_db_session() as db:
    row = db.query(SeriesEdition).filter(SeriesEdition.arr_id == 3).first()
    got = row and (row.anidb_uncensored, row.anidb_censored, row.files_disc)
check("the AniDB snapshot lookup runs off the loop", _lk_threads == [False])
check("the flags and the row are unchanged",
      out.get("checked") == 1 and got == (True, True, 1))


# ── 5. music_source.music_service ───────────────────────────────────────────
print("music_source.music_service")
from src.services import lyrics as ly  # noqa: E402
from src.services import music_source as ms  # noqa: E402

_saved_ms = (ly.LYRICS_DB_PATH, ly.lyrics_coverage, settings.LIDARR_URL, settings.LIDARR_API_KEY)
_coverage_calls = []
ly.lyrics_coverage = lambda *a, **kw: _coverage_calls.append(1) or {"artists_indexed": 1}
settings.LIDARR_URL = settings.LIDARR_API_KEY = None
try:
    ly.LYRICS_DB_PATH = _TMP / "absent_plex_music.db"
    none_absent = ms.music_service()
    created = ly.LYRICS_DB_PATH.exists()
    ly.LYRICS_DB_PATH = _TMP / "plex_music.db"
    c = sqlite3.connect(str(ly.LYRICS_DB_PATH))
    c.execute("CREATE TABLE plex_artists (artist_key TEXT PRIMARY KEY, tracks INTEGER NOT NULL DEFAULT 0)")
    c.execute("INSERT INTO plex_artists VALUES ('a0', 0)")
    c.commit()
    none_empty = ms.music_service()
    c.execute("INSERT INTO plex_artists VALUES ('a1', 4)")
    c.commit()
    c.close()
    plex = ms.music_service()
    settings.LIDARR_URL, settings.LIDARR_API_KEY = "http://lidarr:8686", "k"
    lidarr = ms.music_service()
finally:
    ly.LYRICS_DB_PATH, ly.lyrics_coverage, settings.LIDARR_URL, settings.LIDARR_API_KEY = _saved_ms
check("the probe no longer runs lyrics_coverage()'s full-table aggregates", _coverage_calls == [])
check("the answers are unchanged (no index / empty index / an artist / Lidarr)",
      (none_absent, none_empty, plex, lidarr) == (None, None, "plex", "lidarr"))
check("a missing index file is not created by the probe", created is False)

check.done()
