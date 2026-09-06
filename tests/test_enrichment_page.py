"""Enrichment healing, step 2 — the page behind the numbers.

Pins: match score / basis / three-way decision, the Needs-attention reasons,
the negative pin ("Not this one") through every resolver and the cache,
category derivation (never a bare "movie" for a series), a purge that covers
every key shape a wrong profile may hide under, and the endpoint / frontend
wiring.

    python tests/test_enrichment_page.py
"""
import asyncio
import pathlib
import sys
from contextlib import contextmanager

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.services import enrichment_state as es


def test_match_score_bands_and_decision():
    assert es.match_score("Blade Runner 2049", 2017, "Blade Runner 2049", 2017) == 1.0
    assert es.match_score("Blown Away", 1994, "Blown Away", 1992) == 0.8, "same name, 2 y off: penalised"
    lupin = es.match_score("Lupin III", 1971, "Lupin the Third: The Woman Called Fujiko Mine", 2012)
    assert lupin < 0.5 and es.match_decision(lupin, 1971, 2012) == "refuse"
    assert es.match_decision(0.3, 1999, 1999) == "review", "same-year odd name (localised title) is kept for review"
    assert es.match_decision(0.3, None, 1999) == "refuse", "unknown year + far title: no match beats a wrong match"
    assert es.match_decision(0.6) == "review" and es.match_decision(0.9) == "accept"
    assert es.match_score("Der Pate", 1972, "The Godfather", 1972, alt_titles=("Der Pate",)) == 1.0, \
        "the hit's alternative names count"


def test_match_basis():
    B = es.resolve_match_basis
    assert B("movie", {"tmdb_id": 1}, {}, {"tmdb_id": 1}) == "pin"
    assert B("movie", {}, {"tmdb_id": 1}, {"tmdb_id": 1}) == "arr_id"
    assert B("movie", {}, {"tmdb_id": None}, {"tmdb_id": 1}) == "identity"
    assert B("movie", {}, {}, {}) == "title_search"
    assert B("anime", {}, {"tvdb_id": 5}, {"tvdb_id": 5, "anilist_id": 9}) == "arr_id"
    assert B("music", {}, {}, {"mbid": "x"}) == "identity"
    assert B("music", {"_rejected": {"mbid": {"bad"}}}, {}, {}) == "title_search", \
        "a negative-only pin is not a resolution basis"


def test_attention_reasons():
    A = es.attention_reasons
    assert A("not_found", "Not found: year_mismatch", 1) == ["year_mismatch"]
    assert A("retry_due", "Not found: low_confidence", 0) == ["low_confidence"]
    assert A("not_found", "Not found: no_source_data", 2) == ["not_found_repeatedly"]
    assert A("not_found", "Not found: no_source_data", 1) == []
    assert A("enriched", None, 0, "title_search", 0.62) == ["needs_review"]
    assert A("enriched", None, 0, "title_search", 0.9) == []
    assert A("enriched", None, 0, "arr_id", 0.3) == [], "id-based matches are never 'unsure'"
    assert set(es.ATTENTION_ORDER) == set(es.ATTENTION_REASONS)


def test_parse_rejected_ids():
    assert es.parse_rejected_ids('[{"tmdb_id": "603"}, {"mbid": " x "}, {"tmdb_id": 7}]') == \
        {"tmdb_id": {603, 7}, "mbid": {"x"}}
    assert es.parse_rejected_ids(None) == {} and es.parse_rejected_ids("garbage") == {}
    assert es.parse_rejected_ids([{"tmdb_id": "abc"}]) == {}


# ── negative pin through the resolvers ───────────────────────────────────────

class _Resp:
    def __init__(self, status, payload=None):
        self.status_code, self._p, self.headers = status, payload or {}, {}

    def json(self):
        return self._p


def test_fetch_source_treats_rejected_ids_as_absent():
    import src.services.media_enricher as me
    calls = []

    async def full(tmdb_id, endpoint):
        calls.append(("full", tmdb_id))
        return {"title": "x"}

    async def search(title, endpoint, year=None, rejected=None):
        calls.append(("search", title, set(rejected or ())))
        return {"title": "y"}
    real = (me.fetch_tmdb_full, me._tmdb_search_and_fetch)
    me.fetch_tmdb_full, me._tmdb_search_and_fetch = full, search
    try:
        asyncio.run(me._fetch_source("tmdb", {"title": "Good Boy", "media_type": "movie",
                                              "tmdb_id": 111, "rejected": {"tmdb_id": {111}}}))
        assert calls == [("search", "Good Boy", {111})], calls   # the arr's own id was excluded
        calls.clear()
        asyncio.run(me._fetch_source("tmdb", {"title": "Good Boy", "media_type": "movie",
                                              "tmdb_id": 222, "rejected": {"tmdb_id": {111}}}))
        assert calls == [("full", 222)]
    finally:
        me.fetch_tmdb_full, me._tmdb_search_and_fetch = real


