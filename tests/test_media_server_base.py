"""The media-server seam's pure parts (docs/JELLYFIN_DESIGN.md, 2026-10-26).

- key_namespace tells a server key from the other values the plex_* key
  columns hold; a Jellyfin GUID must read as a server key, which the
  isdigit() guesses it replaces got wrong (design Appendix A.3);
- Jellyfin GUIDs have one stored spelling, 32 lower-case hex;
- canonical keys: strongest id first, the file stem ignores the mount
  point, a title alone never makes a key.

    python tests/test_media_server_base.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services.media_server import base as b  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


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

print("\n-- protocol --")
check("the protocol is runtime-checkable and names the write methods",
      all(hasattr(b.MediaServerAdapter, m) for m in
          ("upsert_playlist", "upsert_collection", "mark_played", "delete_item",
           "live_sessions", "user_states", "is_server_key")))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
