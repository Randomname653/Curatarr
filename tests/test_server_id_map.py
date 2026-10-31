"""The id memory a server switch runs on (docs/JELLYFIN_DESIGN.md §8.2, phase 1c).

- every Plex library walk files its titles, libraries and users under
  server-independent canonical keys, with the Plex id beside each;
- seasons the walk did not list come from their episodes;
- a second identical walk only confirms; a server id that moved to another
  canonical key is released on its old row, never deleted;
- one server id may stand for two keys (a Jellyfin double episode), two
  server ids may not share one (the second copy is a duplicate);
- no file key without the library roots (an absolute path never matches the
  other server's mount point);
- the tech-profile walk feeds it, and a failure there costs nothing else.

    python tests/test_server_id_map.py
"""
import asyncio
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from src.database.models import Base, ServerIdMap as M, User  # noqa: E402
from src.services.media_server import base as b, id_map  # noqa: E402
from src.services.media_server.plex_adapter import PlexAdapter  # noqa: E402
from media_server_fixtures import plex_transport  # noqa: E402

from _checks import check  # noqa: E402


from _checks import memory_engine  # noqa: E402
eng = memory_engine()
Base.metadata.create_all(eng)
Session = sessionmaker(bind=eng)


@contextmanager
def fake_session():
    s = Session()
    try:
        yield s
        s.commit()          # the contract of get_db_session: commit on a clean exit
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()


import src.database.connection as conn  # noqa: E402
import src.database.runner as runner  # noqa: E402


async def _direct(fn, *a, **kw):
    # Off the event loop, as the real runner does: the battery's loop guard
    # fails any src/ coroutine that touches the database on the loop.
    return await asyncio.to_thread(fn, *a, **kw)


conn.get_db_session = fake_session
runner.run_read = _direct
runner.run_write = _direct

with fake_session() as db:
    db.add_all([User(id=1, plex_user_id="12345678", plex_username="jane"),
                User(id=2, plex_user_id="87654321", plex_username="Sam")])
    db.commit()


def rows_now() -> dict:
    with fake_session() as db:
        return {(m.entity_type, m.canonical_key): (m.plex_id, m.jellyfin_id, m.match_method)
                for m in db.query(M).all()}


async def walk(adapter) -> list:
    out = []
    for lib in await adapter.libraries():
        out += [it async for it in adapter.items(lib)]
    return out


print("\n-- a Plex walk fills the map --")
pa = PlexAdapter("http://plex:32400", "plex-token", transport=plex_transport())
items = asyncio.run(walk(pa))
first = asyncio.run(id_map.record_walk("plex", items, adapter=pa))
m = rows_now()
check("movies by TMDB, the id-less one by its file below the library root",
      m[("movie", "tmdb:949")][:2] == ("1001", None)
      and m[("movie", "file:home movie (2003)/home.mp4|123456")] == ("1002", None, "file"))
check("series, season and episodes chain to TVDB",
      m[("series", "tvdb:334824")][0] == "2001" and m[("season", "tvdb:334824/s01")][0] == "2002"
      and m[("episode", "tvdb-ep:6216512")][0] == "2003"
      and m[("episode", "tvdb:334824/s01e02")][0] == "2004"
      and m[("episode", "tvdb:334824/s01e03")][0] == "2005")
check("music: artist and album by MBID, a track without one by album + number",
      m[("artist", "mb-artist:87c5dedd-371d-4a53-9f7f-80522fb7f3cb")][0] == "3001"
      and m[("album", "mb-rg:2c5c5a5b-6f5e-37b2-9a3e-3b2a5a3c1d6e")][0] == "3002"
      and m[("track", "mb-rec:e8e0b8e3-4c1b-4f3c-9d2a-1b2c3d4e5f60")][0] == "3003"
      and m[("track", "mb-rg:2c5c5a5b-6f5e-37b2-9a3e-3b2a5a3c1d6e/d1t02")][0] == "3004")
check("libraries and Curatarr users are filed too",
      m[("library", "library:movie:movies")][0] == "1"
      and m[("user", "user:1")][:2] == ("12345678", None) and m[("user", "user:2")][0] == "87654321")
