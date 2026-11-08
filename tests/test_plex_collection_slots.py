"""Curatarr's Plex collections: edited in place, shown on the home screens.

Before, every weekly push deleted the "Curatarr · " shelves and made new
ones: a shelf pinned or placed on a home screen was gone each week, a
failed create left the household without shelves, and nothing put them on
a home screen (live 2026-11-08: four shelves, none promoted, among 501
Kometa collections). Now:

- the adapter's writes: create with a section, a server uri and the title
  and summary set (locked); a reused collection drops what fell out, adds
  what is new, and is renamed; the home row is POSTed once, PUT after;
- push_collections keeps each library's shelves as SLOTS, oldest first:
  new designs rename and refill them, surplus shelves go only after every
  design of that library was written, missing ones are created;
- COLLECTIONS_ON_HOME: all = owner's and every other account's home and
  Recommended; owner = owner's home and Recommended; off = neither home;
- a failed write keeps last week's shelves and is counted.

    python tests/test_plex_collection_slots.py
"""
from _harness import check  # noqa: I001 - first: temp stores

import asyncio
import json
from urllib.parse import parse_qs, urlparse

import httpx

import src.services.app_state as app_state
from src.config import settings
from src.services import plex_collections as pc
from src.services.media_server.base import Library
from src.services.media_server.plex_adapter import PlexAdapter

calls = []
HUBS = {"Hub": []}


def handler(req: httpx.Request) -> httpx.Response:
    u = urlparse(str(req.url))
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    calls.append((req.method, u.path, q))
    if u.path == "/":
        return httpx.Response(200, json={"MediaContainer": {"machineIdentifier": "MID", "friendlyName": "x"}})
    if u.path == "/library/sections/10/collections":
        return httpx.Response(200, json={"MediaContainer": {"Metadata": [
            {"ratingKey": "700", "title": "Curatarr · Older"}, {"ratingKey": "650", "title": "Curatarr · Oldest"},
            {"ratingKey": "5", "title": "Kometa Thing"}]}})
    if u.path == "/library/collections/650/children":
        return httpx.Response(200, json={"MediaContainer": {"Metadata": [{"ratingKey": "1"}, {"ratingKey": "2"}]}})
    if u.path == "/library/collections" and req.method == "POST":
        return httpx.Response(200, json={"MediaContainer": {"Metadata": [{"ratingKey": "901"}]}})
    if u.path == "/hubs/sections/10/manage" and req.method == "GET":
        return httpx.Response(200, json={"MediaContainer": HUBS})
    return httpx.Response(200)


plex = PlexAdapter("http://plex:32400", "owner-tok", transport=httpx.MockTransport(handler))
movies = Library(key="10", title="Movies", kind="movie")

shelves = asyncio.run(plex.collections(pc.COLLECTION_PREFIX, library_key="10"))
check("the library's Curatarr shelves, oldest first, Kometa's left out",
      [s["key"] for s in shelves] == ["650", "700"])

calls.clear()
key = asyncio.run(plex.write_collection(movies, "Curatarr · Night Rain", ["2", "3"],
                                        summary="Wet streets.", reuse_key="650"))
deletes = [c[1] for c in calls if c[0] == "DELETE"]
adds = [c for c in calls if c[0] == "PUT" and c[1] == "/library/collections/650/items"]
edit = [c for c in calls if c[0] == "PUT" and c[1] == "/library/sections/10/all"]
check("a reused shelf drops what fell out and adds what is new, nothing else",
      key == "650" and deletes == ["/library/collections/650/items/1"]
      and len(adds) == 1 and adds[0][2]["uri"].endswith("/library/metadata/3")
      and adds[0][2]["uri"].startswith("server://MID/"))
check("...and is renamed and described, locked against a metadata refresh",
      len(edit) == 1 and edit[0][2]["type"] == "18" and edit[0][2]["id"] == "650"
      and edit[0][2]["title.value"] == "Curatarr · Night Rain" and edit[0][2]["summary.value"] == "Wet streets."
      and edit[0][2]["title.locked"] == "1")

calls.clear()
key = asyncio.run(plex.write_collection(Library(key="11", title="TV", kind="show"), "Curatarr · B", ["7"]))
post = [c for c in calls if c[0] == "POST"]
check("a new shelf: one create in its section, series stay series (type 2), then its title",
      key == "901" and len(post) == 1 and post[0][2]["sectionId"] == "11" and post[0][2]["type"] == "2"
      and any(c[1] == "/library/sections/11/all" for c in calls))

