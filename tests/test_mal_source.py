"""MyAnimeList for anime: the official API with a client id, Jikan without,
and Jikan paused while it is down.

2026-10-06: Jikan had failed since 2026-08-28 (504 on every endpoint, then
no connection at all). Anime that AniList could not match waited on it in
every run and stayed open, and anime reception, which demanded MAL's scores
and reviews from Jikan, stood still for every title with a MAL id.

    python tests/test_mal_source.py
"""
import asyncio
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import httpx  # noqa: E402

from src.config import settings  # noqa: E402
from src.services import mal_source as ms  # noqa: E402

DETAIL = {
    "id": 48736, "title": "Sono Bisque Doll wa Koi wo Suru",
    "alternative_titles": {"en": "My Dress-Up Darling", "ja": "その着せ替え人形は恋をする",
                           "synonyms": ["Kisekoi"]},
    "start_date": "2022-01-09", "start_season": {"year": 2022, "season": "winter"},
    "synopsis": "s" * 900, "mean": 8.15, "rank": 389, "popularity": 152,
    "num_list_users": 1200000, "num_scoring_users": 700000, "nsfw": "gray",
    "genres": [{"id": 4, "name": "Comedy"}, {"id": 22, "name": "Romance"},
               {"id": 9, "name": "Ecchi"}, {"id": 42, "name": "Seinen"},
               {"id": 23, "name": "School"}, {"id": 81, "name": "Otaku Culture"}],
    "status": "finished_airing", "num_episodes": 12, "source": "manga",
    "rating": "pg_13", "studios": [{"id": 1, "name": "CloverWorks"}],
}


class _Calls(list):
    pass


def _transport(routes, calls):
    def handler(request: httpx.Request):
        calls.append(request)
        for (path, status, body) in routes:
            if request.url.path.endswith(path):
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"error": "not_found"})
    return httpx.MockTransport(handler)


def _run(coro):
    return asyncio.run(coro)


def _with_client(routes, fn):
    calls = _Calls()

    async def go():
        async with httpx.AsyncClient(transport=_transport(routes, calls)) as c:
            return await fn(c)
    return _run(go()), calls


def _set_id(value):
    settings.MAL_CLIENT_ID = value


def test_the_record_comes_out_in_jikans_shape():
    s = ms.to_supplement(DETAIL)
    assert s["source"] == "mal" and s["mal_id"] == 48736
    assert s["title"] == "My Dress-Up Darling" and s["year"] == 2022
    assert len(s["synopsis"]) == 800
    assert s["genres"] == ["Comedy", "Romance"]
    assert s["explicit_genres"] == ["Ecchi"] and s["demographics"] == ["Seinen"]
    assert s["themes"] == ["School", "Otaku Culture"]
    assert (s["score"], s["scored_by"], s["rank"], s["popularity"]) == (8.15, 700000, 389, 152)
    assert s["episodes"] == 12 and s["status"] == "Finished Airing"
    assert s["rating"] == "PG-13 - Teens 13 or older" and s["studios"] == ["CloverWorks"]
    assert s["source_material"] == "Manga"
    assert ms._source_label("light_novel") == "Light novel"
    assert ms._source_label("4_koma_manga") == "4-koma manga"
    assert ms.to_supplement({"id": 1, "start_date": "2009-04"})["year"] == 2009
    assert ms.to_supplement({"id": 1, "rating": "rx"})["rating"] == "Rx - Hentai"


def test_answers_and_failures():
    _set_id("test-client-id")
    try:
        got, calls = _with_client([("/anime/48736", 200, DETAIL)],
                                  lambda c: ms.anime(c, 48736))
        assert got["id"] == 48736
        assert calls[0].headers["X-MAL-CLIENT-ID"] == "test-client-id"
        assert calls[0].url.params["nsfw"] == "true"
        for status, expect in ((404, None), (400, None), (401, ms.UNAVAILABLE),
                               (403, ms.UNAVAILABLE), (429, ms.UNAVAILABLE),
                               (500, ms.UNAVAILABLE)):
            got, _ = _with_client([("/anime/1", status, {})], lambda c: ms.anime(c, 1))
            assert got is expect, status

        def boom(request):
            raise httpx.ConnectTimeout("timed out")

        async def net():
            async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as c:
                return await ms.anime(c, 1)
        assert _run(net()) is ms.UNAVAILABLE
    finally:
        _set_id(None)


def test_without_a_client_id_nothing_is_sent():
    _set_id(None)
    got, calls = _with_client([("/anime/1", 200, DETAIL)], lambda c: ms.anime(c, 1))
    assert got is ms.UNAVAILABLE and calls == []
    _set_id("   ")
    assert ms.client_id() is None
    _set_id(None)


