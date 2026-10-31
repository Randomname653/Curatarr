"""Guards in the enrichment / music paths that let an action through, or read
a failure as "no data", when they could not decide.

  * OMDb budget: an unreadable calls-used-today counted as 0, so the daily
    limit was handed out again; now the day counts as spent (a fresh DB
    without the table still starts from zero)
  * music matcher: an unreadable stop flag read as "keep going" and the
    user's Stop was ignored; now it stops
  * catalog sync: every Lidarr add failing logged at DEBUG and reported
    ok=True; now WARNING, add_failed in the summary, ok=False
  * streaming fetch: a crashing source fetcher read as "no data from that
    source" at DEBUG; now a WARNING (outages stay quiet)
  * MusicBrainz / Last.fm: a response-shape bug was logged as an outage at
    DEBUG; it now propagates and the caller logs it
  * Spotify: an unreadable persisted 429 ban read as "no ban"; now it backs
    off

Each check fails on the code before this change.

    python tests/test_fail_closed_s2_g2.py
"""
import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import Base  # noqa: E402
from src.services import app_state  # noqa: E402
from src.config import settings  # noqa: E402

Base.metadata.create_all(conn_mod.engine)

from _checks import check  # noqa: E402


class _Grab(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def warned(self, needle=""):
        return any(r.levelno >= logging.WARNING and needle in r.getMessage()
                   for r in self.records)


def _grab(name):
    h = _Grab()
    lg = logging.getLogger(name)
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    return h


_real_get = app_state.get_state


def _broken(key):
    raise RuntimeError("database is locked")


# ── 1. OMDb budget ──────────────────────────────────────────────────────────
print("OMDb budget")
import src.services.media_enricher as me  # noqa: E402

_real_limit = settings.OMDB_DAILY_LIMIT
settings.OMDB_DAILY_LIMIT = 1000
h = _grab("src.services.media_enricher")
app_state.get_state = _broken
try:
    left = me.omdb_quota_left()
finally:
    app_state.get_state = _real_get
check("an unreadable call count leaves no budget (was: the full 1000)", left <= 0)
check("...and says so at WARNING", h.warned("unreadable"))

from sqlalchemy.exc import OperationalError  # noqa: E402


def _no_table(key):
    raise OperationalError("SELECT", {}, Exception("no such table: app_state"))


app_state.get_state = _no_table
try:
    left_fresh = me.omdb_quota_left()
finally:
    app_state.get_state = _real_get
check("a fresh DB without app_state still counts from zero", left_fresh == 1000)
settings.OMDB_DAILY_LIMIT = _real_limit

# ── 2. music matcher stop flag ──────────────────────────────────────────────
print("music matcher stop flag")
import src.services.music_matcher as mmatch  # noqa: E402

app_state.get_state = _broken
try:
    stop = mmatch._stop_requested()
finally:
    app_state.get_state = _real_get
check("an unreadable stop flag stops the pipeline", stop is True)
app_state.set_state("music_pipeline_stop_requested", "0")
check("...a readable 'no stop' still runs", mmatch._stop_requested() is False)

# ── 3. catalog sync: every add failing ──────────────────────────────────────
print("catalog sync")
import src.routers.library as lib  # noqa: E402
import src.services.music_catalog_sync as mcs  # noqa: E402
import src.services.soulsync_client as sc  # noqa: E402


async def _pages(page=1, limit=100):
    if page > 1:
        return None
    return {"artists": [{"name": "A", "musicbrainz_id": "mb-a", "album_count": 1},
                        {"name": "B", "musicbrainz_id": "mb-b", "album_count": 1}],
            "pagination": {"has_next": False}}


class _Lidarr:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_artists(self):
        return []

    async def add_artist(self, **kw):
        raise RuntimeError("root folder /music does not exist")

    async def refresh_artist(self, artist_id):
        return {}


_saved = (sc._configured, sc.list_artists_page, lib._get_arr_url_key,
          lib._make_client, lib._read_defaults)
sc._configured = lambda: True
sc.list_artists_page = _pages
lib._get_arr_url_key = lambda svc: ("http://lidarr", "key")
lib._make_client = lambda svc, url, key: _Lidarr()
lib._read_defaults = lambda svc: {"root_folder_path": "/music",
                                  "quality_profile_id": 1, "metadata_profile_id": 1}
h = _grab("src.services.music_catalog_sync")
try:
    res = asyncio.run(mcs.sync_soulsync_to_lidarr())
finally:
    (sc._configured, sc.list_artists_page, lib._get_arr_url_key,
     lib._make_client, lib._read_defaults) = _saved
check("every add failing is not ok", res.get("ok") is False and res.get("add_failed") == 2)
check("...the first failure is a WARNING", h.warned("Lidarr add"))
check("...and the summary line carries add_failed",
      any(r.levelno == logging.INFO and "add_failed=2" in r.getMessage() for r in h.records))

# ── 4. streaming fetch: a crashing source ───────────────────────────────────
print("streaming fetch")
_real_fetch = me._fetch_source


async def _crash(source, ctx):
    raise TypeError("'NoneType' object is not subscriptable")


me._fetch_source = _crash
h = _grab("src.services.media_enricher")
try:
    asyncio.run(me._streaming_fetch_runner({"media_type": "movie", "title": "Heat"}, ["tmdb"]))
finally:
    me._fetch_source = _real_fetch
check("a crashing source fetcher is a WARNING, not 'no data' at DEBUG",
      h.warned("[stream] tmdb"))

# ── 5. MusicBrainz / Last.fm: shape bug vs outage ───────────────────────────
print("music metadata")
import httpx  # noqa: E402
import src.services.music_metadata as mmeta  # noqa: E402
from src.services.enrichment_state import TRANSIENT  # noqa: E402


class _R:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


_saved_mb = (mmeta._mb_request, mmeta._cache_get, mmeta._cache_set)
mmeta._cache_get = lambda k: None
mmeta._cache_set = lambda k, v, d: None


async def _mb_list(client, url, params):
    return _R(["not", "a", "dict"])          # a shape the code does not expect


async def _mb_down(client, url, params):
    raise httpx.ConnectError("connection refused")


try:
    mmeta._mb_request = _mb_list
    try:
        asyncio.run(mmeta.fetch_musicbrainz_artist("Low"))
        shape = "swallowed"
    except AttributeError:
        shape = "raised"
    mmeta._mb_request = _mb_down
    down = asyncio.run(mmeta.fetch_musicbrainz_artist("Low"))
finally:
    mmeta._mb_request, mmeta._cache_get, mmeta._cache_set = _saved_mb
check("a MusicBrainz shape bug is not reported as an outage", shape == "raised")
check("...an outage is still TRANSIENT", down is TRANSIENT)

_saved_lfm = mmeta.fetch_lastfm_artist


async def _lfm_bug(name):
    raise KeyError("artist")


mmeta.fetch_lastfm_artist = _lfm_bug
h = _grab("src.services.music_metadata")
try:
    r = asyncio.run(mmeta.enrich_artist("Low", skip_mb=True))
finally:
    mmeta.fetch_lastfm_artist = _saved_lfm
check("fast mode: a crashing Last.fm fetch is TRANSIENT and a WARNING",
      r is TRANSIENT and h.warned("[music] Last.fm"))

# ── 6. Spotify persisted backoff ────────────────────────────────────────────
print("spotify backoff")
import src.services.spotify_client as spc  # noqa: E402

spc._last_known_until = None
app_state.get_state = _broken
try:
    wait = spc._persisted_backoff_remaining()
finally:
    app_state.get_state = _real_get
check("an unreadable persisted ban backs off (was: 0 = call Spotify)", wait > 0)
check("...and no stored ban is still no backoff", spc._persisted_backoff_remaining() == 0.0)

check.done()
