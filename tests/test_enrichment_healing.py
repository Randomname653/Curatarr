"""Enrichment healing (2026-09): honest states, counted attempts, and
"unavailable" kept apart from "does not exist".

Pins the root causes found in the audit:
  - a LIVE not-found sentinel classified as enriched (status first, cache second)
  - no attempt counter (fresh miss counts, reconcile doesn't, success resets)
  - _write_enrichment_db(item, None, cat) crashed → "Processing failed" never written
  - _fetch_source swallowed TMDB 429s as "miss"; AniList/Jikan/MB/Last.fm
    returned the same value for 429 and no-match; MB negative-cached an outage
  - the evidence (who missed, who was skipped, what wrong hit was found) was
    thrown away on the not-found path
  - the producer's dispatch order (a not-found dict must never reach the LLM)
  - the weekly audit blindly requeued every not-found row

    python tests/test_enrichment_healing.py
"""
import asyncio
import json
import pathlib
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.services import enrichment_state as es


class _Row:
    def __init__(self, enriched=True, error=None, provisional=False,
                 next_retry_at=None, attempt_count=0):
        self.enriched, self.error, self.provisional = enriched, error, provisional
        self.next_retry_at, self.attempt_count = next_retry_at, attempt_count


def test_classifier_status_first_then_cache():
    future = datetime.utcnow() + timedelta(days=2)
    past = datetime.utcnow() - timedelta(days=2)
    C = es.classify_enrichment_row
    assert C(None) == "never_processed"
    assert C(_Row(), has_live_cache=True) == "enriched"
    assert C(_Row(), has_live_cache=False) == "enriched_dead"
    assert C(_Row()) == "enriched", "SQL-only callers have no cache knowledge"
    assert C(_Row(provisional=True), has_live_cache=True) == "enriched_provisional"
    assert C(_Row(error="Not found: no_source_data", next_retry_at=future),
             has_live_cache=True) == "not_found", "a LIVE sentinel must never read as enriched"
    assert C(_Row(error="Not found in metadata APIs")) == "retry_due"      # legacy string
    assert C(_Row(error="Not found: year_mismatch", next_retry_at=past)) == "retry_due"
    assert C(_Row(error="rule_based — LLM upgrade pending")) == "rule_based"
    assert C(_Row(error="api_cached — LLM pending")) == "awaiting_llm"
    assert C(_Row(enriched=False)) == "queued"
    assert C(_Row(enriched=False, error="Processing failed")) == "processing_error"
    assert C(_Row(enriched=False, error="ignored by owner")) == "ignored"
    assert tuple(es.STATE_DEFINITIONS) == es.STATES
    assert all({"label", "explainer", "next_step"} <= set(v) for v in es.STATE_DEFINITIONS.values())


def test_not_found_reason_vocabulary():
    assert es.not_found_error("year_mismatch") == "Not found: year_mismatch"
    assert es.not_found_error("garbage") == "Not found: no_source_data"
    assert es.not_found_reason("Not found in metadata APIs") == "no_source_data"
    assert es.not_found_reason("Not found: low_confidence") == "low_confidence"
    assert es.not_found_reason(None) is None and es.not_found_reason("rule_based x") is None


# ── the writer, against an in-memory DB and a no-op cache ────────────────────

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


class _FakeCache:
    writes: list = []

    def __init__(self, *a, **k):
        pass

    def set_cache(self, key, value, days=None):
        _FakeCache.writes.append((key, days))

    def get_cache(self, key):
        return None

    def close(self):
        pass


def _patched_writer():
    import src.routers.enrichment as en
    import src.cache.metadata_cache as mcm
    fake, S = _mem_session()
    real = (en.get_db_session, mcm.MetadataCache)
    en.get_db_session, mcm.MetadataCache = fake, _FakeCache
    return en, S, real


def _restore(en, real):
    import src.cache.metadata_cache as mcm
    en.get_db_session, mcm.MetadataCache = real


