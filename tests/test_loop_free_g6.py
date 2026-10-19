"""Enrichment, recommendations, the judge's clerk, verification questions and
the editions walker run their database work off the event loop.

Each scenario drives a converted coroutine against a temp file database with
the loop guard in raise mode (src/database/loop_guard.py), the outside world
stubbed, and checks both that the guard saw no site in these files and that
the side effect is unchanged:

  * verification_session.start_verification_session: seven history queries,
    the recent-question check, the language read and the message write
  * editions.sync_editions: cursor read, fresh-row read, upsert, cursor write
  * media_enricher.enrich_media_item: the owner-pin and MediaIdentity lookups
    (the resolved tmdb id is used), and no cache call on the loop thread
  * media_enricher.fetch_omdb_data: the daily-budget read and the counter
    write (read-modify-write on the writer thread)
  * recommendations_engine.score_arr_items / _get_trending: the taste read
    ranks by vector, the trending cache is served from app_state
  * pillars.build_evidence (deletion judgement): the clerk's own DB facts,
    the tech and dialogue reads; the owner's watch is KNOWN, not deferred

    python tests/test_loop_free_g6.py
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402

import src.database.connection as conn_mod  # noqa: E402
from src.config import settings  # noqa: E402
from src.database import loop_guard  # noqa: E402
from src.database.models import (Base, EncryptedTasteVector, MediaIdentity,  # noqa: E402
                                 ProactiveMessage, SeriesEdition, TasteVectorEntry,
                                 User, WatchHistoryEntry)
from src.services import app_state  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


tmp = Path(tempfile.mkdtemp())
eng = create_engine(f"sqlite:///{(tmp / 'g6.db').as_posix()}",
                    connect_args={"check_same_thread": False})
Base.metadata.create_all(eng)
conn_mod.SessionLocal.configure(bind=eng)
conn_mod.engine = eng
loop_guard.install(eng)
loop_guard.serving(True)
settings.ENRICHMENT_CACHE = tmp / "enrichment.db"   # nothing touches the real cache

MINE = tuple(f"src/services/{m}.py" for m in (
    "media_enricher", "verification_session", "recommendations_engine",
    "kb_overview", "editions", "collection_designer", "subtitle_signals",
    "taste_engine", "chat_starters", "facet_index", "library_sorting",
    "corpus_repair", "pillars", "semantic_search"))

NOW = datetime.utcnow()
with conn_mod.get_db_session() as _db:
    _db.add(User(id=1, plex_user_id="p1", plex_username="admin", is_admin=True, is_active=True))
    _db.commit()


def guarded(coro_fn, *a, **kw):
    """(result or the exception, sites in my files) of one run in raise mode."""
    loop_guard.reset()
    os.environ["CURATARR_LOOP_GUARD"] = "raise"
    try:
        try:
            out = asyncio.run(coro_fn(*a, **kw))
        except Exception as e:  # noqa: BLE001 - the check reports it
            out = e
    finally:
        os.environ.pop("CURATARR_LOOP_GUARD", None)
    sites = [s["site"] for s in loop_guard.snapshot()["sites"]
             if s["site"].startswith(MINE)]
    return out, sites


class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body


def _client(body):
    """An httpx.AsyncClient stand-in answering every request with ``body``."""
    class _C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _Resp(body)

        async def post(self, *a, **k):
            return _Resp(body)
    return _C


import httpx  # noqa: E402
_real_client = httpx.AsyncClient


# ── 1. verification_session ─────────────────────────────────────────────────
print("verification_session.start_verification_session")
import src.services.llm_priority as lp  # noqa: E402
import src.services.verification_session as vs  # noqa: E402

with conn_mod.get_db_session() as _db:
    _db.add(WatchHistoryEntry(
        user_id=1, plex_user_id="p1", plex_item_id="w1", title="Ep 1",
        series_title="Slow Show", media_type="show", completed=False,
        duration_ms=3_600_000, view_offset_ms=600_000,
        viewed_at=NOW - timedelta(days=1)))
    _db.commit()

_memories = []


async def _noop(*a, **k):
    return None


async def _memory(**kw):
    _memories.append(kw)


saved = (lp.wait_for_curator, vs.write_memory)
lp.wait_for_curator, vs.write_memory = _noop, _memory
httpx.AsyncClient = _client({"message": {"content": "What made you drop Slow Show?"}})
try:
    out, sites = guarded(vs.start_verification_session, 1)
finally:
    (lp.wait_for_curator, vs.write_memory), httpx.AsyncClient = saved, _real_client
with conn_mod.get_db_session() as _db:
    msgs = [(m.trigger_type, m.message, json.loads(m.trigger_data).get("title"))
            for m in _db.query(ProactiveMessage).all()]
check("no statement on the loop", sites == [])
check("the rephrased question is returned", out == "What made you drop Slow Show?")
check("the message is stored with its question and the question is remembered",
      msgs == [("verification", "What made you drop Slow Show?", "Slow Show")]
      and [m.get("metadata", {}).get("title") for m in _memories] == ["Slow Show"])

out, sites = guarded(vs.start_verification_session, 1)
check("asked within 12 h: nothing asked again, still no loop statement",
      out is None and sites == [])


# ── 2. editions ─────────────────────────────────────────────────────────────
print("editions.sync_editions")
from src.services import editions as ed  # noqa: E402


class _Sonarr:
    async def get_series(self):
        return [{"id": 3, "title": "Disc Show", "seriesType": "anime", "year": 2010,
                 "tvdbId": 9, "titleSlug": "disc-show", "rootFolderPath": "/anime"}]

    async def get_episode_files(self, sid):
        return [{"quality": {"quality": {"source": "bluray"}},
                 "relativePath": "Disc Show S01E01 [BD 1080p].mkv"}]


app_state.set_state(ed._CURSOR_KEY, "2")    # mid-walk: series 3 is still ahead
out, sites = guarded(ed.sync_editions, _Sonarr(), budget=5, now=NOW,
                     lookup=lambda **kw: {"tags": ["tv censoring"]})
with conn_mod.get_db_session() as _db:
    row = _db.query(SeriesEdition).filter(SeriesEdition.arr_id == 3).first()
    got = row and (row.title, row.files_disc, row.episode_files, row.checked_at is not None)
check("no statement on the loop", sites == [])
check("the run finished and counted the series",
      isinstance(out, dict) and out.get("checked") == 1 and out.get("done") is True)
check("the edition row was upserted and stamped",
      got == ("Disc Show", 1, 1, True))
check("the cursor is written back (reset after the full pass)",
      app_state.get_state(ed._CURSOR_KEY) == "0")


# ── 3. media_enricher ───────────────────────────────────────────────────────
print("media_enricher.enrich_media_item / fetch_omdb_data")
import src.services.media_enricher as me  # noqa: E402

with conn_mod.get_db_session() as _db:
    _db.add(MediaIdentity(plex_rating_key="radarr:5", media_type="movie",
                          title="The Matrix", tmdb_id=603))
    _db.commit()


class _ThreadCache:
    """A MetadataCache stand-in that notes the thread of every call."""
    threads: list = []
    writes: list = []

    def __init__(self, *a, **k):
        _ThreadCache.threads.append(threading.get_ident())

    def get_cache(self, key):
        _ThreadCache.threads.append(threading.get_ident())
        return None

    def set_cache(self, key, value, days=7):
        _ThreadCache.threads.append(threading.get_ident())
        _ThreadCache.writes.append(key)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_tmdb_asked = []
_loop_thread = []


async def _tmdb_full(tmdb_id, media_type="movie"):
    _loop_thread.append(threading.get_ident())
    _tmdb_asked.append(tmdb_id)
    return {"title": "The Matrix", "year": 1999, "genres": ["Action"],
            "overview": "A hacker learns the truth.", "source": "tmdb"}


async def _omdb_none(imdb_id):
    return None


saved = (me.MetadataCache, me.fetch_tmdb_full, me.fetch_omdb_data)
me.MetadataCache, me.fetch_tmdb_full, me.fetch_omdb_data = _ThreadCache, _tmdb_full, _omdb_none
try:
    out, sites = guarded(me.enrich_media_item, "The Matrix", media_type="movie",
                         plex_rating_key="radarr:5", year=1999, skip_llm_summary=True)
finally:
    me.MetadataCache, me.fetch_tmdb_full, me.fetch_omdb_data = saved
check("no statement on the loop", sites == [])
check("the MediaIdentity id resolved the fetch (pin lookup answered, not transient)",
      isinstance(out, dict) and out.get("tmdb_id") == 603 and _tmdb_asked == [603])
check("every cache call ran off the loop thread",
      _ThreadCache.threads and _loop_thread and _loop_thread[0] not in _ThreadCache.threads)

saved = (settings.OMDB_API_KEY, settings.OMDB_DAILY_LIMIT)
settings.OMDB_API_KEY, settings.OMDB_DAILY_LIMIT = "test-key", 100
httpx.AsyncClient = _client({"Response": "True", "Title": "The Matrix", "Type": "movie",
                             "Year": "1999", "Ratings": []})
try:
    out, sites = guarded(me.fetch_omdb_data, "tt0133093")
    out2, sites2 = guarded(me.fetch_omdb_data, "tt0133093")
finally:
    httpx.AsyncClient = _real_client
    settings.OMDB_API_KEY, settings.OMDB_DAILY_LIMIT = saved
check("no statement on the loop", sites == [] and sites2 == [])
check("both calls answered and were counted against today's budget",
      isinstance(out, dict) and out.get("title") == "The Matrix"
      and isinstance(out2, dict)
      and app_state.get_state(f"omdb_calls:{me._omdb_day()}") == "2")


# ── 4. recommendations_engine ───────────────────────────────────────────────
print("recommendations_engine.score_arr_items / _get_trending")
import src.services.recommendations_engine as reng  # noqa: E402

with conn_mod.get_db_session() as _db:
    _db.add(TasteVectorEntry(user_id=1, genre_affinity=json.dumps(
        {"movie": {"genre_affinity": {"Drama": 1.0}}})))
    _db.add(EncryptedTasteVector(user_id=1, media_category="movie", salt="",
                                 encrypted_blob=json.dumps(
                                     {"embedding": [1.0, 0.0], "disliked_titles": ["Bad"]})))
    _db.commit()

_embs = {"a": {"embedding": [1.0, 0.0]}, "b": {"embedding": [0.0, 1.0]},
         "bad": {"embedding": [1.0, 0.0]}}
import src.vector_store.chromadb_wrapper as cw  # noqa: E402
saved = cw.get_chroma_db
cw.get_chroma_db = lambda: SimpleNamespace(get_by_ids=lambda ids: {i: _embs[i] for i in ids if i in _embs})
items = [{"title": "B", "plex_rating_key": "b"}, {"title": "Bad", "plex_rating_key": "bad"},
         {"title": "A", "plex_rating_key": "a"}]
try:
    out, sites = guarded(reng.score_arr_items, 1, "movie", items)
finally:
    cw.get_chroma_db = saved
check("no statement on the loop", sites == [])
check("ranked by the stored taste vector, the recorded dislike last",
      isinstance(out, list) and [i["title"] for i in out] == ["A", "B", "Bad"])

app_state.set_state("tmdb_trending:movie", json.dumps(
    {"fetched_at": NOW.isoformat(), "titles": ["Fresh Film (2026)"]}))
saved = settings.TMDB_API_KEY
settings.TMDB_API_KEY = ""     # a cache miss could not reach TMDB here
try:
    out, sites = guarded(reng._get_trending, "movie")
finally:
    settings.TMDB_API_KEY = saved
check("trending is served from its app_state cache, off the loop",
      out == ["Fresh Film (2026)"] and sites == [])


# ── 5. pillars.build_evidence (deletion judgement) ──────────────────────────
print("pillars.build_evidence")
import src.cache.metadata_cache as mcache  # noqa: E402
import src.services.episodic_memory as em  # noqa: E402
from src.services import pillars  # noqa: E402

with conn_mod.get_db_session() as _db:
    _db.add(WatchHistoryEntry(
        user_id=1, plex_user_id="p1", plex_item_id="m1", title="Old Film",
        media_type="movie", completed=True, duration_ms=5_400_000,
        view_offset_ms=5_400_000, viewed_at=NOW - timedelta(days=400)))
    _db.commit()


async def _no_considerations(*a, **k):
    return []


async def _verified(*a, **k):
    _loop_thread[:] = [threading.get_ident()]
    return {"title": "Old Film", "year": 1990, "genres": ["Drama"],
            "overview": "A family drama.", "significance": "A minor classic.",
            "significance_checked": True}


async def _wiki(*a, **k):
    return ""


_ThreadCache.threads.clear()
saved = (em.retrieve_considerations, me.ensure_verified_data, me.fetch_wikipedia_summary,
         mcache.MetadataCache, settings.PRINCIPLES_ENABLED)
em.retrieve_considerations, me.ensure_verified_data = _no_considerations, _verified
me.fetch_wikipedia_summary, mcache.MetadataCache = _wiki, _ThreadCache
settings.PRINCIPLES_ENABLED = False
try:
    out, sites = guarded(pillars.build_evidence,
                         {"title": "Old Film", "year": 1990, "genres": ["Drama"]}, 1, "movie")
finally:
    (em.retrieve_considerations, me.ensure_verified_data, me.fetch_wikipedia_summary,
     mcache.MetadataCache, settings.PRINCIPLES_ENABLED) = saved
flags = out.get("flags", {}) if isinstance(out, dict) else {}
check("no statement on the loop", sites == [])
check("the owner's watch is read, not deferred as UNKNOWN",
      flags.get("owner_watched") is True and flags.get("owner_watch_unknown") is False)
check("the facts block names the owner's watch",
      isinstance(out, dict) and "watched 1x" in out.get("facts", ""))
check("the form guard's cache read answered, off the loop thread",
      flags.get("significance_unchecked") is False and _ThreadCache.threads
      and _loop_thread[0] not in _ThreadCache.threads)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
