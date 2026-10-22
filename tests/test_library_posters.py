"""The library browser shows posters (2026-10-22): each item carries the
arr's REMOTE poster url (the image proxy serves TVDB / fanart.tv / TMDB),
never the arr-local /MediaCover path the browser can't reach. And the phone
tab bar: the curator in the middle, Library in the drawer.

    python tests/test_library_posters.py
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.routers import library as lib  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


series = {"id": 1, "title": "Dark", "images": [
    {"coverType": "banner", "remoteUrl": "https://artworks.thetvdb.com/banners/b.jpg"},
    {"coverType": "poster", "url": "/MediaCover/1/poster.jpg",
     "remoteUrl": "https://artworks.thetvdb.com/banners/p.jpg"}]}
check("a series carries its remote poster, not the banner",
      lib._flatten_arr_item("sonarr", series, None)["poster"] == "https://artworks.thetvdb.com/banners/p.jpg")

local_only = {"id": 2, "title": "Local", "images": [{"coverType": "poster", "url": "/MediaCover/2/poster.jpg"}]}
check("an arr-local cover path is never handed to the browser",
      lib._flatten_arr_item("radarr", local_only, None)["poster"] == "")

artist = {"id": 3, "artistName": "Band", "images": [
    {"coverType": "cover", "remoteUrl": "https://assets.fanart.tv/fanart/music/x.jpg"}]}
check("an artist without a poster falls back to its cover",
      lib._flatten_arr_item("lidarr", artist, None)["poster"] == "https://assets.fanart.tv/fanart/music/x.jpg")
check("no images and no profile, no poster (the Plex music index)",
      lib._flatten_arr_item("lidarr", {"id": "9", "artistName": "Plex"}, None)["poster"] == "")
check("a Plex-index artist whose profile knows its Deezer id gets Deezer's picture",
      lib._flatten_arr_item("lidarr", {"id": "9", "artistName": "Plex"}, {"bio": "b", "deezer_id": "1424821"})["poster"]
      == "https://api.deezer.com/artist/1424821/image?size=big")
check("a junk Deezer id is no picture",
      lib._flatten_arr_item("lidarr", {"id": "9", "artistName": "Plex"}, {"deezer_id": "abc"})["poster"] == "")
check("an arr's own artwork still wins over Deezer",
      lib._flatten_arr_item("lidarr", artist, {"deezer_id": 5})["poster"] == "https://assets.fanart.tv/fanart/music/x.jpg")

html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
bar = re.search(r'<nav id="mobile-tabbar".*?</nav>', html, re.S).group(0)
views = re.findall(r'data-views="([^"]*)"', bar)
check("the phone tab bar: For you, Swipe, Chat in the middle, Deletions / History, More",
      views == ["recs", "swipe", "chat", "deletions", "history", ""]
      and 'class="mt-item mt-center" href="#chat"' in bar)
check("Library lives in the drawer, not the tab bar", "arr-sonarr" not in bar and "openLibrarySheet" not in bar)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
