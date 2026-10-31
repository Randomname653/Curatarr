"""Silent-failure sweep, round 3 — enrichment / Plex-write / import handlers
(docs/SILENT_FAILURE_SWEEP_2026_10.md §2).

Broad handlers that turned an outage or a bug into permanent "no data", or
into a guard that fails open. The behavioural checks below fail on the code
before this change; source-level pins are used only where the handler sits
deep inside a long runner.

    python tests/test_silent_failures_r3_enrich.py
"""
import asyncio
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
import types
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from _checks import memory_engine, with_users  # noqa: E402
import src.database.connection as conn  # noqa: E402
import src.services.app_state as app_state  # noqa: E402
from src.config import settings  # noqa: E402
from src.database.models import (Base, CachedRecommendation, LibraryConfig,  # noqa: E402
                                 SeriesEdition, User)

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── shared fakes ─────────────────────────────────────────────────────────────

_RealClient = httpx.AsyncClient


@contextmanager
def mock_http(handler):
    """Every httpx.AsyncClient built inside answers from ``handler``."""
    def factory(*a, **k):
        k.pop("transport", None)
        return _RealClient(*a, transport=httpx.MockTransport(handler), **k)
    httpx.AsyncClient = factory
    try:
        yield
    finally:
        httpx.AsyncClient = _RealClient


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextmanager
def capture(logger):
    lg = logger if isinstance(logger, logging.Logger) else logging.getLogger(logger)
    h = _Cap()
    old = (lg.level, lg.propagate)
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    try:
        yield h.records
    finally:
        lg.removeHandler(h)
        lg.setLevel(old[0])
        lg.propagate = old[1]


def warned(records, text):
    return any(r.levelno >= logging.WARNING and text in r.getMessage() for r in records)


@contextmanager
def broken_db():
    """get_db_session that fails the way a locked SQLite does."""
    @contextmanager
    def _broken():
        raise sqlite3.OperationalError("database is locked")
        yield  # pragma: no cover
    real = conn.get_db_session
    conn.get_db_session = _broken
    try:
        yield
    finally:
        conn.get_db_session = real


@contextmanager
def memory_db():
    engine = memory_engine()
    Base.metadata.create_all(engine)
    with_users(engine, 1)           # the owner the cached rows belong to
    Session = sessionmaker(bind=engine)

    @contextmanager
    def _session():
        db = Session()
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
    real = conn.get_db_session
    conn.get_db_session = _session
    try:
        yield _session
    finally:
        conn.get_db_session = real


@contextmanager
def patched(obj, **attrs):
    old = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


def raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


# ── 1. anime_mapping ─────────────────────────────────────────────────────────

def test_anime_mapping_never_wipes_or_freezes_empty():
    import src.services.anime_mapping as am
    tmp = Path(tempfile.mkdtemp())
    db_path = tmp / "anime_mapping.db"

    def count():
        c = sqlite3.connect(db_path)
        try:
            return c.execute("SELECT COUNT(*) FROM anime_mapping").fetchone()[0]
        finally:
            c.close()

    def age_out():
        old = time.time() - 30 * 86400
        os.utime(db_path, (old, old))

    real_lod = am._load_or_download
    with patched(am, MAPPING_DB_PATH=db_path, _mapping=None, _load_lock=None):
        good = am.AnimeMapping()
        good.tvdb_to_anidb = {1: 10, 2: 20}
        good.anidb_to_tvdb = {10: 1, 20: 2}
        good.total_entries = 2
        am._save_to_db(good)
        am.update_anilist_id(10, 999)

        age_out()
        with mock_http(lambda req: httpx.Response(200, text="<anime-list><anime")):
            m = asyncio.run(am._load_or_download())
        check("anime-lists: a garbled download keeps the cached DB populated",
              count() == 2 and m.total_entries == 2)

        xml = ('<anime-list><anime anidbid="10" tvdbid="1"/><anime anidbid="20" tvdbid="2"/>'
               '<anime anidbid="30" tvdbid="3"/></anime-list>')
        age_out()
        with mock_http(lambda req: httpx.Response(200, text=xml)):
            m = asyncio.run(am._load_or_download())
        c = sqlite3.connect(db_path)
        learned = c.execute("SELECT anilist_id FROM anime_mapping WHERE anidb_id=10").fetchone()
        c.close()
        check("anime-lists: the weekly refresh keeps AniList ids learned at runtime",
              count() == 3 and learned == (999,) and m.anidb_to_anilist.get(10) == 999)

        calls = []

        async def empty_load():
            calls.append(1)
            return am.AnimeMapping()
        am._load_or_download = empty_load
        try:
            am._mapping, am._load_lock, am._empty_until = None, None, None
            asyncio.run(am.get_anime_mapping())
            am._load_lock = None
            asyncio.run(am.get_anime_mapping())
            held = len(calls)
            am._empty_until = datetime.utcnow() - timedelta(seconds=1)
            am._load_lock = None
            asyncio.run(am.get_anime_mapping())
        finally:
            am._load_or_download = real_lod
            am._mapping = None
        check("anime-lists: an empty mapping is held briefly, then retried (not process-final)",
              held == 1 and len(calls) == 2)


