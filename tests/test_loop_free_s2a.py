"""Big arr listings, Chroma id scans and the poster cache stay off the loop.

The server runs on one event loop. These paths still blocked it:

  * _get_arr_counts parsed the full Radarr / Sonarr / Lidarr listings
    (15-30 MB of JSON) on the loop and ran three Chroma id scans there; now
    each listing is parsed AND reduced to counts in a worker, and the ids
    are fetched once, on the Chroma thread
  * library_breakdown's Chroma prefix count runs on the Chroma thread
  * the audit's zombie walk pages the whole collection on the Chroma thread
  * MediaService.request(parse_in_thread=True) decodes the listings
    (get_movies / get_series / get_artists) in a worker; _collect_arr_items
    and /mapping-coverage parse theirs in a worker too
  * the image proxy's cache write and its every-25th budget sweep run in a
    worker, and the sweep counter stays exact under concurrent misses
  * library_configure's write_env (icacls on Windows) runs in a worker

Each "off the loop" check fails on the code before this change.

    python tests/test_loop_free_s2a.py
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

from src.config import settings  # noqa: E402
settings.ENRICHMENT_CACHE = _TMP / "enrichment.db"

if not settings.effective_jwt_secret:
    from pydantic import SecretStr
    settings.JWT_SECRET = SecretStr("test-only-secret-never-shipped-32-bytes-min-per-rfc7518")

import httpx  # noqa: E402

import src.database.connection as conn_mod  # noqa: E402
from src.database.connection import get_db_session  # noqa: E402
from src.database.models import (ArrEnrichmentStatus, Base, EnrichmentStatus,  # noqa: E402
                                 LibraryConfig, User)
from src.services import app_state  # noqa: E402

Base.metadata.create_all(conn_mod.engine)

import src.routers.enrichment as en  # noqa: E402
import src.routers.image_proxy as ip  # noqa: E402
import src.routers.library as lib  # noqa: E402
import src.services.arr_client as ac  # noqa: E402
import src.vector_store.chromadb_wrapper as cw  # noqa: E402

from _checks import check  # noqa: E402


def _on_loop() -> bool:
    # asyncio.run drives the loop on the main thread.
    return threading.current_thread() is threading.main_thread()


# ── spies ───────────────────────────────────────────────────────────────────
MARK = "_s2a_listing"          # every fake listing carries it
_parses: list = []             # one bool per listing parse: True = on the loop
_real_loads = json.loads


def _spy_loads(s, *a, **kw):
    raw = s if isinstance(s, (bytes, bytearray)) else str(s).encode()
    if MARK.encode() in raw:
        _parses.append(_on_loop())
    return _real_loads(s, *a, **kw)


json.loads = _spy_loads        # httpx's Response.json() looks it up per call


def _parsed_off_loop(n: int) -> bool:
    return len(_parses) == n and not any(_parses)


MOVIES = [{"id": 1, "title": "Heat", "hasFile": True, "monitored": True, "tmdbId": 949,
           "imdbId": "tt0113277", "year": 1995, "note": MARK},
          {"id": 2, "title": "Ronin", "hasFile": True, "monitored": False, "tmdbId": 8195,
           "year": 1998},
          {"id": 3, "title": "Thief", "hasFile": False, "monitored": False}]
SERIES = [{"id": 10, "title": "Dark", "monitored": True, "tvdbId": 334824, "tmdbId": 70523,
           "year": 2017, "seriesType": "standard",
           "statistics": {"episodeFileCount": 26}, "note": MARK},
          {"id": 11, "title": "Mushishi", "monitored": True, "tvdbId": 79735, "year": 2005,
           "seriesType": "anime", "genres": ["Anime"], "statistics": {"episodeFileCount": 26}},
          {"id": 12, "title": "Planned", "monitored": False, "tvdbId": 1,
           "statistics": {"episodeFileCount": 0}, "genres": ["Anime"]}]
ARTISTS = [{"id": 100, "artistName": "Boards of Canada", "monitored": True,
            "foreignArtistId": "mb-boc",
            "statistics": {"trackFileCount": 40, "albumCount": 4}, "note": MARK},
           {"id": 101, "artistName": "Autechre", "monitored": False,
            "statistics": {"trackFileCount": 0, "albumCount": 12}}]


def _arr_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/api/v3/movie"):
        return httpx.Response(200, json=MOVIES)
    if path.endswith("/api/v3/series"):
        return httpx.Response(200, json=SERIES)
    if path.endswith("/api/v1/artist"):
        return httpx.Response(200, json=ARTISTS)
    return httpx.Response(404)


_RealAsyncClient = httpx.AsyncClient
_handler = [_arr_handler]


class _MockClient(_RealAsyncClient):
    def __init__(self, *a, **kw):
        kw.pop("verify", None)
        kw["transport"] = httpx.MockTransport(lambda req: _handler[0](req))
        super().__init__(*a, **kw)


httpx.AsyncClient = _MockClient

settings.RADARR_URL, settings.RADARR_API_KEY = "http://radarr.test", "k"
settings.SONARR_URL, settings.SONARR_API_KEY = "http://sonarr.test", "k"
settings.LIDARR_URL, settings.LIDARR_API_KEY = "http://lidarr.test", "k"


class _Collection:
    def __init__(self, docs):
        self.docs = docs          # [(id, metadata)]
        self.calls = []           # True = on the loop

    def get(self, include=None, limit=None, offset=0, **kw):
        self.calls.append(_on_loop())
        if limit is None:
            rows = self.docs
        else:
            rows = self.docs[offset:offset + limit]
        return {"ids": [d for d, _ in rows], "metadatas": [m for _, m in rows]}


class _Chroma:
    """Stands in for ChromaDBWrapper: records the thread of every call."""

    def __init__(self, docs):
        self.collection = _Collection(docs)
        self.calls = []           # (method, on_loop)

    def all_ids(self):
        self.calls.append(("all_ids", _on_loop()))
        return [d for d, _ in self.collection.docs]

    def count_by_id_prefix(self, prefix):
        self.calls.append(("count_by_id_prefix", _on_loop()))
        return sum(1 for d, _ in self.collection.docs if d.startswith(prefix))


DOCS = [("radarr:1", {}), ("radarr:2", {}), ("sonarr:10", {}), ("sonarr:11", {}),
        ("sonarr:12", {}), ("lidarr:100", {}), ("radarr:1::facet", {}), ("plex:5", {})]
_real_get_chroma = cw.get_chroma_db

# ── 1. MediaService.request(parse_in_thread=True) ───────────────────────────
print("arr_client listings")
from aiohttp import web  # noqa: E402

_decodes: list = []
_real_decode = getattr(ac, "_decode_json_body", None)


def _spy_decode(body, encoding):
    if MARK.encode() in body:
        _decodes.append(_on_loop())
    return _real_decode(body, encoding)


if _real_decode is not None:
    ac._decode_json_body = _spy_decode


async def _arr_server_run():
    app = web.Application()
    app.router.add_get("/api/v3/movie", lambda r: web.json_response(MOVIES))
    app.router.add_get("/api/v3/series", lambda r: web.json_response(SERIES))
    app.router.add_get("/api/v1/artist", lambda r: web.json_response(ARTISTS))
    app.router.add_get("/text", lambda r: web.Response(text=json.dumps(MOVIES)))
    app.router.add_get("/empty", lambda r: web.Response(body=b"  ",
                                                        content_type="application/json"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    out = {}
    try:
        async with ac.RadarrClient(base, "k") as c:
            out["movies"] = await c.get_movies()
            for path in ("/text", "/empty"):
                for flag in (False, True):
                    kw = {"parse_in_thread": True} if flag else {}
                    try:
                        out[(path, flag)] = ("ok", await c.request("GET", path, **kw))
                    except Exception as e:  # noqa: BLE001 - compared below
                        out[(path, flag)] = ("err", type(e).__name__)
        async with ac.SonarrClient(base, "k") as c:
            out["series"] = await c.get_series()
        async with ac.LidarrClient(base, "k") as c:
            out["artists"] = await c.get_artists()
    finally:
        await runner.cleanup()
    return out


_parses.clear()
res = asyncio.run(_arr_server_run())
check("movies / series / artists come back unchanged",
      res["movies"] == MOVIES and res["series"] == SERIES and res["artists"] == ARTISTS)
check(f"the three listings are decoded off the loop ({_decodes})",
      len(_decodes) == 3 and not any(_decodes))
check("a non-JSON content type raises the same error with or without the flag",
      res[("/text", False)] == res[("/text", True)] == ("err", "ContentTypeError"))
check("a blank body is None either way",
      res[("/empty", False)] == res[("/empty", True)] == ("ok", None))

# ── 2. _get_arr_counts ──────────────────────────────────────────────────────
print("_get_arr_counts")
with get_db_session() as db:
    for aid in (1, 99):
        db.add(ArrEnrichmentStatus(service="radarr", arr_id=aid, category="movie",
                                   title=f"m{aid}", enriched=True))
    db.add(ArrEnrichmentStatus(service="sonarr", arr_id=10, category="show",
                               title="Dark", enriched=True))
    db.add(ArrEnrichmentStatus(service="lidarr", arr_id=7, category="album",
                               title="Geogaddi", enriched=True))
    db.add(EnrichmentStatus(plex_rating_key="m1", title="boards of canada",
                            media_category="music", enriched=True))
    db.commit()
app_state.set_state("arr_counts_ts", "")

chroma = _Chroma(DOCS)
cw.get_chroma_db = lambda: chroma
_parses.clear()
try:
    counts = asyncio.run(en._get_arr_counts())
finally:
    cw.get_chroma_db = _real_get_chroma
check(f"the three listings are parsed off the loop ({_parses})", _parsed_off_loop(3))
check(f"the vector counts come from ONE id fetch, off the loop ({chroma.calls})",
      len(chroma.calls) == 1 and not chroma.calls[0][1])
check("radarr counts unchanged", counts.get("radarr") == {
    "total": 3, "downloaded": 2, "monitored": 1, "enriched": 1, "orphaned": 1,
    "vector_count": 3, "pct": 50, "stale": False})
check("sonarr counts unchanged", counts.get("sonarr") == {
    "total": 3, "downloaded": 2, "monitored": 2, "enriched": 1, "orphaned": 0,
    "vector_count": 3, "pct": 50, "stale": False})
check("lidarr counts unchanged", counts.get("lidarr") == {
    "total": 2, "total_albums": 16, "downloaded": 1, "monitored": 1, "enriched": 1,
    "enriched_albums": 1, "vector_count": 1, "pct": 50, "stale": False})
check("and cached for the quick path",
      json.loads(app_state.get_state("arr_counts_cache") or "{}") == counts)


def _broken():
    raise RuntimeError("chroma down")


cw.get_chroma_db = _broken
app_state.set_state("arr_counts_ts", "")
try:
    counts = asyncio.run(en._get_arr_counts())
finally:
    cw.get_chroma_db = _real_get_chroma
check("Chroma down: vector counts read 0, the rest still fills",
      [counts[s]["vector_count"] for s in ("radarr", "sonarr", "lidarr")] == [0, 0, 0]
      and counts["radarr"]["total"] == 3)

_handler[0] = lambda req: httpx.Response(200, content=b"{not json " + MARK.encode())
app_state.set_state("arr_counts_ts", "")
cw.get_chroma_db = lambda: _Chroma(DOCS)
try:
    stale = asyncio.run(en._get_arr_counts())
finally:
    cw.get_chroma_db = _real_get_chroma
    _handler[0] = _arr_handler
check("a broken listing still falls back to the previous counts, marked stale",
      stale["radarr"]["stale"] is True and stale["radarr"]["total"] == 3
      and "stale_reason" in stale["radarr"])

# ── 3. _collect_arr_items ───────────────────────────────────────────────────
print("_collect_arr_items")
_parses.clear()
items = asyncio.run(en._collect_arr_items(["movie", "show", "anime", "music"]))
check(f"the three listings are parsed off the loop ({_parses})", _parsed_off_loop(3))
check("the item rows are unchanged",
      [(i["plex_rating_key"], i["media_type"]) for i in items]
      == [("radarr:1", "movie"), ("radarr:2", "movie"), ("sonarr:10", "show"),
          ("sonarr:11", "anime"), ("lidarr:100", "music"), ("lidarr:101", "music")]
      and items[0]["imdb_id"] == "tt0113277" and items[3]["sonarr_series_type"] == "anime"
      and items[4]["mbid"] == "mb-boc")
only_anime = asyncio.run(en._collect_arr_items(["anime"]))
check("the category filter still applies", [i["plex_rating_key"] for i in only_anime]
      == ["sonarr:11"])

# ── 4. /mapping-coverage ────────────────────────────────────────────────────
print("check_mapping_coverage")
import src.services.anime_mapping as am  # noqa: E402

_real_mapping = am.get_anime_mapping


async def _mapping(force_refresh=False):
    return types.SimpleNamespace(tvdb_to_anidb={79735: 42}, anidb_to_anilist={42: 420})


am.get_anime_mapping = _mapping
_parses.clear()
try:
    cov = asyncio.run(en.check_mapping_coverage(user=None))
finally:
    am.get_anime_mapping = _real_mapping
check(f"the series listing is parsed off the loop ({_parses})", _parsed_off_loop(1))
check("the coverage numbers are unchanged",
      cov["total_anime"] == 2 and cov["covered"] == 1 and cov["missing"] == 1
      and cov["anilist_resolved"] == 1 and cov["covered_sample"][0]["anilist_id"] == 420)

# ── 5. library_breakdown's Chroma count ─────────────────────────────────────
print("library_breakdown")
with get_db_session() as db:
    db.add(LibraryConfig(plex_section_key="1", plex_section_title="Films",
                         plex_section_type="movie", media_category="movie"))
    db.commit()
_U = User(id=1, plex_username="owner", is_admin=True)
chroma = _Chroma(DOCS)
_saved = (cw.get_chroma_db, settings.PLEX_TOKEN, settings.PLEX_AUTH_TOKEN)
cw.get_chroma_db = lambda: chroma
settings.PLEX_TOKEN, settings.PLEX_AUTH_TOKEN = None, ""
try:
    out = asyncio.run(lib.library_breakdown(user=_U, quick=True))
finally:
    cw.get_chroma_db, settings.PLEX_TOKEN, settings.PLEX_AUTH_TOKEN = _saved
check(f"the prefix count runs off the loop ({chroma.calls})",
      chroma.calls == [("count_by_id_prefix", False)])
vs = out["libraries"][0]["vector_store"]
check("the vector-store block is unchanged",
      vs["indexed"] == 3 and "``radarr:``" in vs["basis"])

# ── 6. _audit_enrichments' zombie walk ──────────────────────────────────────
print("_audit_enrichments zombie walk")
zdocs = [("radarr:1", {"domain": "movie", "title": "Heat"}),
         ("radarr:2", {"domain": "movie", "title": "Ronin"}),
         ("radarr:3", {"domain": "movie", "title": "Gone"}),
         ("radarr:1::facet", {}), ("plex:9", {})]
zdocs += [(f"sonarr:{n}", {"domain": "show", "title": f"S{n}"}) for n in range(1, 1201)]
chroma = _Chroma(zdocs)


async def _truth(cats):
    return [{"plex_rating_key": "radarr:1"}, {"plex_rating_key": "radarr:2"}]


_real_collect = en._collect_arr_items
en._collect_arr_items = _truth
cw.get_chroma_db = lambda: chroma
try:
    audit = asyncio.run(en._audit_enrichments(dry_run=True))
finally:
    en._collect_arr_items = _real_collect
    cw.get_chroma_db = _real_get_chroma
calls = chroma.collection.calls
check(f"the collection is paged off the loop ({len(calls)} pages, on loop: {sum(calls)})",
      len(calls) == 4 and not any(calls))
z = audit["zombies"]
check(f"classification and the stale guard are unchanged ({z})",
      z["docs_walked"] == 1203 and z["arr_live_requeued"] == 2 and z["arr_gone"] == 1
      and z["guard_skipped_sonarr"] == 1200 and z["rebuilt"] == 0)

# ── 7. image proxy cache write + sweep ──────────────────────────────────────
print("image proxy")
_cache = _TMP / "images"
_cache.mkdir()
_writes: list = []
_sweeps: list = []
_real_write_bytes = Path.write_bytes
_real_enforce = ip._enforce_cache_budget
_real_sweep = getattr(ip, "_sweep_cache", None)


def _spy_write_bytes(self, data):
    if _cache in self.parents:
        _writes.append(_on_loop())
    return _real_write_bytes(self, data)


def _spy_enforce():
    _sweeps.append(("check", _on_loop()))
    return _real_enforce()


def _spy_sweep():
    _sweeps.append(("sweep", _on_loop()))
    return _real_sweep()


async def _public(host, resolver=None):
    return True


_JPEG = b"\xff\xd8\xff" + b"x" * 2048
_handler[0] = lambda req: httpx.Response(200, content=_JPEG,
                                         headers={"content-type": "image/jpeg"})
_saved_ip = (ip._CACHE_DIR, ip._SWEEP_EVERY, ip._writes_since_sweep, ip._host_resolves_public)
ip._CACHE_DIR, ip._SWEEP_EVERY, ip._writes_since_sweep = _cache, 5, 0
ip._host_resolves_public = _public
Path.write_bytes = _spy_write_bytes
ip._enforce_cache_budget = _spy_enforce
if _real_sweep is not None:
    ip._sweep_cache = _spy_sweep


async def _ten_misses():
    return await asyncio.gather(*[
        ip.proxy_image(src=f"https://image.tmdb.org/t/p/w500/p{n}.jpg") for n in range(10)])


try:
    responses = asyncio.run(_ten_misses())
finally:
    Path.write_bytes = _real_write_bytes
    ip._enforce_cache_budget = _real_enforce
    if _real_sweep is not None:
        ip._sweep_cache = _real_sweep
    (ip._CACHE_DIR, ip._SWEEP_EVERY, counter_after, ip._host_resolves_public) = (
        _saved_ip[0], _saved_ip[1], ip._writes_since_sweep, _saved_ip[3])
    ip._writes_since_sweep = _saved_ip[2]
    _handler[0] = _arr_handler
check(f"every cache write runs off the loop ({_writes})", len(_writes) == 10 and not any(_writes))
check("every budget check runs off the loop",
      len([s for s in _sweeps if s[0] == "check"]) == 10 and not any(s[1] for s in _sweeps))
check(f"ten concurrent misses with a sweep every 5 writes: exactly 2 sweeps, counter at 0 "
      f"({[s for s in _sweeps if s[0] == 'sweep']}, {counter_after})",
      len([s for s in _sweeps if s[0] == "sweep"]) == 2 and counter_after == 0)
check("each miss is served from its cache file",
      all(Path(r.path).read_bytes() == _JPEG for r in responses)
      and len(list(_cache.iterdir())) == 10)

# ── 8. library_configure's write_env ────────────────────────────────────────
print("library_configure")
import src.services.setup_wizard as sw  # noqa: E402

_env_calls: list = []
_saved_cfg = (lib.library_test, sw.write_env, sw.current_env_config, lib.settings)


async def _ok(req, _user=None):
    return {"ok": True, "version": "5.0"}


def _write_env(cfg):
    _env_calls.append((_on_loop(), dict(cfg)))
    if cfg.get("radarr_api_key") == "bad\nkey":
        raise ValueError("line break")


lib.library_test = _ok
sw.write_env, sw.current_env_config = _write_env, (lambda: {"plex_url": "http://p"})
lib.settings = types.SimpleNamespace()     # settings.__init__() must not reload the real one
try:
    done = asyncio.run(lib.library_configure(
        lib.ArrConfigureRequest(service="radarr", url="http://radarr.lan:7878", api_key="k2"),
        _user=_U))
    try:
        asyncio.run(lib.library_configure(
            lib.ArrConfigureRequest(service="radarr", url="http://radarr.lan:7878",
                                    api_key="bad\nkey"), _user=_U))
        refused = None
    except lib.HTTPException as e:
        refused = e.status_code
finally:
    lib.library_test, sw.write_env, sw.current_env_config, lib.settings = _saved_cfg
check(f"write_env runs off the loop ({[c[0] for c in _env_calls]})",
      len(_env_calls) == 2 and not any(c[0] for c in _env_calls))
check("with the overlaid config, and the same answer",
      _env_calls[0][1] == {"plex_url": "http://p", "radarr_url": "http://radarr.lan:7878",
                           "radarr_api_key": "k2"}
      and done == {"ok": True, "service": "radarr", "version": "5.0"})
check("a value .env must not hold is still a 400", refused == 400)

json.loads = _real_loads
httpx.AsyncClient = _RealAsyncClient
check.done()