check("the counts say what happened", first["added"] == len(m) and first["duplicates"] == 0)
check("match methods are recorded",
      m[("movie", "tmdb:949")][2] == "external_id" and m[("user", "user:1")][2] == "server")

print("\n-- a second walk only confirms --")
again = asyncio.run(id_map.record_walk("plex", items, adapter=pa))
check("nothing added, nothing changed, everything confirmed",
      again["added"] == 0 and again["updated"] == 0 and again["released"] == 0
      and again["verified"] == len(m) and rows_now() == m)

print("\n-- ids that move --")
gained = b.MediaItem(key="1002", kind="movie", title="Home Movie",
                     external_ids=b.ExternalIds(tmdb_id=4242), library_key="1")
with fake_session() as db:
    moved = id_map.upsert(db, "plex", id_map.item_rows([gained], {"1": ("/data/media/movies",)}))
m2 = rows_now()
check("a title that gained an id is filed under it; the old row stays, released",
      moved["released"] == 1 and m2[("movie", "tmdb:4242")][0] == "1002"
      and m2[("movie", "file:home movie (2003)/home.mp4|123456")][0] is None)
check("nothing was deleted", len(m2) == len(m) + 1)

print("\n-- one id, two keys; two ids, one key --")
jf_double = b.MediaItem(key="3b4c5d6e7f8091a20b1c2d3e4f506172", kind="episode", title="x",
                        season_number=1, episode_number=2, episode_number_end=3,
                        parent_key="s", grandparent_key="dark", library_key="tv")
series = b.MediaItem(key="dark", kind="series", title="Dark",
                     external_ids=b.ExternalIds(tvdb_id=334824), library_key="tv")
rows = id_map.item_rows([series, jf_double], {"tv": ("/media/tv",)})
check("a Jellyfin double episode files both episode numbers under its one id",
      ("episode", "tvdb:334824/s01e02", jf_double.key) in rows
      and ("episode", "tvdb:334824/s01e03", jf_double.key) in rows)
with fake_session() as db:
    jf = id_map.upsert(db, "jellyfin", rows)
m3 = rows_now()
check("the Jellyfin side lands beside the Plex ids of the same episodes",
      m3[("episode", "tvdb:334824/s01e02")][:2] == ("2004", jf_double.key)
      and m3[("episode", "tvdb:334824/s01e03")][:2] == ("2005", jf_double.key)
      and jf["released"] == 0)
copy4k = b.MediaItem(key="1099", kind="movie", title="Heat", library_key="9",
                     external_ids=b.ExternalIds(tmdb_id=949))
heat = b.MediaItem(key="1001", kind="movie", title="Heat", library_key="1",
                   external_ids=b.ExternalIds(tmdb_id=949))
with fake_session() as db:
    dup = id_map.upsert(db, "plex", id_map.item_rows([heat, copy4k], {}))
check("a second copy of one film is a duplicate; the first keeps the row",
      dup["duplicates"] == 1 and rows_now()[("movie", "tmdb:949")][0] == "1001")

print("\n-- no roots, no file keys --")
nameless = b.MediaItem(key="77", kind="movie", title="x", library_key="unknown",
                       files=(b.FileRef("/srv/x.mkv", 1),))
check("a file key needs the library's roots", id_map.item_rows([nameless], {}) == [])

print("\n-- the tech walk feeds it --")
src = (ROOT / "src/services/plex_sync.py").read_text(encoding="utf-8")
hook = src[src.index("async def sync_tech_profiles"):]
check("sync_tech_profiles files its items and its show items",
      hook.count("id_items.append(mi)") == 2 and 'id_map.record_walk("plex", id_items)' in hook)
check("an id-map failure is logged and costs the profiles nothing",
      hook.index("_persist_tech_profiles, agg)") < hook.index("record_walk")
      and "[id-map] server ids not filed this walk" in hook)
bad = PlexAdapter("http://plex:32400", "wrong", transport=plex_transport())
try:
    asyncio.run(id_map.record_walk("plex", items, adapter=bad))
    raised = False
except Exception:  # noqa: BLE001 - the raise IS the assertion
    raised = True
check("record_walk raises when the server does not answer (the walk's caller logs it)", raised)

check.done()
