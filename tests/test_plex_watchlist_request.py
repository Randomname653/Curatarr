"""The plex.tv watchlist add, request by request.

Every add since 2026-08-31 failed with "plex.tv returned 400" and nothing
else in the log. Probed 2026-09-25: Discover's search refuses a request
without ``searchProviders`` ("Missing required param searchProviders!");
with it (plus includeMetadata and the device headers plex.tv's own clients
send) the search answers, and PUT /actions/addToWatchlist on the same host
puts the title on the list. Pins:
  - the search carries searchProviders / includeMetadata / the client id
  - the add goes to the Discover host with the resolved ratingKey
  - the SearchResults nesting of the real response is read
  - a refusal keeps plex.tv's message in the error, not just the code
  - no token -> no request at all

    python tests/test_plex_watchlist_request.py
"""
import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

import src.database.connection as conn_mod
import src.services.plex_watchlist as pw

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── fakes ────────────────────────────────────────────────────────────────────
TOKEN = {"value": "tok-abc"}


class _Q:
    def filter(self, *a):
        return self

    def first(self):
        return types.SimpleNamespace(plex_token=TOKEN["value"])


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def query(self, *a):
        return _Q()


conn_mod.get_db_session = lambda: _Session()

RK = "5d9c080d2192ba001f305f3d"
SEARCH_BODY = {"MediaContainer": {
    "suggestedTerms": [], "identifier": "tv.plex.provider.discover", "size": 2,
    "SearchResults": [
        {"id": "external", "title": "Movies & TV", "SearchResult": [
            {"score": 0.9, "Metadata": {"type": "show", "title": "Keijo!!!!!!!!", "year": 2016,
                                        "ratingKey": RK, "guid": f"plex://show/{RK}"}},
            {"score": 0.4, "Metadata": {"type": "movie", "title": "Gena the Crocodile", "year": 1969,
                                        "ratingKey": "5d7768c023d5a3001f4f0f59"}},
        ]},
    ]}}
calls: list = []
mode = {"search": 200}


class _Resp:
    def __init__(self, status, body, url):
        self.status_code, self._body, self._url = status, body, url

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(str(self.status_code),
                                        request=httpx.Request("GET", self._url), response=self)


class _Client:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        calls.append(("GET", url, dict(headers or {}), dict(params or {})))
        if mode["search"] != 200:
            return _Resp(mode["search"],
                         {"Error": {"error": "Bad Request",
                                    "message": "Missing required param searchProviders!",
                                    "statusCode": 400}}, url)
        return _Resp(200, SEARCH_BODY, url)

    async def put(self, url, headers=None, params=None):
        calls.append(("PUT", url, dict(headers or {}), dict(params or {})))
        return _Resp(200, {"MediaContainer": {"size": 0}}, url)


pw.httpx = types.SimpleNamespace(AsyncClient=_Client, HTTPStatusError=httpx.HTTPStatusError)

# ── the add ──────────────────────────────────────────────────────────────────
res = asyncio.run(pw.add_to_watchlist(1, "Keijo!!!!!!!!", 2016, "anime"))
check("add succeeds and names the Discover match", res == {"ok": True, "matched": "Keijo!!!!!!!! (2016)"})
check("one search, one add", [c[0] for c in calls] == ["GET", "PUT"])
_, s_url, s_headers, s_params = calls[0]
check("search hits Discover's /library/search", s_url == f"{pw._DISCOVER}/library/search")
check("search carries searchProviders (the missing param behind every 400)",
      s_params.get("searchProviders") == "discover")
check("search asks for metadata, movies+tv, the title",
      s_params.get("includeMetadata") == 1 and s_params.get("searchTypes") == "movies,tv"
      and s_params.get("query") == "Keijo!!!!!!!!")
check("search sends the token and the device headers plex.tv's clients send",
      s_headers.get("X-Plex-Token") == "tok-abc" and s_headers.get("X-Plex-Client-Identifier")
      and s_headers.get("X-Plex-Product") == "Curatarr" and s_headers.get("Accept") == "application/json")
_, a_url, a_headers, a_params = calls[1]
check("the add is a PUT to /actions/addToWatchlist on the Discover host",
      a_url == f"{pw._DISCOVER}/actions/addToWatchlist")
check("...with the matched ratingKey (bare, not the plex:// guid)", a_params == {"ratingKey": RK})
check("...under the same token", a_headers.get("X-Plex-Token") == "tok-abc")

# ── the refusal keeps plex.tv's message ──────────────────────────────────────
calls.clear()
mode["search"] = 400
res = asyncio.run(pw.add_to_watchlist(1, "Keijo!!!!!!!!", 2016, "anime"))
check("a 400 is reported with plex.tv's own message",
      res == {"ok": False, "error": "plex.tv returned 400: Missing required param searchProviders!"})
check("...and nothing is added", [c[0] for c in calls] == ["GET"])
mode["search"] = 200

# ── no token, no request ─────────────────────────────────────────────────────
calls.clear()
TOKEN["value"] = ""
res = asyncio.run(pw.add_to_watchlist(1, "Keijo!!!!!!!!", 2016, "anime"))
check("a user without a plex.tv token gets an honest error and no request",
      res["ok"] is False and "no plex.tv token" in res["error"] and calls == [])
TOKEN["value"] = "tok-abc"

# ── ambiguity still refuses ──────────────────────────────────────────────────
calls.clear()
res = asyncio.run(pw.add_to_watchlist(1, "Nothing Like It", None, "movie"))
check("no unambiguous match -> no add", res["ok"] is False and "no unambiguous" in res["error"]
      and [c[0] for c in calls] == ["GET"])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
