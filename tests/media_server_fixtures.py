"""Recorded media-server payloads behind an httpx.MockTransport.

Shared by tests/test_media_server_seam.py and the adapter contract test.
The Plex and Jellyfin fixture sets describe the SAME library (same titles,
same files, under different mount points), so a test can check that both
adapters arrive at the same canonical keys: what a rebind matches on.
"""
import json
from pathlib import Path

import httpx

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(server: str, name: str) -> dict:
    return json.loads((FIXTURES / server / name).read_text(encoding="utf-8"))


def plex_transport(token: str = "plex-token", log: list = None) -> httpx.MockTransport:
    """Answers the Plex paths the adapter reads; 401 without the token,
    404 for anything not recorded. ``log`` collects (path, params)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append((request.url.path, dict(request.url.params)))
        if request.headers.get("X-Plex-Token") != token:
            return httpx.Response(401)
        path, params = request.url.path, request.url.params
        name = None
        if request.url.host == "plex.tv":
            if path == "/api/v2/user":
                return httpx.Response(200, json=load("plex", "plextv_user.json"))
            if path == "/api/users":
                return httpx.Response(200, text=(FIXTURES / "plex" / "plextv_users.xml").read_text(encoding="utf-8"))
            return httpx.Response(404)
        if path == "/":
            name = "root.json"
        elif path == "/library/sections":
            name = "library_sections.json"
        elif path == "/accounts":
            name = "accounts.json"
        elif path.startswith("/library/sections/") and path.endswith("/all"):
            sec = path.split("/")[3]
            name = f"section_{sec}_type_{params.get('type')}.json"
        elif path.startswith("/library/metadata/"):
            parts = path.split("/")
            name = (f"metadata_{parts[3]}_children.json" if path.endswith("/children")
                    else f"metadata_{parts[3]}.json")
        if name and (FIXTURES / "plex" / name).exists():
            return httpx.Response(200, json=load("plex", name))
        return httpx.Response(404)
    return httpx.MockTransport(handler)


JELLYFIN_LIBRARIES = {                    # library ItemId -> CollectionType
    "f137a2dd21bbc1b99aa5c0f6bf02a805": "movies",
    "a656b907eb3a73532e40e44b968d0225": "tvshows",
    "7e64e319657a9516ec78490da03edccb": "music",
}


def jellyfin_transport(api_key: str = "jf-key", log: list = None, played: dict = None,
                       report: bool = True, sessions: list = None) -> httpx.MockTransport:
    """Answers the Jellyfin paths the adapter reads; 401 unless the
    MediaBrowser header carries the key, 404 for anything not recorded.
    ``/Items?Ids=`` and ``/Items?ParentId=`` (no Recursive) are answered
    from the recorded library walks. ``played`` (user id -> items with
    UserData) replaces Items_played_{user}.json, so a test can move counts
    between polls; ``report=False`` answers the Playback Reporting query with
    404 (no plugin); ``sessions`` replaces Sessions.json."""
    def every_item() -> list:
        out = []
        for p in sorted((FIXTURES / "jellyfin").glob("*.json")):
            if p.name.startswith(("Items_", "Artists_")) and not p.name.startswith("Items_played_"):
                out += load("jellyfin", p.name).get("Items") or []
        return out

    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append((request.url.path, dict(request.url.params)))
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("MediaBrowser ") or f'Token="{api_key}"' not in auth:
            return httpx.Response(401)
        path, q = request.url.path, request.url.params
        if path == "/user_usage_stats/submit_custom_query":
            if not report or request.method != "POST":
                return httpx.Response(404)
            return httpx.Response(200, json=load("jellyfin", "PlaybackReporting_query.json"))
        if path == "/Sessions" and sessions is not None:
            return httpx.Response(200, json=sessions)
        uid = q.get("userId")
        if path == "/Items" and uid:
            mine = (played or {}).get(uid)
            if mine is None and (FIXTURES / "jellyfin" / f"Items_played_{uid}.json").exists():
                mine = load("jellyfin", f"Items_played_{uid}.json")["Items"]
            mine = mine or []
            if q.get("Ids"):
                want = set(q["Ids"].split(","))
                found = [i for i in mine if i["Id"] in want]
                if not found:      # not played: the item without play state
                    found = [{**i, "UserData": {"Played": False, "PlayCount": 0}}
                             for i in every_item() if i["Id"] in want]
                return httpx.Response(200, json={"Items": found, "TotalRecordCount": len(found)})
            start, limit = int(q.get("StartIndex", 0)), int(q.get("Limit", 500))
            return httpx.Response(200, json={"Items": mine[start:start + limit],
                                             "TotalRecordCount": len(mine), "StartIndex": start})
        simple = {"/System/Info": "System_Info.json", "/Library/VirtualFolders": "Library_VirtualFolders.json",
                  "/Users": "Users.json", "/Sessions": "Sessions.json"}
        if path in simple:
            return httpx.Response(200, json=load("jellyfin", simple[path]))
        if path == "/Artists/AlbumArtists":
            name = f"Artists_AlbumArtists_{JELLYFIN_LIBRARIES.get(q.get('ParentId'), '?')}.json"
        elif path == "/Items" and q.get("Ids"):
            want = set(q["Ids"].split(","))
            found = [i for i in every_item() if i["Id"] in want]
            return httpx.Response(200, json={"Items": found, "TotalRecordCount": len(found), "StartIndex": 0})
        elif path == "/Items" and q.get("Recursive") != "true" and q.get("ParentId"):
            kids = [i for i in every_item() if i.get("ParentId") == q["ParentId"]]
            return httpx.Response(200, json={"Items": kids, "TotalRecordCount": len(kids), "StartIndex": 0})
        elif path == "/Items":
            name = (f"Items_{JELLYFIN_LIBRARIES.get(q.get('ParentId'), '?')}"
                    f"_{q.get('IncludeItemTypes')}.json")
        else:
            return httpx.Response(404)
        if (FIXTURES / "jellyfin" / name).exists():
            return httpx.Response(200, json=load("jellyfin", name))
        return httpx.Response(404)
    return httpx.MockTransport(handler)


class JellyfinAuthServer:
    """A Jellyfin server's sign-in endpoints. ``approved`` flips the Quick
    Connect request; ``qc_enabled`` False answers Initiate with 401 as 10.9
    does; ``passwords`` maps name -> (password, fixture). ``logouts`` collects
    the tokens Curatarr closed; ``devices`` the DeviceIds it presented."""

    def __init__(self, qc_enabled=True, approved=False, qc_user="jane"):
        self.qc_enabled, self.approved, self.qc_user = qc_enabled, approved, qc_user
        self.passwords = {"jane": ("hunter2", "jane"), "Sam": ("pw", "sam")}
        self.logouts, self.devices, self.bodies = [], [], []

    def transport(self) -> httpx.MockTransport:
        import re as _re
        secret = load("jellyfin", "QuickConnect_Initiate.json")["Secret"]

        def handler(request: httpx.Request) -> httpx.Response:
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("MediaBrowser "):
                return httpx.Response(400)
            m = _re.search(r'DeviceId="([^"]+)"', auth)
            self.devices.append(m.group(1) if m else None)
            path = request.url.path
            body = json.loads(request.content) if request.content else {}
            self.bodies.append((path, body))
            if path == "/QuickConnect/Enabled":
                return httpx.Response(200, json=self.qc_enabled)
            if path == "/QuickConnect/Initiate":
                if not self.qc_enabled:
                    return httpx.Response(401)
                return httpx.Response(200, json=load("jellyfin", "QuickConnect_Initiate.json"))
            if path == "/QuickConnect/Connect":
                if request.url.params.get("Secret") != secret:
                    return httpx.Response(404)
                return httpx.Response(200, json={**load("jellyfin", "QuickConnect_Initiate.json"),
                                                 "Authenticated": self.approved})
            if path == "/Users/AuthenticateWithQuickConnect":
                if body.get("Secret") != secret or not self.approved:
                    return httpx.Response(401)
                return httpx.Response(200, json=load("jellyfin", f"AuthenticationResult_{self.qc_user}.json"))
            if path == "/Users/AuthenticateByName":
                want = self.passwords.get(body.get("Username"))
                if not want or want[0] != body.get("Pw"):
                    return httpx.Response(401)
                return httpx.Response(200, json=load("jellyfin", f"AuthenticationResult_{want[1]}.json"))
            if path == "/Sessions/Logout":
                t = _re.search(r'Token="([^"]+)"', auth)
                self.logouts.append(t.group(1) if t else None)
                return httpx.Response(204)
            return httpx.Response(404)
        return httpx.MockTransport(handler)


class JellyfinWriteServer:
    """A Jellyfin that remembers writes: playlists (per owner, with entry
    ids), collections, played marks and deletions, on top of the recorded
    library. Searches, next-episode and unheard-album reads answer from the
    library fixtures. ``calls`` logs (method, path, params)."""

    def __init__(self, api_key: str = "jf-key", deny_delete: bool = False):
        self.api_key, self.deny_delete = api_key, deny_delete
        self.playlists, self.collections = {}, {}
        self.played, self.deleted, self.calls = set(), set(), []
        self._n = 0
        self.lib_of, self.items = {}, {}
        for p in sorted((FIXTURES / "jellyfin").glob("*.json")):
            if p.name.startswith(("Items_", "Artists_")) and not p.name.startswith("Items_played_"):
                coll = p.stem.split("_")[-2] if p.name.startswith("Items_") else p.stem.split("_")[-1]
                lib = next((k for k, v in JELLYFIN_LIBRARIES.items() if v == coll), None)
                for it in load("jellyfin", p.name).get("Items") or []:
                    self.items[it["Id"]] = it
                    self.lib_of[it["Id"]] = lib
        self._reads = jellyfin_transport(api_key)

    def _id(self) -> str:
        self._n += 1
        return f"{self._n:032x}"

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            q, path, m = request.url.params, request.url.path, request.method
            self.calls.append((m, path, dict(q)))
            if f'Token="{self.api_key}"' not in request.headers.get("Authorization", ""):
                return httpx.Response(401)
            body = json.loads(request.content) if request.content else {}

            def items(xs):
                return httpx.Response(200, json={"Items": xs, "TotalRecordCount": len(xs), "StartIndex": 0})

            types = set((q.get("IncludeItemTypes") or "").split(",")) - {""}
            if m == "GET" and path == "/Items" and types == {"Playlist"}:
                return items([{"Id": k, "Name": v["name"], "Type": "Playlist"}
                              for k, v in self.playlists.items() if v["user"] == q.get("userId")])
            if m == "GET" and path == "/Items" and types == {"BoxSet"}:
                return items([{"Id": k, "Name": v["name"], "Type": "BoxSet"} for k, v in self.collections.items()])
            if m == "GET" and path == "/Items" and q.get("ParentId") in self.collections:
                return items([self.items[i] for i in self.collections[q["ParentId"]]["items"] if i in self.items])
            if m == "GET" and path == "/Items" and q.get("SearchTerm"):
                term = q["SearchTerm"].casefold()
                hits = [it for k, it in self.items.items() if it.get("Type") in types
                        and term in it["Name"].casefold() and k not in self.deleted
                        and (not q.get("ParentId") or self.lib_of.get(k) == q["ParentId"])]
                return items(hits)
            if m == "GET" and path == "/Artists/AlbumArtists" and q.get("SearchTerm"):
                term = q["SearchTerm"].casefold()
                return items([it for it in self.items.values() if it.get("Type") == "MusicArtist"
                              and term in it["Name"].casefold()])
            if m == "GET" and path == "/Items" and types == {"Episode"} and q.get("userId"):
                eps = sorted((it for it in self.items.values() if it.get("Type") == "Episode"
                              and it.get("SeriesId") == q.get("ParentId")),
                             key=lambda e: (e.get("ParentIndexNumber") or 0, e.get("IndexNumber") or 0))
                if q.get("IsPlayed") == "false":
                    seen = {i["Id"] for i in load("jellyfin", f"Items_played_{q['userId']}.json")["Items"]} \
                        if (FIXTURES / "jellyfin" / f"Items_played_{q['userId']}.json").exists() else set()
                    eps = [e for e in eps if e["Id"] not in seen | self.played]
                return items(eps[:1])
            if m == "GET" and path == "/Items" and types == {"MusicAlbum"} and q.get("AlbumArtistIds"):
                return items([it for it in self.items.values() if it.get("Type") == "MusicAlbum"
                              and any(a["Id"] == q["AlbumArtistIds"] for a in it.get("AlbumArtists") or [])][:1])
            if m == "POST" and path == "/Playlists":
                pid = self._id()
                self.playlists[pid] = {"name": body["Name"], "user": body["UserId"], "media": body.get("MediaType"),
                                       "items": list(body.get("Ids") or [])}
                return httpx.Response(200, json={"Id": pid})
            if path.startswith("/Playlists/") and path.endswith("/Items"):
                pid = path.split("/")[2]
                pl = self.playlists.get(pid)
                if pl is None:
                    return httpx.Response(404)
                if m == "GET":
                    return items([{**self.items.get(i, {"Id": i}), "PlaylistItemId": f"e-{i}"} for i in pl["items"]])
                if m == "POST":
                    pl["items"] += [i for i in q["ids"].split(",") if i]
                    return httpx.Response(204)
                if m == "DELETE":
                    drop = {e.removeprefix("e-") for e in q["entryIds"].split(",")}
                    pl["items"] = [i for i in pl["items"] if i not in drop]
                    return httpx.Response(204)
            if m == "POST" and path == "/Collections":
                cid = self._id()
                self.collections[cid] = {"name": q["Name"], "items": [i for i in (q.get("Ids") or "").split(",") if i]}
                return httpx.Response(200, json={"Id": cid})
            if path.startswith("/Collections/") and path.endswith("/Items"):
                coll = self.collections[path.split("/")[2]]
                ids = [i for i in q["ids"].split(",") if i]
                coll["items"] = (coll["items"] + ids) if m == "POST" else [i for i in coll["items"] if i not in ids]
                return httpx.Response(204)
            if m == "POST" and path.startswith("/UserPlayedItems/"):
                self.played.add(path.split("/")[2])
                return httpx.Response(200, json={"Played": True})
            if m == "DELETE" and path.startswith("/Items/"):
                k = path.split("/")[2]
                if k in self.playlists:
                    self.playlists.pop(k)
                elif k in self.collections:
                    self.collections.pop(k)
                elif self.deny_delete:
                    return httpx.Response(204)          # "deleted", but it stays: no deletion right
                elif k in self.items:
                    self.deleted.add(k)
                else:
                    return httpx.Response(404)
                return httpx.Response(204)
            if m == "GET" and path == "/Items" and q.get("Ids"):
                want = [i for i in q["Ids"].split(",") if i not in self.deleted]
                return items([self.items[i] for i in want if i in self.items])
            return self._reads.handle_request(request)
        return httpx.MockTransport(handler)