def test_tmdb_search_skips_rejected_candidates():
    import src.services.media_enricher as me
    from src.config import settings
    results = [{"id": 1, "title": "Good Boy", "release_date": "2025-10-01"},
               {"id": 2, "title": "Good Boy", "release_date": "2025-12-01"}]

    async def get(client, path, params=None):
        return {"results": results}
    picked = []

    async def full(tmdb_id, mt):
        picked.append(tmdb_id)
        return {"tmdb_id": tmdb_id}
    real = (me._tmdb_get, me.fetch_tmdb_full, settings.TMDB_API_KEY)
    me._tmdb_get, me.fetch_tmdb_full = get, full
    settings.TMDB_API_KEY = "k"
    try:
        asyncio.run(me._tmdb_search_and_fetch("Good Boy", "movie", year=2025))
        assert picked == [1]
        picked.clear()
        asyncio.run(me._tmdb_search_and_fetch("Good Boy", "movie", year=2025, rejected={1}))
        assert picked == [2], "the excluded twin is never picked again"
    finally:
        me._tmdb_get, me.fetch_tmdb_full, settings.TMDB_API_KEY = real


def test_anilist_and_jikan_skip_rejected():
    import src.services.media_enricher as me
    media = [{"id": 1, "title": {"english": "Golden Boy"}, "startDate": {"year": 1995}},
             {"id": 2, "title": {"english": "Golden Boy"}, "startDate": {"year": 1995}}]

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp(200, {"data": {"Page": {"media": media}}})

        async def get(self, url, params=None):
            if url.endswith("/anime"):
                return _Resp(200, {"data": [{"mal_id": 1, "title": "Golden Boy"},
                                            {"mal_id": 2, "title": "Golden Boy"}]})
            mal = int(url.rstrip("/").split("/")[-2])
            return _Resp(200, {"data": {"mal_id": mal, "title": "Golden Boy", "year": 1995,
                                        "synopsis": "s", "genres": [], "themes": [],
                                        "demographics": [], "explicit_genres": [], "studios": []}})

    async def _nowait():
        return None
    real = (me.httpx.AsyncClient, me._anilist_wait)
    me.httpx.AsyncClient, me._anilist_wait = Client, _nowait
    try:
        assert asyncio.run(me.search_anilist_by_title("Golden Boy"))["anilist_id"] == 1
        assert asyncio.run(me.search_anilist_by_title("Golden Boy", rejected={1}))["anilist_id"] == 2
        assert asyncio.run(me.fetch_jikan_data(title="Golden Boy"))["mal_id"] == 1
        assert asyncio.run(me.fetch_jikan_data(title="Golden Boy", rejected={1}))["mal_id"] == 2
    finally:
        me.httpx.AsyncClient, me._anilist_wait = real


def test_musicbrainz_skips_rejected_and_replaces_a_wrong_cached_hit():
    import src.services.music_metadata as mm

    class Cache:
        store: dict = {}

        def __init__(self):
            pass

        def get_cache(self, key):
            return Cache.store.get(key)

        def set_cache(self, key, value, days=None):
            Cache.store[key] = {"response": value}

        def close(self):
            pass
    calls = []

    async def mb(client, url, params):
        calls.append(url)
        if url.endswith("/artist"):
            return _Resp(200, {"artists": [{"id": "bad"}, {"id": "good"}]})
        return _Resp(200, {"name": "Solstice", "tags": [], "genres": []})
    real = (mm._mb_request, mm.MetadataCache)
    mm._mb_request, mm.MetadataCache = mb, Cache
    try:
        Cache.store = {"mb:artist:solstice": {"response": {"mbid": "bad", "name": "Solstice"}}}
        r = asyncio.run(mm.fetch_musicbrainz_artist("Solstice", rejected={"bad"}))
        assert r["mbid"] == "good", r
        assert any(u.endswith("/artist/good") for u in calls)
        assert Cache.store["mb:artist:solstice"]["response"]["mbid"] == "good", \
            "the cached wrong hit was re-searched and replaced"
        assert asyncio.run(mm.fetch_musicbrainz_artist("Solstice"))["mbid"] == "good", "cache hit"
    finally:
        mm._mb_request, mm.MetadataCache = real


# ── category derivation + purge coverage (in-memory DB) ──────────────────────

def _mem_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from src.database.models import Base
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)

    @contextmanager
    def fake():
        db = S()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    return fake, S