def test_writer_counts_fresh_attempts_and_resets_on_success():
    en, S, real = _patched_writer()
    try:
        from src.database.models import EnrichmentStatus, ArrEnrichmentStatus
        item = {"plex_rating_key": "radarr:7", "title": "Ghost Film",
                "service": "radarr", "arr_id": 7}
        sentinel = en._build_not_found_sentinel("Ghost Film", "movie", {
            "_reason": "no_source_data",
            "_sources_state": {"tmdb": {"status": "miss", "at": "x"}},
            "_context": {"arr_year": 2003, "had_ids": []},
        })

        def row():
            with S() as s:
                return s.query(EnrichmentStatus).filter_by(plex_rating_key="radarr:7").one()

        def arr():
            with S() as s:
                return s.query(ArrEnrichmentStatus).filter_by(service="radarr", arr_id=7).one()

        run = lambda **kw: asyncio.run(en._write_enrichment_db(item, sentinel, "movie", **kw))
        assert run(fresh_attempt=True) is True
        assert run(fresh_attempt=True) is True
        r = row()
        assert r.attempt_count == 2 and r.next_retry_at is None, "two free attempts"
        assert r.error == "Not found: no_source_data" and r.enriched is True
        state = json.loads(r.sources_state)
        assert state["tmdb"]["status"] == "miss" and state["_context"]["arr_year"] == 2003, \
            "the evidence is persisted for sentinels too"
        assert run(fresh_attempt=False) is False
        assert row().attempt_count == 2, "a cache reconcile never counts"
        assert run(fresh_attempt=True) is True
        r = row()
        assert r.attempt_count == 3 and r.next_retry_at is not None
        assert r.next_retry_at - r.last_attempt_at >= timedelta(days=2, hours=23), "third miss waits 3 d"
        assert _FakeCache.writes[-1][1] == 3, "sentinel cache TTL follows the wait"
        a = arr()
        assert a.error == "Not found: no_source_data" and a.enriched is False, "arr view mirrors the row"

        assert en._rollback_attempts(["radarr:7", "radarr:nope"]) == 1
        r = row()
        assert r.attempt_count == 2 and r.next_retry_at is None, "outage rollback gives the attempt back"

        good = {"source": "tmdb+llm", "title": "Ghost Film", "plex_rating_key": "radarr:7",
                "match_basis": "title_search", "match_confidence": 0.91}
        assert asyncio.run(en._write_enrichment_db(item, good, "movie", fresh_attempt=True)) is False
        r = row()
        assert r.attempt_count == 0 and r.next_retry_at is None and r.error is None
        assert r.match_basis == "title_search" and abs(r.match_confidence - 0.91) < 1e-9
        a = arr()
        assert a.error is None and a.enriched is True

        assert asyncio.run(en._write_enrichment_db(item, None, "movie", fresh_attempt=True)) is False
        r = row()
        assert r.error == "Processing failed" and r.enriched is False and r.attempt_count == 0, \
            "an LLM failure is recorded, not crashed on, and counts no attempt"
    finally:
        _restore(en, real)


def test_not_found_sentinel_builder():
    import src.routers.enrichment as en
    s = en._build_not_found_sentinel("X", "show", None)
    assert s["source"] == "not_found" and s["not_found_reason"] == "no_source_data"
    assert "sources_state" not in s
    s = en._build_not_found_sentinel("X", "show", {
        "_reason": "year_mismatch",
        "_sources_state": {"tmdb": {"status": "ok", "title": "X", "year": 1992}},
        "_context": {"arr_year": 2007, "resolved": {"title": "X", "year": 1992}, "had_ids": []},
    })
    assert s["not_found_reason"] == "year_mismatch"
    assert s["sources_state"]["tmdb"]["title"] == "X"
    assert s["sources_state"]["_context"]["arr_year"] == 2007
    assert "had_ids" not in s["sources_state"]["_context"], "empty context values are dropped"