# ── 2. anime_offline ─────────────────────────────────────────────────────────

def test_anime_offline_rebuild_is_atomic():
    import src.services.anime_offline as ao
    tmp = Path(tempfile.mkdtemp())
    entries = [{"title": "Cowboy Bebop", "animeSeason": {"year": 1998}, "tags": ["space"],
                "sources": ["https://anidb.net/anime/23", "https://anilist.co/anime/1"]}]
    with patched(ao, OFFLINE_DB_PATH=tmp / "offline.db"):
        ao._rebuild(entries)
        # sources=5 → TypeError mid-insert, after the first row went in
        try:
            ao._rebuild(entries + [{"title": "Broken", "sources": 5}])
        except Exception:
            pass
        hit = ao.lookup(anilist_id=1)
        check("anime-offline: a rebuild failing mid-insert leaves the old snapshot intact",
              bool(hit) and hit["title"] == "Cowboy Bebop")

    with patched(ao, OFFLINE_DB_PATH=tmp / "empty.db"):
        sqlite3.connect(ao.OFFLINE_DB_PATH).close()        # file, no tables
        with mock_http(lambda req: httpx.Response(503)):
            ok = asyncio.run(ao.ensure_offline_db())
        check("anime-offline: a failed refresh does not report a table-less file as usable",
              ok is False)
        with capture(ao.logger) as recs:
            ao._lookup_failures = 0
            ao.lookup(title="x")
        check("anime-offline: an unreadable snapshot is a WARNING, not DEBUG",
              warned(recs, "lookup failed"))


# ── 3. media_enricher ────────────────────────────────────────────────────────

def test_media_enricher_merge_pin_jikan_topups():
    import src.services.media_enricher as me
    from src.services.enrichment_state import TRANSIENT

    def boom(*a, **k):
        raise ValueError("shape drift")
    with patched(me, _merge_raw_metadata=boom):
        raw = {"title": "Batman Beyond"}
        with capture(me.logger) as recs:
            me._merge_source_into_raw(raw, "tvdb", {"title": "Batman Beyond",
                                                    "genres": ["Action", "Hentai"]}, "show", False)
        check("merge crash: WARNING, and the unconfirmed adult genre is still stripped",
              warned(recs, "merge into raw failed") and raw.get("genres") == ["Action"])
        raw2 = {"title": "X"}
        me._merge_source_into_raw(raw2, "anilist", {"title": "X", "genres": ["Hentai"]},
                                  "anime", True)
        check("merge crash: an anime-DB source that lists it itself keeps it",
              raw2.get("genres") == ["Hentai"])

    with broken_db():
        try:
            me._match_override_ids("radarr:5")
            outcome = "returned"
        except me.TMDBTransientError:
            outcome = "transient"
        except Exception:
            outcome = "other"
    check("owner pin unreadable → transient (item retried), not {} (pin ignored)",
          outcome == "transient")

    def bad_shape(req):
        return httpx.Response(200, json={"data": {"mal_id": 1, "title": "X",
                                                  "genres": [{"nom": "Action"}]}})
    with mock_http(bad_shape), capture(me.logger) as recs:
        r = asyncio.run(me._fetch_jikan(mal_id=1))
    check("Jikan: a parse error is a miss + WARNING, not a TRANSIENT outage",
          r is None and warned(recs, "Jikan response parse failed"))

    def down(req):
        raise httpx.ConnectError("down", request=req)
    with mock_http(down):
        r = asyncio.run(me._fetch_jikan(mal_id=1))
    check("Jikan: a network failure is still TRANSIENT", r is TRANSIENT)

    data = {"title": "X", "reception_checked": True, "relations_checked": True}

    async def sig_bug(*a, **k):
        raise KeyError("bug")

    async def sig_slow(*a, **k):
        raise asyncio.TimeoutError()
    with patched(me, build_verified_data=lambda *a, **k: dict(data), topup_significance=sig_bug):
        with capture(me.logger) as recs:
            asyncio.run(me.ensure_verified_data("X", "movie"))
        crash_warns = warned(recs, "[verified] significance")
    with patched(me, build_verified_data=lambda *a, **k: dict(data), topup_significance=sig_slow):
        with capture(me.logger) as recs:
            asyncio.run(me.ensure_verified_data("X", "movie"))
        timeout_warns = warned(recs, "[verified] significance")
    check("JIT top-up: a crash is a WARNING, a timeout stays DEBUG",
          crash_warns and not timeout_warns)

    src = (ROOT / "src/services/media_enricher.py").read_text(encoding="utf-8")
    check("MediaIdentity merge failures warn on both paths",
          src.count('logger.warning("[enricher] MediaIdentity merge failed') == 2
          and 'logger.debug("[enricher] MediaIdentity merge failed' not in src)


