"""Per-user Plex writes go through the user's token FOR THIS SERVER.

Live probe 2026-11-08: the server answered a Plex Home member's plex.tv
account token with the OWNER's playlists, so the member's weekly push had
been replacing the owner's Shows and Anime playlists. plex.tv's resources
list hands out the server's own token for each account:

- the owner writes with the server token from the settings;
- a member's token is the resource whose clientIdentifier is this server,
  cached per account token; a refused sign-in or a server not shared with
  the account is None (skipped), never the account token;
- the next episode of a series is the first regular one: specials (season
  0) come first in Plex's order, and only a series of nothing but specials
  starts with one;
- the music sections are the mapped ones, else every music library the
  server has (an install whose music is in the Plex index only maps none).

    python tests/test_plex_server_token.py
"""
from _harness import check  # noqa: I001 - first: temp stores

import asyncio
from types import SimpleNamespace

import src.services.plex_playlists as pp

MID = "abc123machine"
requests = []


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class _Client:
    """plex.tv's resources list and a series' allLeaves."""
    answer = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        requests.append((url, (headers or {}).get("X-Plex-Token")))
        return _Client.answer(url, headers or {}, params or {})


async def _mid(force=False):
    return MID


real_client = pp.httpx.AsyncClient
pp.httpx.AsyncClient = _Client
pp.get_machine_identifier = _mid
pp._owner_token = lambda: "owner-server-token"
try:
    resources = [
        {"name": "Other", "provides": "server", "clientIdentifier": "elsewhere", "accessToken": "other-tok"},
        {"name": "Home Server", "provides": "server", "clientIdentifier": MID, "accessToken": "member-server-tok"},
    ]
    _Client.answer = lambda url, h, p: _Resp(200, resources)
    owner = SimpleNamespace(id=1, is_admin=True, plex_token="owner-account", plex_username="owner")
    member = SimpleNamespace(id=2, is_admin=False, plex_token="member-account", plex_username="member")

    check("the owner writes with the server token from the settings, no plex.tv call",
          asyncio.run(pp.server_token(owner)) == "owner-server-token" and not requests)
    tok = asyncio.run(pp.server_token(member))
    check("a member gets the resource token of THIS server, asked with their account token",
          tok == "member-server-tok" and requests[-1] == ("https://plex.tv/api/v2/resources", "member-account"))
    n = len(requests)
    check("...cached for the same account token", asyncio.run(pp.server_token(member)) == "member-server-tok"
          and len(requests) == n)
    member.plex_token = "member-account-new"
    asyncio.run(pp.server_token(member))
    check("...and asked again after a new sign-in", len(requests) == n + 1)

    pp._SERVER_TOKENS.clear()
    _Client.answer = lambda url, h, p: _Resp(200, [resources[0]])
    check("a server not shared with the account: None, never the account token",
          asyncio.run(pp.server_token(member)) is None)
    _Client.answer = lambda url, h, p: _Resp(401, {})
    check("a refused sign-in: None (skipped until the next login)", asyncio.run(pp.server_token(member)) is None)
    _Client.answer = lambda url, h, p: _Resp(503, {})
    try:
        asyncio.run(pp.server_token(member))
        raised = False
    except RuntimeError:
        raised = True
    check("plex.tv down raises, so the custodian task stays due", raised)
    check("no stored sign-in: None", asyncio.run(pp.server_token(
        SimpleNamespace(id=3, is_admin=False, plex_token=None, plex_username="x"))) is None)

    def leaves(rows):
        return lambda url, h, p: _Resp(200, {"MediaContainer": {"Metadata": rows}})
    _Client.answer = leaves([{"ratingKey": "s0e1", "parentIndex": 0}, {"ratingKey": "s1e1", "parentIndex": 1}])
    check("the next episode skips a special that sorts first",
          asyncio.run(pp.first_unwatched_episode_key("t", "99")) == "s1e1")
    _Client.answer = leaves([{"ratingKey": "s0e1", "parentIndex": 0}])
    check("...a series of nothing but specials starts with one",
          asyncio.run(pp.first_unwatched_episode_key("t", "99")) == "s0e1")
finally:
    pp.httpx.AsyncClient = real_client

import src.services.media_server as ms  # noqa: E402
from src.services.media_server.base import Library  # noqa: E402


class _Adapter:
    async def libraries(self):
        return [Library(key="10", title="Movies", kind="movie"), Library(key="14", title="Music", kind="music")]


real_get, real_read = ms.get_adapter, pp.run_read
ms.get_adapter = lambda: _Adapter()


async def _mapped_none(fn, *a):
    return []


async def _mapped_nine(fn, *a):
    return ["9"]
try:
    pp.run_read = _mapped_none
    check("no music library mapped: every music library the server has",
          asyncio.run(pp.music_section_keys()) == ["14"])
    pp.run_read = _mapped_nine
    check("a mapped one wins", asyncio.run(pp.music_section_keys()) == ["9"])
finally:
    ms.get_adapter, pp.run_read = real_get, real_read

check.done()