def test_producer_dispatch_never_polishes_a_miss():
    src = (_ROOT / "src/routers/enrichment.py").read_text(encoding="utf-8")
    body = src[src.index("async def _process_one"):src.index("async def _producer")]
    assert 'elif raw is not None and not raw.get("_not_found"):' in body
    assert body.index('not raw.get("_not_found")') < body.index("cat_queues[pcat].put"), \
        "the LLM queue branch must exclude the not-found dict"
    assert "_build_not_found_sentinel(canonical, pcat, raw)" in body
    assert "fresh_attempt=True" in body and "fresh_attempt=False" in body
    assert "_attempted_keys[pcat].add" in body and "_rollback_attempts(" in src
    assert "Not found in metadata APIs" not in src, "the old free-text state string is gone"


def test_transient_marker_flows_to_sources_state():
    import src.services.media_enricher as me
    raw = {"title": "T", "media_type": "movie", "sources_state": {}}
    me._merge_source_into_raw(raw, "tmdb", es.TRANSIENT, "movie", False)
    me._merge_source_into_raw(raw, "omdb", {}, "movie", False)
    assert raw["sources_state"]["tmdb"]["status"] == "transient"
    assert raw["sources_state"]["omdb"]["status"] == "miss"
    assert me._drained_outcome(raw["sources_state"]) == "transient"
    assert me._drained_outcome({"tmdb": {"status": "miss"}, "_context": {"x": 1}}) == "not_found"
    assert me._drained_outcome({"tmdb": {"status": "transient"}, "omdb": {"status": "ok"}}) == "not_found"
    assert not es.TRANSIENT and es.TRANSIENT == {} and es.TRANSIENT.get("x") is None
    err = me.TransientFetchError({"tmdb": {"status": "transient"}})
    assert isinstance(err, me.TMDBTransientError)
    assert err.retry_after_s == 5.0 and "tmdb" in err.body_snippet


def test_resolved_entity_is_recorded_as_evidence():
    import src.services.media_enricher as me
    raw = {"title": "Arr Title", "media_type": "movie", "sources_state": {}}
    me._merge_source_into_raw(raw, "tmdb", {"title": "Resolved Title", "year": 1992, "tmdb_id": 123,
                                            "overview": "x", "genres": []}, "movie", False)
    assert raw["title"] == "Arr Title", "the arr title still wins inside the blob"
    ev = raw["sources_state"]["tmdb"]
    assert ev["status"] == "ok" and ev["title"] == "Resolved Title"
    assert ev["year"] == 1992 and ev["id"] == 123


def test_fetch_source_translates_every_failure_kind():
    import src.services.media_enricher as me
    real = (me.fetch_omdb_data, me._tmdb_search_and_fetch)

    async def omdb_none(_):
        return None

    async def omdb_empty(_):
        return {}

    async def tmdb_boom(*a, **k):
        raise me.TMDBTransientError(429, retry_after_s=1.0)

    async def tmdb_bug(*a, **k):
        raise ValueError("shape drift")
    try:
        me.fetch_omdb_data = omdb_none
        assert asyncio.run(me._fetch_source("omdb", {"imdb_id": "tt1"})) is es.TRANSIENT
        me.fetch_omdb_data = omdb_empty
        assert asyncio.run(me._fetch_source("omdb", {"imdb_id": "tt1"})) == {}, "definitive miss stays a miss"
        assert asyncio.run(me._fetch_source("omdb", {})) is None, "not asked without an imdb id"
        me._tmdb_search_and_fetch = tmdb_boom
        assert asyncio.run(me._fetch_source("tmdb", {"title": "x", "media_type": "movie"})) is es.TRANSIENT
        me._tmdb_search_and_fetch = tmdb_bug
        assert asyncio.run(me._fetch_source("tmdb", {"title": "x", "media_type": "movie"})) is es.TRANSIENT, \
            "a crashing fetcher did not say 'no record'"
    finally:
        me.fetch_omdb_data, me._tmdb_search_and_fetch = real


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code, self._p, self.headers = status, payload or {}, {}

    def json(self):
        return self._p