# ── 4. routers/enrichment (deep inside the runners: source pins) ─────────────

def test_enrichment_router_prefilter_and_audit_pins():
    src = (ROOT / "src/routers/enrichment.py").read_text(encoding="utf-8")
    seg = src[src.index("kept, n_dead, n_changed, n_skipped"):
              src.index('"Pre-filter: %d -> %d items')]
    exc = seg[seg.index("except Exception"):]
    check("pre-filter: a failed freshness check keeps the item (not counted as live)",
          "kept.append(i)" in exc[:exc.index("n_skipped += 1")]
          and "pass\n                        n_skipped" not in seg)
    pin = src[src.index("pins: dict = {}"):src.index("mc = MetadataCache()", src.index("pins: dict = {}"))]
    check("audit: an unreadable pin table skips the entity check this run",
          'logger.warning("[audit] pin lookup failed' in pin and "truth = {}" in pin)


# ── 5. library_memory / watch_status ─────────────────────────────────────────

def test_memory_indexes_and_watched_lookup_warn():
    import src.services.library_memory as lm
    import src.services.watch_status as ws
    with broken_db(), capture(lm.logger) as recs:
        seen = lm.seen_index(7)
        removed = lm.removed_index(7)
    check("seen/removed index: same empty shape, but a WARNING",
          seen == {"titles": set(), "tmdb_ids": set()} and removed["titles"] == set()
          and warned(recs, "seen_index failed") and warned(recs, "removed_index failed"))

    import src.routers.recommendations as rr

    async def arr_down(category=None):
        raise httpx.ConnectError("radarr down")
    with patched(rr, _fetch_arr_candidates=arr_down), capture(lm.logger) as recs:
        owned = asyncio.run(lm.owned_index("movie"))
    check("owned index: WARNING on an unreadable library",
          owned["titles"] == set() and warned(recs, "owned_index fetch failed"))

    with broken_db(), capture(ws.logger) as recs:
        w = ws.watched_lookup(7, ["Dune"])
    check("watched_lookup: {} on a DB error is a WARNING ('not watched' is a claim)",
          w == {} and warned(recs, "[watch] lookup failed"))


# ── 6. SoulSync ──────────────────────────────────────────────────────────────

