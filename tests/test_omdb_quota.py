"""OMDb has a daily budget: calls are counted per UTC day, the request is
skipped once the budget is spent (transient, nothing stamped), and OMDb's
own "Request limit reached!" marks the day spent at once (2026-09-25).

    python tests/test_omdb_quota.py
"""
import asyncio
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import src.services.app_state as app_state  # noqa: E402
import src.services.media_enricher as me  # noqa: E402
from src.config import settings  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


state: dict = {}
app_state.get_state = lambda k: state.get(k)
app_state.set_state = lambda k, v: state.__setitem__(k, v)

answers = {"body": {"Response": "True", "Title": "The Matrix", "Type": "movie", "Year": "1999",
                    "Ratings": [], "imdbID": "tt0133093"}}
requests: list = []


class _Resp:
    status_code = 200

    def json(self):
        return answers["body"]


class _Client:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        requests.append(params.get("i"))
        return _Resp()


real_client, real_key, real_limit = me.httpx.AsyncClient, settings.OMDB_API_KEY, settings.OMDB_DAILY_LIMIT
me.httpx.AsyncClient = _Client
settings.OMDB_API_KEY = "test-key"
settings.OMDB_DAILY_LIMIT = 3
key = f"omdb_calls:{me._omdb_day()}"


async def scenario():
    r1 = await me.fetch_omdb_data("tt0133093")
    check("a call under the budget is made and counted",
          r1 and r1.get("title") == "The Matrix" and state.get(key) == "1" and requests == ["tt0133093"])
    check("the day key is the UTC date", key.split(":", 1)[1].count("-") == 2 and me.omdb_quota_left() == 2)

    answers["body"] = {"Response": "False", "Error": "Movie not found!"}
    r2 = await me.fetch_omdb_data("tt0000001")
    check("a definitive miss still costs a call and stays definitive", r2 == {} and state.get(key) == "2")

    answers["body"] = {"Response": "False", "Error": "Request limit reached!"}
    r3 = await me.fetch_omdb_data("tt0000002")
    check("OMDb's limit answer is transient and marks the day spent",
          r3 is None and int(state.get(key)) >= settings.OMDB_DAILY_LIMIT and me.omdb_quota_left() <= 0)

    n = len(requests)
    r4 = await me.fetch_omdb_data("tt0000003")
    check("at the budget no request goes out and the answer is transient (nothing stamped)",
          r4 is None and len(requests) == n)

    settings.OMDB_DAILY_LIMIT = 0
    answers["body"] = {"Response": "True", "Title": "Unbudgeted", "Type": "movie", "Year": "2000", "Ratings": []}
    r5 = await me.fetch_omdb_data("tt0000004")
    check("limit 0 = no budget", r5 and r5.get("title") == "Unbudgeted" and len(requests) == n + 1)


try:
    asyncio.run(scenario())
finally:
    me.httpx.AsyncClient, settings.OMDB_API_KEY, settings.OMDB_DAILY_LIMIT = real_client, real_key, real_limit

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
