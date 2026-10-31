"""The media-server seam (docs/JELLYFIN_DESIGN.md, phase 1a, 2026-10-26).

- outside src/services/media_server/ no NEW code builds a media-server URL:
  the files that still do are listed with their count, which may only go
  down (a ratchet, like test_silent_except_ratchet started out);
- key_namespace tells a server key from the other values the plex_* key
  columns hold; a Jellyfin GUID must read as a server key, which the
  isdigit() guesses it replaced got wrong (design Appendix A.3);
- Jellyfin GUIDs have one stored spelling, 32 lower-case hex;
- canonical keys: strongest id first, the file stem ignores the mount
  point, a double episode / single-file album gets one key per item, a
  title alone never makes a key;
- the deletion path checks server keys against the server's own shape;
- PlexAdapter's read path over recorded payloads, and get_adapter().

    python tests/test_media_server_seam.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services.media_server import base as b  # noqa: E402

from _checks import check  # noqa: E402


GUID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"

print("\n-- keys --")
check("a Plex ratingKey is a server key", b.key_namespace("48291") == "server")
check("a Jellyfin GUID is a server key", b.key_namespace(GUID) == "server")
check("*arr forms are arr, the ledger's episode form too",
      b.key_namespace("radarr:12") == "arr" and b.key_namespace("sonarr:ep:991") == "arr"
      and b.key_namespace("lidarr:3") == "arr")
check("placeholders keep their own namespace",
      b.key_namespace("spotify:track:4uLU6hMCjMI75M1A2tKUQC") == "spotify"
      and b.key_namespace("netflix:" + "0" * 24) == "netflix"
      and b.key_namespace("ext-script:foo") == "ext-script")
check("empty, None, a leading zero, a dashed GUID are not server keys",
      b.key_namespace("") == "unknown" and b.key_namespace(None) == "unknown"
      and b.key_namespace("0123") == "unknown"
      and b.key_namespace("a1b2c3d4-e5f6-0718-293a-4b5c6d7e8f90") == "unknown")
check("an upper-case GUID is not the stored spelling", not b.is_jellyfin_key(GUID.upper()))
check("normalize: dashed / upper-case become the stored spelling",
      b.normalize_jellyfin_id("A1B2C3D4-E5F6-0718-293A-4B5C6D7E8F90") == GUID
      and b.normalize_jellyfin_id(GUID) == GUID)
check("normalize refuses what is not a GUID",
      b.normalize_jellyfin_id("48291") is None and b.normalize_jellyfin_id(GUID + "0") is None
      and b.normalize_jellyfin_id(None) is None and b.normalize_jellyfin_id("g" * 32) is None)
check("Plex keys keep the deletion path's bound (ten digits)",
      b.is_plex_key("9999999999") and not b.is_plex_key("10000000000"))

print("\n-- files --")
plex_file = b.FileRef("/data/media/movies/Heat (1995)/Heat.mkv", 7_340_032_000)
jf_file = b.FileRef("D:\\Media\\Movies\\Heat (1995)\\Heat.mkv", 7_340_032_000)
check("the same file under two mount points has one stem key",
      plex_file.stem_key(["/data/media/movies"]) == jf_file.stem_key(["D:/Media/Movies"]))
check("the longest matching root wins",
      b.FileRef("/m/movies/x/a.mkv").relative_to(["/m", "/m/movies"]) == "x/a.mkv")
check("a root that is only a name prefix does not match",
      b.FileRef("/m/movies2/a.mkv").relative_to(["/m/movies"]) == "m/movies2/a.mkv")
check("a different size is a different stem key",
      plex_file.stem_key(["/data/media/movies"])
      != b.FileRef(plex_file.path, 1).stem_key(["/data/media/movies"]))

print("\n-- canonical keys --")
mv = b.MediaItem(key="48291", kind="movie", title="Heat",
                 external_ids=b.ExternalIds(tmdb_id=949, imdb_id="tt0113277"),
                 files=(plex_file,))
check("a movie keys on TMDB first", b.canonical_key(mv) == "tmdb:949")
mv_imdb = b.MediaItem(key=GUID, kind="movie", title="Heat",
                      external_ids=b.ExternalIds(imdb_id="tt0113277"))
check("then IMDb", b.canonical_key(mv_imdb) == "imdb:tt0113277")
mv_file = b.MediaItem(key=GUID, kind="movie", title="Heat", files=(jf_file,))
check("then the file stem, independent of the mount point",
      b.canonical_key(mv_file, roots=["D:/Media/Movies"])
      == b.canonical_key(b.MediaItem(key="1", kind="movie", title="x", files=(plex_file,)),
                         roots=["/data/media/movies"]))
check("a title alone never makes a key",
      b.canonical_key(b.MediaItem(key="1", kind="movie", title="Heat", year=1995)) is None)
sr = b.MediaItem(key="10", kind="series", title="Dark", external_ids=b.ExternalIds(tvdb_id=334824))
series_ck = b.canonical_key(sr)
check("a series keys on TVDB", series_ck == "tvdb:334824")
ep = b.MediaItem(key="11", kind="episode", title="Secrets", season_number=1, episode_number=1)
check("an episode without its own id keys on series + numbers",
      b.canonical_key(ep, parent_canonical=series_ck) == "tvdb:334824/s01e01")
check("an episode with a TVDB episode id keys on it",
      b.canonical_key(b.MediaItem(key="11", kind="episode", title="x", season_number=1,
                                  episode_number=1,
                                  external_ids=b.ExternalIds(tvdb_episode_id=6216512)),
                      parent_canonical=series_ck) == "tvdb-ep:6216512")
check("a season needs its series",
      b.canonical_key(b.MediaItem(key="12", kind="season", title="S1", season_number=1)) is None)
ar = b.MediaItem(key="20", kind="artist", title="Björk")
check("an artist without an MBID keys on its normalized name",
      b.canonical_key(ar) == "name:björk")
al = b.MediaItem(key="21", kind="album", title="Homogenic!")
tr = b.MediaItem(key="22", kind="track", title="Jóga", track_number=2)
al_ck = b.canonical_key(al, parent_canonical=b.canonical_key(ar))
check("album and track chain to their parents",
      al_ck == "name:björk/homogenic"
      and b.canonical_key(tr, parent_canonical=al_ck) == "name:björk/homogenic/d1t02")

dbl = (b.FileRef("/tv/Dark/S01/Dark.S01E01E02.mkv", 99),)
e1 = b.MediaItem(key="31", kind="episode", title="a", season_number=1, episode_number=1, files=dbl)
e2 = b.MediaItem(key="32", kind="episode", title="b", season_number=1, episode_number=2, files=dbl)
check("a double episode on one file gets two keys (number after the stem)",
      b.canonical_key(e1, roots=["/tv"]) == "file:dark/s01/dark.s01e01e02.mkv|99#s01e01"
      and b.canonical_key(e2, roots=["/tv"]) == "file:dark/s01/dark.s01e01e02.mkv|99#s01e02")
cue = (b.FileRef("/music/Album.flac", 5),)
t1 = b.MediaItem(key="41", kind="track", title="x", track_number=1, files=cue)
t2 = b.MediaItem(key="42", kind="track", title="y", track_number=2, disc_number=2, files=cue)
check("a single-file album gets one key per track",
      b.canonical_key(t1, roots=["/music"]) == "file:album.flac|5#d1t01"
      and b.canonical_key(t2, roots=["/music"]) == "file:album.flac|5#d2t02")
check("an unnumbered episode on its own file keeps the bare stem",
      b.canonical_key(b.MediaItem(key="33", kind="episode", title="c", files=dbl), roots=["/tv"])
      == "file:dark/s01/dark.s01e01e02.mkv|99")

print("\n-- protocol --")
check("the protocol is runtime-checkable and names the write methods",
      all(hasattr(b.MediaServerAdapter, m) for m in
          ("upsert_playlist", "upsert_collection", "mark_played", "delete_item",
           "live_sessions", "user_states", "is_server_key")))

print("\n-- deletion path --")
from src.routers import recommendations as recs  # noqa: E402

check("arr ids keep the integer shape", recs._require_media_id("42") == "42"
      and recs._require_media_id(GUID) is None)
check("a Plex proposal accepts a rating key, refuses a GUID and junk",
      recs._require_media_id("48291", "plex") == "48291"
      and recs._require_media_id(GUID, "plex") is None
      and recs._require_media_id("../1", "plex") is None)
check("a Jellyfin proposal accepts only the stored GUID spelling",
      recs._require_media_id(GUID, "jellyfin") == GUID
      and recs._require_media_id(GUID.upper(), "jellyfin") is None
      and recs._require_media_id("48291", "jellyfin") is None
      and recs._require_media_id("", "jellyfin") is None)
limbo = (ROOT / "src/services/limbo_reconcile.py").read_text(encoding="utf-8")
check("limbo checks the id against its proposal's service",
      "_require_media_id(media_id, service)" in limbo)

print("\n-- A.3: the isdigit() guesses are gone --")
sync_src = (ROOT / "src/services/plex_sync.py").read_text(encoding="utf-8")
check("the orphan sweep asks key_namespace, not isdigit",
      'if key_namespace(rk) != "server":' in sync_src and "if not rk.isdigit():" not in sync_src)
from src.services import netflix_match as nm  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from datetime import datetime  # noqa: E402
from src.database.models import (ArrEnrichmentStatus as AES, Base, User,  # noqa: E402
                                 WatchHistoryEntry as W)

from _checks import memory_engine  # noqa: E402
eng = memory_engine()
Base.metadata.create_all(eng)
db = sessionmaker(bind=eng)()
db.add(User(id=1, plex_user_id="u1", plex_username="jane"))
for i, (t, k) in enumerate((("Heat", GUID), ("Ronin", "48291"), ("Alien", "netflix:" + "a" * 24))):
    db.add(W(user_id=1, plex_user_id="u1", plex_item_id=k, title=t, media_type="movie",
             viewed_at=datetime(2026, 1, 1 + i)))
    db.add(AES(service="radarr", arr_id=10 + i, category="movie", title=t))
db.commit()
cat = nm.catalog(db)
films = {e["title"]: e["key"] for e in cat["movie"].values()}
check("a Jellyfin film's key enters the Netflix catalogue, a Plex one's still does, "
      "a placeholder falls back to the arr form",
      films.get("Heat") == GUID and films.get("Ronin") == "48291" and films.get("Alien") == "radarr:12")
enr = (ROOT / "src/services/media_enricher.py").read_text(encoding="utf-8")
check("the enricher never takes a key for an alternative title",
      'key_namespace(str(cache_id)) == "unknown"' in enr)
from src.database import models as M  # noqa: E402
check("A.2: the short key columns are 64 wide",
      M.LibraryConfig.__table__.c.plex_section_key.type.length == 64
      and M.MusicTag.__table__.c.item_key.type.length == 64
      and M.MusicTag.__table__.c.section_key.type.length == 64
      and M.MediaIdentity.__table__.c.plex_rating_key.type.length == 64)

print("\n-- seam guard --")
import ast  # noqa: E402
import re  # noqa: E402

_URL = re.compile(r"/library/|/status/sessions|X-Plex-Token|^/Items\b|/Items[/?]|MediaBrowser ")
# Files that still talk to a media server directly, and how many string
# literals they spend on it. A count may only go DOWN as callers move onto
# the adapter; a file not listed here may not start.
RATCHET = {
    "src/routers/auth.py": 3,
    "src/routers/libraries.py": 3,
    "src/routers/library.py": 3,
    "src/routers/recommendations.py": 5,
    "src/services/arr_client.py": 4,
    "src/services/limbo_reconcile.py": 2,
    "src/services/lyrics.py": 6,
    "src/services/music_matcher.py": 2,
    "src/services/music_tags.py": 4,
    "src/services/orphan_repair.py": 4,
    "src/services/plex_collections.py": 5,
    "src/services/plex_players.py": 2,
    "src/services/plex_playlists.py": 9,
    "src/services/plex_sync.py": 15,
    "src/services/plex_watchlist.py": 2,
    "src/services/setup_wizard.py": 1,
    "src/services/subtitle_signals.py": 5,
}
# SoulSync's own REST API has /library/ paths too; it is no media server.
NOT_A_MEDIA_SERVER = {"src/services/soulsync_client.py"}


def url_literals(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body:
            first = n.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docs.add(id(first.value))
    return sum(1 for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and id(n) not in docs and _URL.search(n.value))


found = {}
for p in sorted((ROOT / "src").rglob("*.py")):
    rel = p.relative_to(ROOT).as_posix()
    if rel.startswith("src/services/media_server/") or rel in NOT_A_MEDIA_SERVER:
        continue
    n = url_literals(p)
    if n:
        found[rel] = n
new = sorted(set(found) - set(RATCHET))
grown = sorted(f"{f} {RATCHET[f]}->{n}" for f, n in found.items() if f in RATCHET and n > RATCHET[f])
shrunk = sorted(f"{f} {RATCHET[f]}->{found.get(f, 0)}" for f in RATCHET if found.get(f, 0) < RATCHET[f])
check(f"no new file builds media-server URLs outside the seam (new: {new})", not new)
check(f"no listed file builds more of them (grown: {grown})", not grown)
check(f"the ratchet is tight - lower the counts that shrank (shrunk: {shrunk})", not shrunk)
pkg = ROOT / "src/services/media_server"
check("the adapters themselves are where the URLs live",
      url_literals(pkg / "plex_adapter.py") > 0 and url_literals(pkg / "jellyfin_adapter.py") > 0)

print("\n-- PlexAdapter over recorded payloads --")
import asyncio  # noqa: E402
from media_server_fixtures import plex_transport  # noqa: E402
from src.services.media_server.plex_adapter import PlexAdapter  # noqa: E402

log = []
pa = PlexAdapter("http://plex:32400/", "plex-token", transport=plex_transport(log=log))
check("PlexAdapter satisfies the protocol", isinstance(pa, b.MediaServerAdapter))


async def _collect(it):
    return [x async for x in it]


info = asyncio.run(pa.ping())
check("ping reads name, version and machine id",
      info.kind == "plex" and info.name == "nas" and info.server_id.startswith("6f1d0c2e")
      and info.version.startswith("1.41"))
check("after ping the deep link names the server",
      pa.deep_link("1001") == "http://plex:32400/web/index.html#!/server/"
      "6f1d0c2e9b3a4f5d8e7c1b2a3d4e5f60/details?key=%2Flibrary%2Fmetadata%2F1001"
      and pa.deep_link(GUID) is None)
libs = asyncio.run(pa.libraries())
check("libraries: movie, show, music with their roots; photos skipped",
      [(lib.key, lib.kind) for lib in libs] == [("1", "movie"), ("2", "show"), ("3", "music")]
      and libs[0].roots == ("/data/media/movies",))
movies = asyncio.run(_collect(pa.items(libs[0])))
heat = movies[0]
check("a movie carries every id, its file and size, genres, the added date",
      heat.key == "1001" and heat.external_ids.tmdb_id == 949
      and heat.external_ids.imdb_id == "tt0113277"
      and heat.files == (b.FileRef("/data/media/movies/Heat (1995)/Heat (1995).mkv", 7340032000),)
      and heat.genres == ("Crime", "Thriller") and heat.added_at == datetime(2023, 11, 14, 22, 13, 20)
      and heat.library_key == "1")
check("items() asks for Guids (includeGuids=1)",
      any(p == "/library/sections/1/all" and q.get("includeGuids") == "1" for p, q in log))
shows = asyncio.run(_collect(pa.items(libs[1])))
kinds = [i.kind for i in shows]
eps = [i for i in shows if i.kind == "episode"]
check("a show library walks series, seasons, episodes",
      kinds == ["series", "season", "episode", "episode", "episode"])
check("an episode's tvdb guid is the episode id, numbers and parents are kept",
      eps[0].external_ids.tvdb_episode_id == 6216512 and eps[0].external_ids.tvdb_id is None
      and (eps[0].season_number, eps[0].episode_number) == (1, 1)
      and eps[0].parent_key == "2002" and eps[0].grandparent_key == "2001")
music = asyncio.run(_collect(pa.items(libs[2])))
track = [i for i in music if i.kind == "track"][1]
check("a track keeps disc / track numbers and its parents' titles",
      (track.disc_number, track.track_number) == (1, 2) and track.grandparent_title == "Björk")
check("item(): a recorded key, a 404 is None, a junk key is None without a request",
      asyncio.run(pa.item("1001")).title == "Heat" and asyncio.run(pa.item("9999")) is None
      and asyncio.run(pa.item("../x")) is None)
check("children() of a series are its seasons",
      [c.kind for c in asyncio.run(pa.children("2001"))] == ["season"])
check("key_exists: 200 yes, 404 no",
      asyncio.run(pa.key_exists("1001")) is True and asyncio.run(pa.key_exists("9999")) is False)
users = asyncio.run(pa.users())
check("users: account 1 is the admin, the nameless system entry is skipped",
      [(u.key, u.name, u.is_admin) for u in users] == [("1", "jane", True), ("216511115", "Sam", False)])
bad = PlexAdapter("http://plex:32400", "wrong", transport=plex_transport())
try:
    asyncio.run(bad.libraries())
    raised = False
except Exception as e:  # noqa: BLE001 - the raise IS the assertion
    raised = "401" in str(e)
check("a refused token raises - never an empty library", raised)
check("now_playing is off for Plex and live_sessions answers empty",
      not pa.capabilities().now_playing and asyncio.run(pa.live_sessions()) == [])

print("\n-- get_adapter --")
from types import SimpleNamespace  # noqa: E402
from src.services.media_server import (MediaServerNotConfigured, active_kind,  # noqa: E402
                                       get_adapter)

plex_cfg = SimpleNamespace(MEDIA_SERVER_TYPE=None, effective_plex_url="http://plex:32400",
                           effective_plex_token="t", JELLYFIN_URL=None, JELLYFIN_API_KEY=None)
check("unset MEDIA_SERVER_TYPE means plex", active_kind(plex_cfg) == "plex"
      and isinstance(get_adapter(plex_cfg), PlexAdapter))
try:
    get_adapter(SimpleNamespace(**{**plex_cfg.__dict__, "effective_plex_token": ""}))
    nc = False
except MediaServerNotConfigured:
    nc = True
check("a server without its token is reported, not half-built", nc)
from src.config import Settings  # noqa: E402
check("settings: MEDIA_SERVER_TYPE accepts plex/jellyfin, blank means unset",
      Settings(_env_file=None, MEDIA_SERVER_TYPE="jellyfin").MEDIA_SERVER_TYPE == "jellyfin"
      and Settings(_env_file=None, MEDIA_SERVER_TYPE="").MEDIA_SERVER_TYPE is None)

check.done()