def test_soulsync_failures_visible_and_follow_veto_closed():
    import src.services.soulsync_client as ss
    with patched(settings, SOULSYNC_URL="http://ss.lan", SOULSYNC_API_KEY="k"):
        ss._healthy = True
        with mock_http(lambda req: httpx.Response(500)), capture(ss.logger) as recs:
            v = asyncio.run(ss._get("/watchlist"))
            wl = asyncio.run(ss.watchlist_artists())
        check("SoulSync: a 500 is a WARNING and the watchlist reads None (unknown)",
              v is None and wl is None and warned(recs, "/watchlist -> HTTP 500"))
        ss._healthy = True
        with mock_http(lambda req: httpx.Response(404)), capture(ss.logger) as recs:
            asyncio.run(ss._get("/library/artists/9"))
        check("SoulSync: a 404 is an answer, not a warning", not warned(recs, "404"))
        with mock_http(lambda req: httpx.Response(200, json={"data": {"artists": []}})):
            wl = asyncio.run(ss.watchlist_artists())
        check("SoulSync: an empty watchlist is [] (follows nobody)", wl == [])
    eng = (ROOT / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
    blk = eng[eng.index("_names = await watchlist_artists()"):]
    blk = blk[:blk.index("followed = {")]
    check("deletions: SoulSync configured but silent stops the music run",
          "if _names is None and _ss_on():" in blk
          and "_record_deletion_run(category, stopped=_why)" in blk and "return []" in blk)


# ── 7. Plex playlists / collections ──────────────────────────────────────────

def test_plex_writes_raise_on_transient_errors():
    import src.services.plex_collections as pc
    import src.services.plex_playlists as pp

    async def no_mid(force=False):
        return None

    async def mid(force=False):
        return "MID"
    with patched(pp, _base=lambda: "http://plex.lan", _owner_token=lambda: "tok"):
        with mock_http(lambda req: httpx.Response(503)):
            check("key_exists: Plex 503 raises (was False = 'stale')",
                  raises(lambda: asyncio.run(pp.key_exists("tok", "1"))))
        with mock_http(lambda req: httpx.Response(404)):
            check("key_exists: 404 is a real 'stale'",
                  asyncio.run(pp.key_exists("tok", "1")) is False)

        def sec_down(req):
            raise httpx.ConnectError("down", request=req)
        with mock_http(sec_down):
            check("resolve_video_key: Plex unreachable raises (was None = 'not in Plex')",
                  raises(lambda: asyncio.run(pp.resolve_video_key("Dune", "movie", sections=["1"]))))

        def one_down(req):
            if "/sections/1/" in str(req.url):
                raise httpx.ConnectError("down", request=req)
            return httpx.Response(200, json={"MediaContainer": {"Metadata": [{"ratingKey": 77}]}})
        with mock_http(one_down):
            k = asyncio.run(pp.resolve_artist_key("Daft Punk", sections=["1", "2"]))
        check("resolve_artist_key: a hit in another section still wins", k == "77")

        with patched(pp, get_machine_identifier=no_mid):
            check("create_playlist: unknown server identity raises (was None)",
                  raises(lambda: asyncio.run(pp.create_playlist("tok", "T", ["1"]))))

        with memory_db() as session, patched(pp, _load_snapshot=lambda uid: {},
                                              _save_snapshot=lambda uid, s: None,
                                              get_machine_identifier=mid):
            with session() as db:
                db.add(CachedRecommendation(user_id=1, category="movie", lane="library",
                                            title="Dune", reason="x", confidence=0.9,
                                            cached_at=datetime.utcnow(), plex_rating_key="111"))
                db.add(LibraryConfig(plex_section_key="1", plex_section_title="Movies",
                                     plex_section_type="movie", media_category="movie"))
            user = types.SimpleNamespace(id=1, plex_username="don", plex_token="utok")
            with mock_http(lambda req: httpx.Response(503)):
                try:
                    res = asyncio.run(pp.push_user_playlists(user))
                except Exception:
                    res = "raised"
        check("playlist push: Plex down raises (custodian keeps it due), not {'pushed': []}",
              res == "raised")

    with patched(pc, _owner_token=lambda: "tok", get_machine_identifier=no_mid):
        check("create_collection: unknown server identity raises (was None)",
              raises(lambda: asyncio.run(pc.create_collection("1", 1, "Curatarr · A", ["1"]))))

    async def lc(sec):
        return [{"title": "Curatarr · Old", "ratingKey": "9"}]

    async def dc(key):
        return None

    async def cc(*a, **k):
        raise httpx.ConnectError("plex down")
    state = {}
    designs = [{"section_key": "1", "plex_type": 1, "title": "Curatarr · A", "keys": ["1"]},
               {"section_key": "1", "plex_type": 1, "title": "Curatarr · B", "keys": ["2"]}]
    with patched(pc, list_collections=lc, delete_collection=dc, create_collection=cc), \
            patched(app_state, get_state=state.get, set_state=state.__setitem__):
        res = asyncio.run(pc.push_collections(designs))
    check("push_collections: failed creates are counted", res.get("failed") == 2
          and res["created"] == 0)

    import src.services.collection_designer as cd
    import src.services.data_custodian as dcu

    async def two_designs():
        return designs

    async def half(ds):
        return {"created": 1, "deleted": 2, "failed": 1}
    # A partial push is a FAILURE now (2026-10-19): it stays due AND backs
    # off, instead of re-running the curator design every 30 minutes.
    with patched(cd, design_collections=two_designs), patched(pc, push_collections=half):
        try:
            asyncio.run(dcu._run_collections())
            raised = False
        except dcu.TaskFailed:
            raised = True
    check("custodian collections: fewer shelves than designs → fails (stays due, backs off)",
          raised)


# ── 8. library_sorting ───────────────────────────────────────────────────────

def test_library_sorting_cache_and_partial():
    import src.services.library_sorting as ls
    state = {}

    async def meta(handler):
        async with _RealClient(transport=httpx.MockTransport(handler)) as c:
            return await ls._tmdb_meta(c, 123)

    def down(req):
        raise httpx.ConnectError("x", request=req)
    with patched(ls, get_state=state.get, set_state=state.__setitem__):
        asyncio.run(meta(lambda req: httpx.Response(503)))
        asyncio.run(meta(down))
        not_cached = "tvdb_meta:123" not in state
        asyncio.run(meta(lambda req: httpx.Response(
            200, json={"tv_results": [{"origin_country": ["JP"], "genre_ids": [16]}]})))
    check("lib-sort: a TMDB 503/outage is not cached as 'not anime' forever", not_cached)
    check("lib-sort: a 200 answer is cached", state.get("tvdb_meta:123") == "JP#1")

    def sonarr(req):
        if req.method == "GET":
            return httpx.Response(200, json={"id": 5, "title": "Frieren",
                                             "seriesType": "standard", "genres": []})
        return httpx.Response(202, json=[])

    def local_fails(sid, new_cat, errors=None):
        errors.append("db: database is locked")
        return None

    cw = sys.modules.get("src.vector_store.chromadb_wrapper")
    fake_cw = types.ModuleType("src.vector_store.chromadb_wrapper")

    def no_chroma():
        raise RuntimeError("no chroma in tests")
    fake_cw.get_chroma_db = no_chroma
    sys.modules["src.vector_store.chromadb_wrapper"] = fake_cw
    try:
        with patched(settings, SONARR_URL="http://sonarr.lan", SONARR_API_KEY="k"), \
                patched(ls, _recategorize_local=local_fails), mock_http(sonarr):
            r = asyncio.run(ls.apply_reclassify([{"sonarr_id": 5, "fix": {"seriesType": "anime"}}]))
    finally:
        if cw is not None:
            sys.modules["src.vector_store.chromadb_wrapper"] = cw
        else:
            sys.modules.pop("src.vector_store.chromadb_wrapper", None)
    row = (r.get("results") or [{}])[0]
    check("reclassify: Sonarr accepted + local re-file failed → partial, said so",
          row.get("ok") == "partial" and row.get("partial") is True
          and "local re-file failed" in (row.get("message") or "") and r.get("partial") == 1)


# ── 9. editions ──────────────────────────────────────────────────────────────

def test_editions_keep_flags_and_surface_search_failure():
    import src.services.editions as ed
    stamp = datetime(2026, 9, 1)
    with memory_db() as session:
        with session() as db:
            db.add(SeriesEdition(service="sonarr", arr_id=1, title="Kite", category="anime",
                                 files_disc=0, anidb_uncensored=True, anidb_tags="tv censoring",
                                 checked_at=stamp))

        class Client:
            async def get_series(self):
                return [{"id": 1, "title": "Kite", "seriesType": "anime", "year": 1998,
                         "genres": ["Anime"]}]

            async def get_episode_files(self, sid):
                return []

        def snapshot_down(**kw):
            raise sqlite3.OperationalError("no such table: offline_anime")
        res = asyncio.run(ed.sync_editions(Client(), lookup=snapshot_down,
                                           get_state=lambda k: None,
                                           set_state=lambda k, v: None))
        with session() as db:
            row = db.query(SeriesEdition).filter(SeriesEdition.arr_id == 1).one()
            kept = (row.anidb_uncensored, row.anidb_tags, row.checked_at)
    check("editions: an AniDB failure keeps the stored flags and does not stamp",
          kept == (True, "tv censoring", stamp) and res["failed"] == 1)

    class AllDown:
        async def search_releases(self, sid, season):
            raise httpx.ConnectError("indexers down")
    check("editions: every season search failing raises (router → 502), not '0 releases'",
          raises(lambda: asyncio.run(ed.check_releases(AllDown(), 1))))

    class OneDown:
        async def search_releases(self, sid, season):
            if season == 1:
                raise httpx.ConnectError("flaky")
            return [{"title": "Kite S02 BluRay", "indexer": "x", "quality": {}}]
    out = asyncio.run(ed.check_releases(OneDown(), 1))
    check("editions: one season failing still returns the other's offers", isinstance(out, list))


# ── 10. external_ids ─────────────────────────────────────────────────────────

def test_external_ids_harvest_aborts_on_partial_index():
    import src.cache.metadata_cache as mcm
    import src.services.arr_client as ac
    import src.services.external_ids as ei

    class BadSonarr:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise httpx.ConnectError("sonarr down")

        async def __aexit__(self, *a):
            return False

    class GoodRadarr:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get_movies(self):
            return [{"title": "Foo", "year": 2020, "imdbId": "tt1", "tmdbId": 5}]

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE api_cache (cache_key TEXT, response TEXT, expires_at TEXT)")
    db.execute("INSERT INTO api_cache VALUES (?,?,?)",
               ("v2:raw:anime:foo", json.dumps({"title": "Foo", "media_type": "anime",
                                                "year": 2020}), "2999-01-01"))
    cache = types.SimpleNamespace(conn=db)
    writes = []
    with patched(ac, SonarrClient=BadSonarr, RadarrClient=GoodRadarr), \
            patched(settings, SONARR_URL="http://s.lan", SONARR_API_KEY="k",
                    RADARR_URL="http://r.lan", RADARR_API_KEY="k"), \
            patched(mcm, write_fields=lambda *a, **k: writes.append(a)), \
            capture(ei.logger) as recs:
        try:
            res = asyncio.run(ei.harvest(cache))
        except Exception as e:  # noqa: BLE001
            res = {"crash": repr(e)}
    check("external ids: Sonarr down aborts the harvest (no film claims a series' id)",
          res.get("error") and res.get("added") == 0 and not writes
          and warned(recs, "tv unavailable"))


# ── 11. music_metadata ───────────────────────────────────────────────────────

def test_enrich_artist_negative_cache_and_keys():
    import src.services.music_metadata as mm
    import src.services.soulsync_client as ss
    from src.services.enrichment_state import TRANSIENT
    store = {}

    class FakeMC:
        def get_cache(self, k):
            return store.get(k)

        def set_cache(self, k, v, days=None):
            store[k] = {"response": v, "days": days}

        def close(self):
            pass

    async def mb_down(name, mbid=None):
        return TRANSIENT

    async def mb_miss(name, mbid=None):
        return None

    async def lfm_none(name):
        return None

    async def lfm_hit(name):
        return {"genres": ["doom"], "tags": ["doom"], "similar_artists": [], "bio": "",
                "listeners": 5}

    async def ss_none(name):
        return None

    async def ss_crash(name):
        raise TypeError("bug")
    with patched(mm, MetadataCache=FakeMC, fetch_musicbrainz_artist=mb_down,
                 fetch_lastfm_artist=lfm_none), patched(ss, artist_info=ss_none):
        r = asyncio.run(mm.enrich_artist("Solstice"))
    # Since the #161 integration the verdict is TRANSIENT itself (falsy, so
    # every "no profile" caller still reads it as nothing), which lets the
    # music enricher leave the row for the next run instead of writing it off.
    check("music: a TRANSIENT MusicBrainz is not negative-cached for 7 days",
          r is TRANSIENT and not store)

    with patched(mm, MetadataCache=FakeMC, fetch_musicbrainz_artist=mb_miss,
                 fetch_lastfm_artist=lfm_hit), patched(ss, artist_info=ss_crash), \
            capture(mm.logger) as recs:
        p = asyncio.run(mm.enrich_artist("Solstice", mbid="abc"))
    check("music: the profile is written under the mbid key it is read from",
          p and "artist_profile:abc" in store and store["artist_profile:abc"]["response"] == p)
    check("music: a SoulSync crash is a WARNING", warned(recs, "SoulSync artist_info crashed"))


# ── 12. raw_refresh ──────────────────────────────────────────────────────────

def test_raw_refresh_discovery_is_per_row():
    import src.cache.metadata_cache as mcm
    import src.routers.enrichment as er
    import src.services.raw_refresh as rf

    class FakeMC:
        def __init__(self):
            self.conn = sqlite3.connect(":memory:")
            self.conn.execute("CREATE TABLE api_cache (cache_key TEXT, response TEXT, "
                              "expires_at TEXT)")

        def get_cache(self, k):
            return None

        def close(self):
            pass

    calls = []

    async def one(raw, truth=None):
        calls.append(raw["title"])
        if raw["title"] == "A":
            raise KeyError("shape")
        return True

    async def no_arr(cats):
        return []
    state = {}
    with memory_db() as session:
        with session() as db:
            for i, t in enumerate(("A", "B"), 1):
                db.add(CachedRecommendation(user_id=1, category="movie", lane="discovery",
                                            title=t, reason="x", confidence=0.5, tmdb_id=i))
        with patched(mcm, MetadataCache=FakeMC), patched(rf, _refresh_one=one), \
                patched(er, _collect_arr_items=no_arr), \
                patched(app_state, get_state=state.get, set_state=state.__setitem__):
            asyncio.run(rf.run_raw_refresh())
    check("raw-refresh: one crashing discovery row does not end the pass",
          set(calls) == {"A", "B"})


# ── 13. spotify_import ───────────────────────────────────────────────────────

def test_spotify_bad_timestamp_skipped_and_counted():
    import src.services.spotify_import as si
    tmp = Path(tempfile.mkdtemp())
    rows = [{"ts": "garbage", "ms_played": 60000, "master_metadata_track_name": "T1",
             "master_metadata_album_artist_name": "A", "spotify_track_uri": "spotify:track:1"},
            {"ts": "2024-01-02T03:04:05Z", "ms_played": 60000,
             "master_metadata_track_name": "T2", "master_metadata_album_artist_name": "A",
             "spotify_track_uri": "spotify:track:2"}]
    (tmp / "Streaming_History_Audio_2024.json").write_text(json.dumps(rows), encoding="utf-8")
    with memory_db() as session:
        with session() as db:
            db.merge(User(id=1, plex_user_id="p1", plex_username="don"))
        stats = si.run_import(1, tmp)
    check("spotify: an unparseable timestamp is skipped and counted, not faked as now",
          stats.get("imported") == 1 and stats.get("bad_timestamp") == 1)


# ── 14. routers/music ────────────────────────────────────────────────────────

def test_music_pipeline_lock_released_when_setup_fails():
    import src.routers.music as mr
    state = {"music_pipeline_running": "1"}

    def set_state(k, v):
        if k == "music_pipeline_interrupted" and v == "1":
            raise sqlite3.OperationalError("database is locked")
        state[k] = v
    import src.services.state_lease as sl

    def release(key, db_path=None):      # the #161 lease: compare-and-clear
        state[key] = "0"
        return True
    with patched(mr, set_state=set_state, get_state=state.get), patched(sl, release=release):
        try:
            asyncio.run(mr._run_music_pipeline(1, 10))
        except Exception:
            pass
    check("music pipeline: a locked DB during setup still releases the running flag",
          state.get("music_pipeline_running") == "0")


if __name__ == "__main__":
    logging.getLogger().addHandler(logging.NullHandler())
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                FAIL += 1
                print(f"  FAIL  {name} crashed: {type(e).__name__}: {e}")
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