def test_category_is_derived_never_defaulted_to_movie():
    import src.routers.enrichment as en
    from src.database.models import ArrEnrichmentStatus
    fake, S = _mem_session()
    real = en.get_db_session
    en.get_db_session = fake
    try:
        with S() as s:
            s.add(ArrEnrichmentStatus(service="sonarr", arr_id=5, category="anime", title="X"))
            s.commit()
        assert en._derive_category("sonarr", 5) == "anime"
        assert en._derive_category("sonarr", 6) == "show", "unknown series: the service's domain"
        assert en._derive_category("radarr", 6) == "movie" and en._derive_category("lidarr", 6) == "music"
        assert en._derive_category("sonarr", 5, "movie") == "movie", "an explicit valid hint wins"
        assert en._derive_category("sonarr", 5, "bogus") == "anime"
    finally:
        en.get_db_session = real
    src = (_ROOT / "src/routers/enrichment.py").read_text(encoding="utf-8")
    for fn in ("async def apply_match_override", "async def delete_match_override"):
        body = src[src.index(fn):]
        body = body[:body.index("\n@router")]
        assert 'or "movie"' not in body, f"{fn} still invents a category"


def test_purge_covers_every_key_shape():
    import src.routers.enrichment as en
    import src.cache.metadata_cache as mcm
    from src.database.models import ArrEnrichmentStatus, EnrichmentStatus
    fake, S = _mem_session()

    class Conn:
        deleted: list = []

        def execute(self, sql, params=()):
            Conn.deleted.append(params[0])
            return type("C", (), {"rowcount": 1})()

        def commit(self):
            pass

    class MC:
        conn = Conn()

        def __init__(self):
            pass

        def close(self):
            pass
    real = (en.get_db_session, mcm.MetadataCache)
    en.get_db_session, mcm.MetadataCache = fake, MC
    try:
        with S() as s:
            s.add(EnrichmentStatus(plex_rating_key="sonarr:9", title="Lupin III", media_category="anime",
                                   enriched=True, error="Not found: year_mismatch", attempt_count=3))
            s.add(ArrEnrichmentStatus(service="sonarr", arr_id=9, category="anime", title="Lupin III",
                                      tvdb_id=777, error="Not found: year_mismatch"))
            s.commit()
        en._purge_and_requeue_item("sonarr", 9, "anime")
        keys = set(Conn.deleted)
        for k in ("raw_prefetch:sonarr:9", "raw:anime:sonarr:9", "enriched:anime:sonarr:9",
                  "enriched:anime:Lupin III", "enriched:show:Lupin III", "raw:anime:777"):
            assert k in keys, f"{k} not purged — a wrong profile could be reconciled back"
        with S() as s:
            row = s.query(EnrichmentStatus).filter_by(plex_rating_key="sonarr:9").one()
            assert row.attempt_count == 0 and row.error is None and row.next_retry_at is None
            arr = s.query(ArrEnrichmentStatus).filter_by(service="sonarr", arr_id=9).one()
            assert arr.error is None and arr.enriched is False
    finally:
        en.get_db_session, mcm.MetadataCache = real


def test_page_endpoints_and_frontend_wiring():
    en = (_ROOT / "src/routers/enrichment.py").read_text(encoding="utf-8")
    for route in ('@router.get("/items")', '@router.get("/unmatched")',
                  '@router.post("/items/{service}/{arr_id}/retry")',
                  '@router.post("/items/{service}/{arr_id}/ignore")',
                  '@router.delete("/items/{service}/{arr_id}/ignore")'):
        assert route in en, route
    assert en.count("_kb.invalidate()") >= 5, "every write endpoint drops the KB cache"
    assert "classified_items" in en and "attention_reasons" in en
    assert "reason in t[0]" in en and '"total_all"' in en, "Needs attention filters by reason server-side"
    kb = (_ROOT / "src/services/kb_overview.py").read_text(encoding="utf-8")
    assert "async def classified_items" in kb and "def invalidate" in kb
    assert "attention_reasons(" in kb, "the sidebar badge counts what the Needs-attention page lists"
    me = (_ROOT / "src/services/media_enricher.py").read_text(encoding="utf-8")
    assert 'ctx["rejected"]' in me and "async def anilist_candidates" in me
    assert '"_match_basis"' in me and '"_reason":        "low_confidence"' in me
    fe = (_ROOT / "frontend/index.html").read_text(encoding="utf-8")
    for needle in ("renderMatchPicker", "loadKbItems", "loadKbAttention", 'id="kb-badge"',
                   "Not this one", "Retry now", "Search &amp; pin", "Un-ignore", "_updateKbBadge",
                   'id="kb-attention"', 'id="kb-drilldown"', "Pin this id"):
        assert needle in fe, needle
    assert "p.category||'movie'" not in fe, "the client no longer invents a category"


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    sys.exit(1 if fails else 0)
