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