class _FakeClient:
    status, payload = 429, {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        return _Resp(_FakeClient.status, _FakeClient.payload)

    async def post(self, *a, **k):
        return _Resp(_FakeClient.status, _FakeClient.payload)


def test_upstream_fetchers_say_unavailable_not_missing():
    import src.services.media_enricher as me
    import src.services.music_metadata as mm
    from src.config import settings
    real_client = me.httpx.AsyncClient
    real = (me._anilist_wait, me._anilist_set_backoff, mm._mb_request, mm.MetadataCache,
            getattr(settings, "LASTFM_API_KEY", None))

    async def _nowait():
        return None

    def _noback(_headers):
        return 0.0

    async def _mb(client, url, params):
        return _Resp(_FakeClient.status, _FakeClient.payload)
    try:
        me.httpx.AsyncClient = _FakeClient          # httpx is one shared module
        me._anilist_wait, me._anilist_set_backoff = _nowait, _noback
        mm._mb_request, mm.MetadataCache = _mb, _FakeCache
        settings.LASTFM_API_KEY = "test-key"
        _FakeClient.status, _FakeClient.payload = 429, {}
        assert asyncio.run(me.search_anilist_by_title("Futurama")) is es.TRANSIENT
        assert asyncio.run(me.fetch_anilist_full(1)) is es.TRANSIENT
        assert asyncio.run(me.fetch_jikan_data(mal_id=1)) is es.TRANSIENT
        _FakeCache.writes.clear()
        assert asyncio.run(mm.fetch_musicbrainz_artist("Solstice")) is es.TRANSIENT
        assert asyncio.run(mm.fetch_lastfm_artist("Solstice")) is es.TRANSIENT
        assert not _FakeCache.writes, "an outage must not be negative-cached"
        _FakeClient.status, _FakeClient.payload = 200, {"data": {"Page": {"media": []}}}
        assert asyncio.run(me.search_anilist_by_title("Futurama")) is None, "nothing matched = definitive"
        _FakeClient.payload = {"artists": []}
        assert asyncio.run(mm.fetch_musicbrainz_artist("Nobody")) is None
        assert _FakeCache.writes and _FakeCache.writes[-1][0].startswith("mb:artist:"), \
            "a definitive MB miss is still negative-cached"
    finally:
        me.httpx.AsyncClient = real_client
        me._anilist_wait, me._anilist_set_backoff, mm._mb_request, mm.MetadataCache = real[:4]
        settings.LASTFM_API_KEY = real[4]


def test_audit_no_longer_flags_not_found():
    import src.routers.enrichment as en
    assert en._enrichment_incomplete_reason({"source": "not_found"}, "movie") is None, \
        "the backoff owns not-found retries now; the weekly blind requeue is gone"
    assert en._enrichment_incomplete_reason({"source": "tmdb+llm", "rating": 0}, "movie") == "zero_rating"


def test_raw_refresh_survives_the_new_shapes():
    src = (_ROOT / "src/services/raw_refresh.py").read_text(encoding="utf-8")
    assert 'fresh.get("_not_found")' in src and "except TMDBTransientError" in src


def test_open_reason_sentences():
    st = {"tmdb": {"status": "miss", "at": "x"}, "_context": {"arr_year": 2003, "had_ids": ["tvdb_id"]}}
    s = es.open_reason("Not found: no_source_data", st, attempt_count=4,
                       next_retry_at=datetime(2026, 9, 12), title="X", category="show",
                       now=datetime(2026, 9, 6))
    assert "TMDB: no match" in s and "OMDb not asked" in s and "tried 4×" in s and "12 Sep" in s, s
    st2 = {"tmdb": {"status": "ok", "title": "Blown Away", "year": 1992},
           "_context": {"arr_year": 1994, "resolved": {"title": "Blown Away", "year": 1992}}}
    s2 = es.open_reason("Not found: year_mismatch", json.dumps(st2), attempt_count=1,
                        title="Blown Away", category="movie")
    assert "1992" in s2 and "1994" in s2 and "pin the right entry" in s2 and "due on the next run" in s2, s2
    assert es.open_reason(None, None) == es.STATE_DEFINITIONS["enriched"]["explainer"]
    assert es.open_reason("rule_based — LLM upgrade pending", None) == es.STATE_DEFINITIONS["rule_based"]["explainer"]


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