def test_search_takes_the_close_match_and_skips_rejected():
    from src.services.media_enricher import _titles_close_enough
    body = {"data": [{"node": {"id": 11, "title": "Akebi-chan no Sailor-fuku",
                               "alternative_titles": {"en": "Akebi's Sailor Uniform"}}},
                     {"node": {"id": 12, "title": "Akebi-chan 100 man-bu"}}]}
    _set_id("test-client-id")
    try:
        got, calls = _with_client([("/anime", 200, body)],
                                  lambda c: ms.search(c, "Akebi's Sailor Uniform", _titles_close_enough))
        assert got == 11
        assert calls[0].url.params["q"] == "Akebi's Sailor Uniform"
        got, _ = _with_client([("/anime", 200, body)],
                              lambda c: ms.search(c, "Akebi's Sailor Uniform",
                                                  _titles_close_enough, rejected={11}))
        assert got is None, "the owner's 'Not this one' stays rejected"
        got, calls = _with_client([("/anime", 200, body)],
                                  lambda c: ms.search(c, "K", _titles_close_enough))
        assert got is None and calls == [], "MAL refuses searches under three letters"
        got, _ = _with_client([("/anime", 503, {})],
                              lambda c: ms.search(c, "Akebi", _titles_close_enough))
        assert got is ms.UNAVAILABLE
    finally:
        _set_id(None)


def test_jikan_pauses_after_three_failures_and_recovers():
    ms._jikan.update(fails=0, until=0.0)
    for _ in range(ms.JIKAN_PAUSE_AFTER - 1):
        ms.jikan_result(False)
    assert not ms.jikan_paused()
    ms.jikan_result(True)
    assert ms._jikan["fails"] == 0, "one answer clears the count"
    for _ in range(ms.JIKAN_PAUSE_AFTER):
        ms.jikan_result(False)
    assert ms.jikan_paused()
    ms._jikan["until"] = 0.0                       # the hour is over
    assert not ms.jikan_paused()
    ms.jikan_result(False)                         # tried once, failed again
    assert ms.jikan_paused()
    ms._jikan.update(fails=0, until=0.0)


def test_the_enricher_asks_mal_or_a_paused_jikan_not_at_all():
    from src.services import media_enricher as me
    from src.services.enrichment_state import TRANSIENT
    seen = []

    async def fake_jikan(mal_id, title, rejected):
        seen.append(("jikan", mal_id, title))
        return None

    async def fake_official(mal_id, title, rejected):
        seen.append(("mal", mal_id, title))
        return {"source": "mal", "mal_id": mal_id}

    real = (me._fetch_jikan, me._fetch_mal_official)
    me._fetch_jikan, me._fetch_mal_official = fake_jikan, fake_official
    ms._jikan.update(fails=0, until=0.0)
    try:
        _set_id("test-client-id")
        assert _run(me.fetch_mal_data(mal_id=5))["mal_id"] == 5
        _set_id(None)
        assert _run(me.fetch_mal_data(title="Grenadier")) is None
        ms._jikan["until"] = 10 ** 12                 # paused
        assert _run(me.fetch_mal_data(title="Grenadier")) is TRANSIENT
        assert seen == [("mal", 5, None), ("jikan", None, "Grenadier")], seen
    finally:
        me._fetch_jikan, me._fetch_mal_official = real
        ms._jikan.update(fails=0, until=0.0)
        _set_id(None)


def test_the_official_fetch_end_to_end():
    from src.services import media_enricher as me
    from src.services.enrichment_state import TRANSIENT
    body = {"data": [{"node": {"id": 48736, "title": "Sono Bisque Doll wa Koi wo Suru",
                               "alternative_titles": {"en": "My Dress-Up Darling"}}}]}
    routes = [("/anime/48736", 200, DETAIL), ("/anime", 200, body)]
    calls = []
    real_client = httpx.AsyncClient

    class _Client(real_client):
        def __init__(self, *a, **kw):
            kw["transport"] = _transport(routes, calls)
            super().__init__(*a, **kw)

    _set_id("test-client-id")
    me.httpx.AsyncClient = _Client
    try:
        s = _run(me._fetch_mal_official(None, "My Dress-Up Darling", None))
        assert s["mal_id"] == 48736 and s["score"] == 8.15
        routes[:] = [("/anime", 503, {})]
        assert _run(me._fetch_mal_official(None, "My Dress-Up Darling", None)) is TRANSIENT
        routes[:] = [("/anime", 200, {"data": []})]
        assert _run(me._fetch_mal_official(None, "Nothing Like It", None)) is None
    finally:
        me.httpx.AsyncClient = real_client
        _set_id(None)


def test_reception_goes_on_without_a_paused_jikan():
    from src.services import reception as rc
    calls = []

    async def fake_jikan(client, path):
        calls.append(path)
        return None

    real = rc._jikan
    rc._jikan = fake_jikan
    ms._jikan.update(fails=0, until=0.0)
    try:
        # official scores, Jikan paused: the scores, no reviews, no waiting
        _set_id("test-client-id")
        ms._jikan["until"] = 10 ** 12
        stats, revs = _with_client([("/anime/48736", 200, DETAIL)],
                                   lambda c: rc._mal_reception(c, 48736))[0]
        assert stats == {"score": 8.15, "scored_by": 700000, "members": 1200000}
        assert revs == [] and calls == []
        # no client id, Jikan paused: AniList carries it alone
        _set_id(None)
        assert _with_client([], lambda c: rc._mal_reception(c, 48736))[0] == ({}, [])
        # a single Jikan miss, not yet paused: the title waits for a retry
        ms._jikan.update(fails=0, until=0.0)
        try:
            _with_client([], lambda c: rc._mal_reception(c, 48736))
            raise AssertionError("expected TransientSourceError")
        except rc.TransientSourceError:
            pass
    finally:
        rc._jikan = real
        ms._jikan.update(fails=0, until=0.0)
        _set_id(None)


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
