"""Curatarr's writes into Jellyfin (docs/JELLYFIN_DESIGN.md §5, phase 4).

- "Curatarr Recommended" playlists per member, owned by their Jellyfin user,
  written with the one API key: a series goes on as its next unwatched
  episode, a stale or never-resolved key is found again by title and healed
  onto the recommendation row; a refresh edits the playlist IN PLACE (same
  id), and a second push without new recommendations does nothing;
- the music playlist: one unheard album per recommended artist;
- the Wrap-Up playlist(s), with what was not found named;
- collections: created or edited in place, our older shelves removed, the
  titles in them untouched, the snapshot written;
- the watchlist: a title from the library onto "Curatarr Watchlist", once;
  a title not in the library refused with a reason, never raised;
- mark played; delete with a re-read (an item that stays is a failure);
- the tech walk fills MediaTechProfile, the ledger and the id memory from
  MediaSources, so recommendations have Jellyfin keys to write;
- the Plex modules hand over on a Jellyfin install (and only there), the
  delete / probe / limbo paths know service "jellyfin".

    python tests/test_jellyfin_writes.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import (Base, CachedRecommendation as CR, LibraryConfig, LibraryItem,  # noqa: E402
                                 MediaTechProfile, ServerIdMap, User)
Base.metadata.create_all(conn_mod.engine)

from media_server_fixtures import JellyfinWriteServer  # noqa: E402
from src.config import settings  # noqa: E402
from src.services.app_state import get_state  # noqa: E402
from src.services.media_server import jellyfin_tech, jellyfin_writes as jw  # noqa: E402
from src.services.media_server.base import ServerUser  # noqa: E402
from src.services.media_server.jellyfin_adapter import JellyfinAdapter  # noqa: E402

from _checks import check  # noqa: E402


JANE, SAM = "b0c1d2e3f405162738495a6b7c8d9e0f", "c1d2e3f405162738495a6b7c8d9e0fa1"
HEAT, HOME = "4b1d2f3e5a6c7b8d9e0f1a2b3c4d5e6f", "5c2e3f4a6b7d8c9e0f1a2b3c4d5e6f70"
DARK, SECRETS, DOUBLE = "9a8b7c6d5e4f30211203f4e5d6c7b8a9", "2a3b4c5d6e7f80910a1b2c3d4e5f6071", "3b4c5d6e7f8091a20b1c2d3e4f506172"
HOMOGENIC, HUNTER = "7e8f90a1b2c3d4e5f60718293a4b5c6d", "8f90a1b2c3d4e5f60718293a4b5c6d7e"
LIB_MOV, LIB_TV, LIB_MUS = "f137a2dd21bbc1b99aa5c0f6bf02a805", "a656b907eb3a73532e40e44b968d0225", "7e64e319657a9516ec78490da03edccb"
now = datetime.utcnow()

with conn_mod.get_db_session() as db:
    db.add_all([User(id=1, plex_user_id=JANE, plex_username="jane", is_admin=True),
                User(id=2, plex_user_id="12345678", plex_username="plexonly")])
    db.commit()
    db.add_all([LibraryConfig(plex_section_key=LIB_MOV, plex_section_title="Movies", plex_section_type="movie", media_category="movie"),
                LibraryConfig(plex_section_key=LIB_TV, plex_section_title="Shows", plex_section_type="show", media_category="show"),
                LibraryConfig(plex_section_key=LIB_MUS, plex_section_title="Music", plex_section_type="artist", media_category="music")])
    db.add_all([CR(user_id=1, category="movie", lane="library", title="Heat", confidence=0.9, cached_at=now),
                CR(user_id=1, category="movie", lane="library", title="Home Movie", confidence=0.8,
                   plex_rating_key="1002", cached_at=now),          # a Plex key from before a switch
                CR(user_id=1, category="show", lane="library", title="Dark", confidence=0.9,
                   plex_rating_key=DARK, cached_at=now),
                CR(user_id=1, category="music", lane="library", title="Björk", confidence=0.9, cached_at=now)])
    db.commit()

SRV = JellyfinWriteServer()
A = JellyfinAdapter("http://jf:8096", "jf-key", transport=SRV.transport())
jane = SimpleNamespace(id=1, plex_username="jane", plex_token=None)
run = asyncio.run


def pl(name):
    return [(k, v) for k, v in SRV.playlists.items() if v["name"] == name]


print("\n-- recommended playlists --")
res = run(jw.push_user_playlists(jane, adapter=A))
movies = pl("Curatarr Recommended · Movies")
shows = pl("Curatarr Recommended · Shows")
check("movies and shows pushed, owned by jane's Jellyfin user, no token needed",
      res["pushed"] == ["movie", "show"] and movies and movies[0][1]["user"] == JANE
      and movies[0][1]["media"] == "Video")
check("an unresolved and a stale (Plex) key were found again by title",
      movies[0][1]["items"] == [HEAT, HOME])
with conn_mod.get_db_session() as db:
    healed = {r.title: r.plex_rating_key for r in db.query(CR).filter(CR.category == "movie")}
check("...and healed onto the recommendation rows", healed == {"Heat": HEAT, "Home Movie": HOME})
check("a series goes on as jane's next unwatched episode (she saw S01E01)",
      shows[0][1]["items"] == [DOUBLE])
check("a second push with nothing new writes nothing", run(jw.push_user_playlists(jane, adapter=A)) == {"pushed": []})
old_id = movies[0][0]
with conn_mod.get_db_session() as db:
    db.query(CR).filter(CR.title == "Home Movie").delete()
    db.query(CR).filter(CR.title == "Heat").update({"cached_at": datetime.utcnow() + timedelta(seconds=5)})
    db.commit()
run(jw.push_user_playlists(jane, adapter=A))
movies = pl("Curatarr Recommended · Movies")
check("new recommendations edit the playlist in place: same id, the dropped title gone",
      len(movies) == 1 and movies[0][0] == old_id and movies[0][1]["items"] == [HEAT])
check("a Plex-only account is skipped with a reason",
      run(jw.push_user_playlists(SimpleNamespace(id=2, plex_username="plexonly"), adapter=A))
      == {"pushed": [], "skipped": "no_jellyfin_account"})

print("\n-- music --")
mu = run(jw.push_user_music_playlist(jane, adapter=A))
music = pl("Curatarr Recommended · Music")
check("one album per recommended artist, as an audio playlist",
      mu == {"pushed": ["music"]} and music[0][1]["items"] == [HOMOGENIC] and music[0][1]["media"] == "Audio")

print("\n-- Wrap-Up --")
wu = run(jw.push_wrapup_playlist(jane, "2026", [{"title": "Heat", "media_type": "movie"},
                                                {"title": "Dark", "media_type": "show"},
                                                {"title": "Not Here", "media_type": "movie"}],
                                 [{"track": "Hunter", "artist": "Björk"}, {"track": "Hunter", "artist": "Somebody"}],
                                 adapter=A))
check("video and music playlists, the misses named (a wrong artist is a miss)",
      [p["title"] for p in wu["pushed"]] == ["Curatarr Wrap-Up · 2026", "Curatarr Wrap-Up · 2026 · Music"]
      and pl("Curatarr Wrap-Up · 2026")[0][1]["items"] == [HEAT, DOUBLE]
      and pl("Curatarr Wrap-Up · 2026 · Music")[0][1]["items"] == [HUNTER]
      and wu["missing"] == ["Not Here", "Hunter"])

print("\n-- collections --")
SRV.collections["f" * 32] = {"name": "Curatarr · Old Shelf", "items": [HEAT]}
SRV.collections["e" * 32] = {"name": "Kometa Classics", "items": [HEAT]}
designs = [{"section_key": LIB_MOV, "plex_type": 1, "title": "Curatarr · Night Moves", "keys": [HEAT, HOME]}]
cr = run(jw.push_collections(designs, adapter=A))
names = {v["name"]: v["items"] for v in SRV.collections.values()}
check("our shelf is created, our old one removed, someone else's left alone",
      cr == {"created": 1, "deleted": 1, "failed": 0} and names.get("Curatarr · Night Moves") == [HEAT, HOME]
      and "Curatarr · Old Shelf" not in names and "Kometa Classics" in names)
cid = next(k for k, v in SRV.collections.items() if v["name"] == "Curatarr · Night Moves")
run(jw.push_collections([{**designs[0], "keys": [HOME]}], adapter=A))
check("the next rotation edits it in place", SRV.collections[cid]["items"] == [HOME])
check("the snapshot names the shelf", json.loads(run(asyncio.to_thread(get_state, "curatarr_collections")))
      ["designs"][0]["title"] == "Curatarr · Night Moves")

print("\n-- watchlist --")
w1 = run(jw.add_to_watchlist(1, "Heat", 1995, "movie", adapter=A))
w2 = run(jw.add_to_watchlist(1, "Dark", adapter=A))
run(jw.add_to_watchlist(1, "Heat", adapter=A))
check("titles go onto 'Curatarr Watchlist', a series as its next episode, each once",
      w1["ok"] and w2["ok"] and pl("Curatarr Watchlist")[0][1]["items"] == [HEAT, DOUBLE])
miss = run(jw.add_to_watchlist(1, "Not In The Library", adapter=A))
check("a title not in the library is refused with a reason, not raised",
      miss["ok"] is False and "not in the Jellyfin library" in miss["error"])
check("an account that is not Jellyfin's is refused",
      run(jw.add_to_watchlist(2, "Heat", adapter=A))["ok"] is False)

print("\n-- mark played, delete --")
run(A.mark_played(ServerUser(key=JANE, name="jane"), SECRETS))
check("mark played goes to /UserPlayedItems for the user",
      SECRETS in SRV.played and ("POST", f"/UserPlayedItems/{SECRETS}", {"userId": JANE}) in SRV.calls)
check("delete: gone on the re-read is a success", run(A.delete_item(HOME)) is True and HOME in SRV.deleted)
stubborn = JellyfinAdapter("http://jf:8096", "jf-key", transport=JellyfinWriteServer(deny_delete=True).transport())
check("an item that stays after the delete is a failure", run(stubborn.delete_item(HEAT)) is False)
check("only the stored GUID spelling reaches the delete URL",
      run(A.delete_item("../Users")) is False and run(A.delete_item(HEAT.upper())) is False)

print("\n-- the tech walk --")
SRV2 = JellyfinWriteServer()
A2 = JellyfinAdapter("http://jf:8096", "jf-key", transport=SRV2.transport())
tw = run(jellyfin_tech.sync_tech_profiles(force=True, adapter=A2))
with conn_mod.get_db_session() as db:
    prof = {p.plex_rating_key: SimpleNamespace(**{c.name: getattr(p, c.name) for c in MediaTechProfile.__table__.columns})
            for p in db.query(MediaTechProfile).all()}
    ledger = {i.plex_rating_key for i in db.query(LibraryItem).all()}
    idmap = {(m.entity_type, m.canonical_key): m.jellyfin_id for m in db.query(ServerIdMap).all()}
heat_p, dark_p = prof.get(HEAT), prof.get(DARK)
check("a film's profile: size, runtime, 1080p HEVC, languages, its TMDB id",
      heat_p is not None and heat_p.media_type == "movie" and round(heat_p.size_mb) == 7000
      and heat_p.resolution == "1080" and heat_p.codec == "hevc" and heat_p.audio_langs == "eng"
      and heat_p.sub_langs == "eng,fre,ger" and heat_p.tmdb_id == 949 and heat_p.hdr is False)
check("a series' profile sums its episodes and carries the series ids",
      dark_p is not None and dark_p.item_count == 2 and round(dark_p.size_mb) == round(4.5e9 / 1048576)
      and dark_p.tvdb_id == 334824 and dark_p.title == "Dark")
check("music stays out unless asked for (as on Plex)", HUNTER not in prof)
check("the walk writes the library ledger and the id memory",
      {HEAT, DARK} <= ledger and idmap.get(("movie", "tmdb:949")) == HEAT and tw["ids"]["added"] > 0)
check("twice a day at most", run(jellyfin_tech.sync_tech_profiles(adapter=A2)).get("skipped") is True)

print("\n-- hand-over from the Plex modules --")
import src.services.plex_playlists as pp  # noqa: E402
import src.services.plex_collections as pc  # noqa: E402
import src.services.plex_watchlist as pw  # noqa: E402
calls = []


async def _fake(*a, **kw):
    calls.append(a)
    return {"handed": True}


real = {n: getattr(jw, n) for n in ("push_user_playlists", "push_user_music_playlist",
                                     "push_wrapup_playlist", "push_collections", "add_to_watchlist")}
for n in real:
    setattr(jw, n, _fake)
settings.MEDIA_SERVER_TYPE = "jellyfin"
try:
    got = [run(pp.push_user_playlists(jane)), run(pp.push_user_music_playlist(jane)),
           run(pp.push_wrapup_playlist(jane, "x", [], [])), run(pc.push_collections([])),
           run(pw.add_to_watchlist(1, "Heat"))]
finally:
    settings.MEDIA_SERVER_TYPE = None
check("on Jellyfin all five Plex write entry points hand over", got == [{"handed": True}] * 5)
calls.clear()
plex_answer = run(pp.push_user_playlists(jane))
check("on Plex they do not (a token-less Plex user is skipped as before)",
      not calls and plex_answer == {"pushed": [], "skipped": "no_token"})
for n, f in real.items():
    setattr(jw, n, f)
rec = (ROOT / "src/routers/recommendations.py").read_text(encoding="utf-8")
check("the delete path knows service 'jellyfin': configured check, probe, delete through the adapter",
      'if service == "jellyfin":\n        return bool(settings.JELLYFIN_URL' in rec
      and '.delete_item(media_id)' in rec and 'if service in ("plex", "jellyfin"):' in rec)
import src.services.limbo_reconcile as lr  # noqa: E402
import src.services.media_server as ms  # noqa: E402
real_get = ms.get_adapter
ms.get_adapter = lambda *a, **kw: A
try:
    gone = run(lr.item_gone(None, "jellyfin", HOME))
    there = run(lr.item_gone(None, "jellyfin", HEAT))
    junk = run(lr.item_gone(None, "jellyfin", "1002"))
finally:
    ms.get_adapter = real_get
check("limbo asks the adapter: deleted = gone, present = there, a Plex id = no answer",
      gone is True and there is False and junk is None)

check.done()
