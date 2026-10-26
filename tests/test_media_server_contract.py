"""One contract, two adapters (docs/JELLYFIN_DESIGN.md D8, phase 1b).

The Plex and Jellyfin fixture sets (tests/fixtures/) describe the SAME
library: same titles, same files, mounted at /data/media/... on Plex and
/media/... on Jellyfin. Both adapters must:

- satisfy MediaServerAdapter and answer the same read questions the same way
  (libraries, kinds, external ids, files, numbers, users, missing keys);
- arrive at the same canonical keys for the whole library, which is what a
  rebind-server run matches on, the double episode included (Plex: two
  items, Jellyfin: one item with IndexNumberEnd);
- refuse a wrong credential loudly, never as an empty library;
- hand out keys only in the server's own stored spelling.

Plus the Jellyfin specifics: the MediaBrowser header, /Items?userId-free
reads, ticks and ISO dates, dashed GUIDs, /Sessions.

    python tests/test_media_server_contract.py
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from media_server_fixtures import jellyfin_transport, plex_transport  # noqa: E402
from src.services.media_server import base as b, id_map  # noqa: E402
from src.services.media_server.jellyfin_adapter import JellyfinAdapter  # noqa: E402
from src.services.media_server.plex_adapter import PlexAdapter  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def run(coro):
    return asyncio.run(coro)


async def walk(adapter):
    libs = await adapter.libraries()
    items = []
    for lib in libs:
        items += [it async for it in adapter.items(lib)]
    return libs, items


jlog, plog = [], []
ADAPTERS = {
    "plex": PlexAdapter("http://plex:32400", "plex-token", transport=plex_transport(log=plog)),
    "jellyfin": JellyfinAdapter("http://jf:8096/", "jf-key", transport=jellyfin_transport(log=jlog)),
}
WALK = {name: run(walk(a)) for name, a in ADAPTERS.items()}

for name, a in ADAPTERS.items():
    print(f"\n-- contract: {name} --")
    libs, items = WALK[name]
    by = {(i.kind, i.title): i for i in items}
    check("satisfies the protocol", isinstance(a, b.MediaServerAdapter) and a.kind == name)
    info = run(a.ping())
    check("ping names the server and its id", info.kind == name and info.name == "nas"
          and info.version and info.server_id)
    check("movie, show and music libraries, in that order; others skipped",
          [lib.kind for lib in libs] == ["movie", "show", "music"]
          and all(lib.roots and a.is_server_key(lib.key) for lib in libs))
    check("the library walk yields every modelled kind",
          {i.kind for i in items} == {"movie", "series", "season", "episode", "artist", "album", "track"})
    heat = by[("movie", "Heat")]
    check("Heat: TMDB + IMDb, year, genres, one file with its size, the added date",
          heat.external_ids.tmdb_id == 949 and heat.external_ids.imdb_id == "tt0113277"
          and heat.year == 1995 and heat.genres == ("Crime", "Thriller")
          and len(heat.files) == 1 and heat.files[0].size_bytes == 7340032000
          and heat.files[0].path.endswith("/Heat (1995)/Heat (1995).mkv")
          and heat.added_at == datetime(2023, 11, 14, 22, 13, 20) and heat.duration_ms == 10_200_000)
    ep1 = by[("episode", "Secrets")]
    check("an episode: the TVDB id is the episode's, season/episode numbers, both parents",
          ep1.external_ids.tvdb_episode_id == 6216512 and ep1.external_ids.tvdb_id is None
          and (ep1.season_number, ep1.episode_number) == (1, 1)
          and a.is_server_key(ep1.parent_key) and a.is_server_key(ep1.grandparent_key)
          and ep1.grandparent_title == "Dark")
    dark = by[("series", "Dark")]
    check("a series carries TVDB, TMDB and IMDb, and no file (a folder is no media file)",
          dark.external_ids.tvdb_id == 334824 and dark.external_ids.tmdb_id == 70523
          and dark.external_ids.imdb_id == "tt5753856" and dark.files == ())
    joga = by[("track", "Jóga")]
    check("a track: disc and track number, album and album artist as parents",
          (joga.disc_number, joga.track_number) == (1, 2) and joga.parent_title == "Homogenic"
          and joga.grandparent_title == "Björk" and a.is_server_key(joga.grandparent_key))
    check("every key handed out is in the server's own spelling",
          all(a.is_server_key(k) for i in items for k in (i.key, i.parent_key, i.grandparent_key) if k))
    check("item() finds a walked key; children() lists a series' seasons",
          run(a.item(heat.key)).title == "Heat"
          and [c.kind for c in run(a.children(dark.key))] == ["season"])
    other = "0123456789abcdef0123456789abcdef" if name == "jellyfin" else "999999"
    check("an unknown key: item() None, key_exists() False; junk never reaches the server",
          run(a.item(other)) is None and run(a.key_exists(other)) is False
          and run(a.key_exists(heat.key)) is True and run(a.item("../etc")) is None)
    users = run(a.users())
    check("users: jane is the admin, Sam is not",
          [(u.name, u.is_admin) for u in users] == [("jane", True), ("Sam", False)])
    check("deep links point into the server's web app",
          (a.deep_link(heat.key) or "").startswith(("http://plex:32400/web/", "http://jf:8096/web/"))
          and a.deep_link("../x") is None)
    bad = (PlexAdapter("http://plex:32400", "nope", transport=plex_transport()) if name == "plex"
           else JellyfinAdapter("http://jf:8096", "nope", transport=jellyfin_transport()))
    try:
        run(bad.libraries())
        loud = False
    except Exception as e:  # noqa: BLE001 - the raise IS the assertion
        loud = "401" in str(e)
    check("a wrong credential raises with the status, never an empty library", loud)
    later_here = (("upsert_playlist", (users[0], "t", [])), ("delete_item", (heat.key,)))
    if name == "plex":       # Jellyfin's arrived with phase 3; Plex's move behind the seam later
        later_here = (("user_states", (users[0],)), ("plays", (users[0],))) + later_here
    for meth, args in later_here:
        try:
            run(getattr(a, meth)(*args))
            later = False
        except NotImplementedError:
            later = True
        check(f"{meth} is honestly not there yet (phases 3/4)", later)

print("\n-- the rebind's question: same library, same canonical keys --")


def keys(name):
    libs, items = WALK[name]
    roots = {lib.key: lib.roots for lib in libs}
    return {(t, k) for t, k, _ in id_map.item_rows(items, roots)}


pk, jk = keys("plex"), keys("jellyfin")
check(f"both servers file the library under the same canonical keys (only plex: {sorted(pk - jk)}, "
      f"only jellyfin: {sorted(jk - pk)})", pk == jk and len(pk) == 11)   # 2 films, 1+1+3 TV, 1+1+2 music
check("the id-less film matches by its file below the library root, across mount points",
      ("movie", "file:home movie (2003)/home.mp4|123456") in pk & jk)
jl, ji = WALK["jellyfin"]
jrows = id_map.item_rows(ji, {lib.key: lib.roots for lib in jl})
double = [k for t, k, sid in jrows if t == "episode" and sid == "3b4c5d6e7f8091a20b1c2d3e4f506172"]
check("Jellyfin's one double-episode item stands for both of Plex's episodes",
      sorted(double) == ["tvdb:334824/s01e02", "tvdb:334824/s01e03"])

print("\n-- Jellyfin specifics --")
ja = ADAPTERS["jellyfin"]
check("capabilities: no per-play history in core, live sessions yes, no star ratings",
      not ja.capabilities().per_play_history and ja.capabilities().now_playing
      and not ja.capabilities().star_ratings and not ja.capabilities().per_user_tokens)
check("no 10.8 route: nothing under /Users/{id}/Items",
      not any(p.startswith("/Users/") for p, _ in jlog))
check("library walks ask for the fields a rebind needs",
      all({"ProviderIds", "MediaSources", "Path", "DateCreated"} <= set(q.get("Fields", "").split(","))
          for p, q in jlog if p == "/Items" and q.get("Recursive") == "true" and "userId" not in q))
check("artists come from /Artists/AlbumArtists", any(p == "/Artists/AlbumArtists" for p, _ in jlog))
check("the dashed upper-case artist id is stored in the one spelling",
      [i for i in ji if i.title == "Jóga"][0].grandparent_key == "6d7e8f90a1b2c3d4e5f60718293a4b5c")
check("the double episode keeps its end number",
      [i for i in ji if i.kind == "episode" and i.episode_number == 2][0].episode_number_end == 3)
sessions = run(ja.live_sessions())
check("live sessions: what plays, where, how far; an idle session is skipped",
      len(sessions) == 1 and sessions[0].item_key == "2a3b4c5d6e7f80910a1b2c3d4e5f6071"
      and sessions[0].position_ms == 2_550_000 and sessions[0].duration_ms == 3_000_000
      and sessions[0].device == "Pixel 8" and sessions[0].client == "Jellyfin Android"
      and not sessions[0].paused)
check("the deep link carries the server id after ping",
      ja.deep_link("4b1d2f3e5a6c7b8d9e0f1a2b3c4d5e6f")
      == "http://jf:8096/web/#/details?id=4b1d2f3e5a6c7b8d9e0f1a2b3c4d5e6f&serverId=d4e3c2b1a0f9e8d7c6b5a4938271605f")
from src.services.media_server import jellyfin_adapter as jfa  # noqa: E402
check("ISO dates with seven fractional digits parse to naive UTC",
      jfa._ts("2023-11-14T22:13:20.1234567Z") == datetime(2023, 11, 14, 22, 13, 20, 123456)
      and jfa._ts("garbage") is None and jfa._ts(None) is None)
check("a mixed library (no CollectionType) is skipped",
      "Mixed" not in [lib.title for lib in jl])

print("\n-- get_adapter picks Jellyfin --")
from types import SimpleNamespace  # noqa: E402
from src.services.media_server import MediaServerNotConfigured, get_adapter  # noqa: E402

cfg = SimpleNamespace(MEDIA_SERVER_TYPE="jellyfin", JELLYFIN_URL="http://jf:8096",
                      JELLYFIN_API_KEY=SimpleNamespace(get_secret_value=lambda: "jf-key"),
                      effective_plex_url="http://plex:32400", effective_plex_token="t")
got = get_adapter(cfg, transport=jellyfin_transport())
check("MEDIA_SERVER_TYPE=jellyfin gives the Jellyfin adapter, with the secret unwrapped",
      isinstance(got, JellyfinAdapter) and run(got.ping()).kind == "jellyfin")
try:
    get_adapter(SimpleNamespace(**{**cfg.__dict__, "JELLYFIN_API_KEY": None}))
    nc = False
except MediaServerNotConfigured:
    nc = True
check("Jellyfin without its key is reported, not half-built", nc)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