calls.clear()
asyncio.run(plex.show_collection("10", "650", own_home=True, shared_home=True, recommended=True))
first = [c for c in calls if c[0] in ("POST", "PUT")]
check("a home row the first time: POST with the collection and all three flags",
      len(first) == 1 and first[0][0] == "POST" and first[0][1] == "/hubs/sections/10/manage"
      and first[0][2] == {"metadataItemId": "650", "promotedToOwnHome": "1",
                          "promotedToSharedHome": "1", "promotedToRecommended": "1"})
HUBS["Hub"] = [{"identifier": "custom.collection.10.650"}]
calls.clear()
asyncio.run(plex.show_collection("10", "650", own_home=True, shared_home=False, recommended=True))
again = [c for c in calls if c[0] in ("POST", "PUT")]
check("...after that a PUT on the managed hub",
      len(again) == 1 and again[0][0] == "PUT" and again[0][1] == "/hubs/sections/10/manage/custom.collection.10.650"
      and again[0][2]["promotedToSharedHome"] == "0")

calls.clear()
asyncio.run(plex.remove_collection("700"))
check("removing deletes the collection object only",
      [(c[0], c[1]) for c in calls] == [("DELETE", "/library/collections/700")])


class Fake:
    def __init__(self, slots, fail=()):
        self.slots, self.fail, self.log = slots, set(fail), []

    async def collections(self, prefix="", *, library_key):
        return list(self.slots.get(library_key, []))

    async def write_collection(self, lib, title, keys, *, summary="", reuse_key=None):
        if title in self.fail:
            raise httpx.ConnectError("plex down")
        self.log.append(("write", lib.key, lib.kind, title, reuse_key, summary))
        return reuse_key or f"new-{title[-1]}"

    async def show_collection(self, sec, key, **flags):
        self.log.append(("show", sec, key, flags))

    async def remove_collection(self, key):
        self.log.append(("remove", key))


state = {}
app_state.get_state, app_state.set_state = state.get, state.__setitem__
state["curatarr_collections"] = json.dumps({"designs": [{"section_key": "12"}]})
designs = [{"section_key": "10", "plex_type": 1, "title": "Curatarr · A", "keys": ["1"], "description": "a"},
           {"section_key": "11", "plex_type": 2, "title": "Curatarr · B", "keys": ["2"], "description": "b"},
           {"section_key": "11", "plex_type": 2, "title": "Curatarr · C", "keys": ["3"], "description": "c"}]
fake = Fake({"10": [{"key": "650", "title": "Curatarr · Old1"}, {"key": "700", "title": "Curatarr · Old2"}],
             "11": [{"key": "800", "title": "Curatarr · Old3"}],
             "12": [{"key": "900", "title": "Curatarr · Gone"}]})
settings.COLLECTIONS_ON_HOME = "all"
res = asyncio.run(pc.push_collections(designs, adapter=fake))
writes = [x for x in fake.log if x[0] == "write"]
check("each library's shelves are reused oldest first; a library with more designs gets a new one",
      [(w[1], w[3], w[4]) for w in writes] == [("10", "Curatarr · A", "650"), ("11", "Curatarr · B", "800"),
                                               ("11", "Curatarr · C", None)]
      and [w[2] for w in writes] == ["movie", "show", "show"] and writes[0][5] == "a")
check("surplus shelves go: the second movie shelf, and the emptied category's from the last snapshot",
      sorted(x[1] for x in fake.log if x[0] == "remove") == ["700", "900"]
      and res == {"created": 3, "new": 1, "edited": 2, "deleted": 2, "failed": 0})
shows = [x for x in fake.log if x[0] == "show"]
check("all: on the owner's home, every other account's home and Recommended",
      len(shows) == 3 and all(x[3] == {"own_home": True, "shared_home": True, "recommended": True} for x in shows))
check("the snapshot names what was written",
      [d["rating_key"] for d in json.loads(state["curatarr_collections"])["designs"]] == ["650", "800", "new-C"])

for mode, want in (("owner", {"own_home": True, "shared_home": False, "recommended": True}),
                   ("off", {"own_home": False, "shared_home": False, "recommended": False})):
    settings.COLLECTIONS_ON_HOME = mode
    f = Fake({"10": [{"key": "650", "title": "x"}]})
    asyncio.run(pc.push_collections(designs[:1], adapter=f))
    check(f"{mode}: {want}", [x[3] for x in f.log if x[0] == "show"] == [want])
settings.COLLECTIONS_ON_HOME = "all"

f = Fake({"11": [{"key": "800", "title": "x"}, {"key": "801", "title": "y"}, {"key": "802", "title": "z"}]},
         fail={"Curatarr · C"})
res = asyncio.run(pc.push_collections(designs[1:], adapter=f))
check("a failed write is counted, and the library keeps its other shelves this week",
      res["failed"] == 1 and not [x for x in f.log if x[0] == "remove"])

check.done()
