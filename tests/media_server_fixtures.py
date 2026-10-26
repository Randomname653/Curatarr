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


def jellyfin_transport(api_key: str = "jf-key", log: list = None) -> httpx.MockTransport:
    """Answers the Jellyfin paths the adapter reads; 401 unless the
    MediaBrowser header carries the key, 404 for anything not recorded.
    ``/Items?Ids=`` and ``/Items?ParentId=`` (no Recursive) are answered
    from the recorded library walks."""
    def every_item() -> list:
        out = []
        for p in sorted((FIXTURES / "jellyfin").glob("*.json")):
            if p.name.startswith(("Items_", "Artists_")):
                out += load("jellyfin", p.name).get("Items") or []
        return out

    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append((request.url.path, dict(request.url.params)))
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("MediaBrowser ") or f'Token="{api_key}"' not in auth:
            return httpx.Response(401)
        path, q = request.url.path, request.url.params
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
