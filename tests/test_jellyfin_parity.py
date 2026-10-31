"""Jellyfin parity (docs/JELLYFIN_DESIGN.md, phase 6).

- the music index (services/lyrics.py) filled from Jellyfin: tracks with
  sizes and lyrics (GET /Audio/{id}/Lyrics), artists with MusicBrainz ids;
- music deletion candidates without Lidarr come from that index with
  service "jellyfin" and a Jellyfin deep link; an artist is deleted album by
  album, with a re-read (an album that stays is a failure);
- moods and styles from Genres and Tags (music_tags);
- likes, favourites and dislikes as ratings (design D6): Jellyfin-sourced
  rows are replaced, a Plex star on the same item wins and stays;
- subtitles: Jellyfin's own track (a plain one over SDH, never forced, never
  a bitmap one), a series measured on its first episode;
- the Plex modules hand over on a Jellyfin install, and only there.

    python tests/test_jellyfin_parity.py
"""
import asyncio
import inspect
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import Base, LibraryConfig, MusicTag, PlexRating, User, WatchHistoryEntry as WH  # noqa: E402
Base.metadata.create_all(conn_mod.engine)

from media_server_fixtures import JellyfinWriteServer  # noqa: E402
from src.config import settings  # noqa: E402
from src.services import lyrics, music_tags, subtitle_signals  # noqa: E402
import src.services.media_server as ms  # noqa: E402
from src.services.media_server import jellyfin_history, jellyfin_music  # noqa: E402
from src.services.media_server.base import ServerUser  # noqa: E402
from src.services.media_server.jellyfin_adapter import JellyfinAdapter  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


JANE = "b0c1d2e3f405162738495a6b7c8d9e0f"
HEAT, DARK = "4b1d2f3e5a6c7b8d9e0f1a2b3c4d5e6f", "9a8b7c6d5e4f30211203f4e5d6c7b8a9"
BJORK, HOMOGENIC = "6d7e8f90a1b2c3d4e5f60718293a4b5c", "7e8f90a1b2c3d4e5f60718293a4b5c6d"
HUNTER, JOGA = "8f90a1b2c3d4e5f60718293a4b5c6d7e", "90a1b2c3d4e5f60718293a4b5c6d7e8f"
LIB_MUS = "7e64e319657a9516ec78490da03edccb"
LIB_MOV, LIB_TV = "f137a2dd21bbc1b99aa5c0f6bf02a805", "a656b907eb3a73532e40e44b968d0225"
STALE = "ffffffffffffffffffffffffffffffff"

with conn_mod.get_db_session() as db:
    db.add(User(id=1, plex_user_id=JANE, plex_username="jane", is_admin=True))
    db.commit()
    db.add_all([LibraryConfig(plex_section_key=LIB_MUS, plex_section_title="Music",
                              plex_section_type="artist", media_category="music"),
                LibraryConfig(plex_section_key=LIB_MOV, plex_section_title="Movies",
                              plex_section_type="movie", media_category="movie"),
                LibraryConfig(plex_section_key=LIB_TV, plex_section_title="Shows",
                              plex_section_type="show", media_category="show")])
    db.add_all([PlexRating(user_id=1, plex_item_id=HOMOGENIC, media_type="album", rating=6.0),   # a Plex star
                PlexRating(user_id=1, plex_item_id=STALE, media_type="track", rating=10.0, source="jellyfin_like")])
    db.commit()

lyrics.LYRICS_DB_PATH = lyrics.PLEX_MUSIC_DB_PATH = _TMP / "plex_music.db"
SRV = JellyfinWriteServer()
A = JellyfinAdapter("http://jf:8096", "jf-key", transport=SRV.transport())
real_get = ms.get_adapter
settings.MEDIA_SERVER_TYPE = "jellyfin"
ms.get_adapter = lambda *a, **kw: A

try:
    print("\n-- the music index --")
    done = asyncio.run(jellyfin_music.run_music_index(adapter=A))
    arts = {a["artist_key"]: a for a in lyrics.plex_artists()}
    bj = arts.get(BJORK) or {}
    check("the walk finishes", done is True)
    check("Björk is in the index with her tracks, footprint and MusicBrainz id",
          bj.get("name") == "Björk" and (bj.get("tracks") or 0) >= 2 and (bj.get("size_bytes") or 0) > 0
          and bool(bj.get("mbid")))
    tracks = {t["plex_rating_key"]: t for t in lyrics.plex_album_tracks(HOMOGENIC)}
    check("the album's tracks are filed under it", {HUNTER, JOGA} <= set(tracks))
    check("Hunter's lyrics are fetched from /Audio/{id}/Lyrics",
          (tracks.get(HUNTER) or {}).get("lines") == 2)
    check("a track without a lyric stream asks for nothing",
          (tracks.get(JOGA) or {}).get("lines") == 0
          and not any(c[1] == f"/Audio/{JOGA}/Lyrics" for c in SRV.calls))
    check("the shaped track carries the album and the artist",
          jellyfin_music.plex_shaped_track({"Id": HUNTER, "Type": "Audio", "AlbumId": HOMOGENIC,
                                            "AlbumArtists": [{"Id": BJORK, "Name": "Björk"}]})
          ["grandparentRatingKey"] == BJORK)
    print("\n-- a Plex still named in .env is not this install's server --")
    settings.PLEX_BASE_URL, settings.PLEX_AUTH_TOKEN = "http://plex.invalid:32400", "plex-token"
    with conn_mod.get_db_session() as db:
        db.add(WH(user_id=1, plex_user_id=JANE, plex_item_id="spotify:track:abc", title="Hunter",
                  series_title="Björk", media_type="music", source="spotify", viewed_at=datetime(2026, 10, 1)))
        db.commit()
    from src.services import music_matcher, orphan_repair, plex_players
    from src.services import plex_sync
    res = asyncio.run(music_matcher.match_spotify_to_plex(1))
    with conn_mod.get_db_session() as db:
        sp = db.query(WH).filter(WH.source == "spotify").one().plex_item_id
    check("Spotify plays are matched onto the music index's Jellyfin tracks", res.get("matched") == 1 and sp == HUNTER)
    check("the orphaned-section check and its repair leave Plex alone",
          asyncio.run(orphan_repair.detect_orphaned_sections()) == []
          and "error" in asyncio.run(orphan_repair.apply_orphan_mapping([{"section_id": "1", "category": "movie"}])))
    check("the player backfill does not read Plex's history", plex_players._plex() == (None, None))
    ra = asyncio.run(plex_sync.reattribute_watch_history())
    check("re-attribution has nothing to do on Jellyfin", ra.get("skipped") is True and ra["updated"] == 0)

    GONE = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    with conn_mod.get_db_session() as db:
        db.add_all([WH(user_id=1, plex_user_id=JANE, plex_item_id=HEAT, title="Heat", media_type="movie",
                       viewed_at=datetime(2026, 10, 2)),
                    WH(user_id=1, plex_user_id=JANE, plex_item_id=GONE, title="Gone", media_type="movie",
                       viewed_at=datetime(2026, 10, 3))])
        db.commit()
    first = asyncio.run(plex_sync.cleanup_orphan_watch_history())
    check("orphan cleanup reads Jellyfin and keeps the share guard",
          first.get("archived") == 0 and first.get("would_archive") == 1 and "Jellyfin really lost" in first.get("error", ""))
    forced = asyncio.run(plex_sync.cleanup_orphan_watch_history(force=True))
    with conn_mod.get_db_session() as db:
        hidden = {r.plex_item_id for r in db.query(WH).execution_options(include_orphaned=True)
                  .filter(WH.orphaned_at.isnot(None))}
    check("only the item Jellyfin no longer has is hidden", forced.get("archived") == 1 and hidden == {GONE})
    SRV_DOWN = JellyfinAdapter("http://jf:8096", "wrong-key", transport=SRV.transport())
    check("an unreadable Jellyfin hides nothing",
          "error" in asyncio.run(jellyfin_history.live_item_keys(adapter=SRV_DOWN)))
    settings.PLEX_BASE_URL, settings.PLEX_AUTH_TOKEN = "", ""

    check("only Audio items are tracks", jellyfin_music.plex_shaped_track({"Id": HOMOGENIC, "Type": "MusicAlbum"}) is None)

    print("\n-- music deletion candidates without Lidarr --")
    from src.routers import recommendations as rec
    cands = asyncio.run(rec._plex_music_candidates())
    c = next((x for x in cands if x["plex_rating_key"] == BJORK), {})
    check("Björk is a candidate on service jellyfin", c.get("service") == "jellyfin" and c.get("category") == "music")
    check("the card links into Jellyfin", (c.get("arr_url") or "").startswith(f"http://jf:8096/web/#/details?id={BJORK}"))
    check("with the footprint and the MusicBrainz id", c.get("size_mb", 0) > 0 and c.get("musicbrainz_id") == bj.get("mbid"))

    stubborn = JellyfinAdapter("http://jf:8096", "jf-key", transport=JellyfinWriteServer(deny_delete=True).transport())
    check("an artist whose albums stay is not reported deleted", asyncio.run(stubborn.delete_artist(BJORK)) is False)
    check("a malformed id is refused", asyncio.run(A.delete_artist("1234")) is False)
    check("an artist is deleted album by album, with a re-read", asyncio.run(A.delete_artist(BJORK)) is True
          and HOMOGENIC in SRV.deleted and ("DELETE", f"/Items/{BJORK}", {}) not in SRV.calls)
    lyrics.mark_artist_gone(BJORK)
    check("then the index forgets the artist", lyrics.plex_artist(BJORK) is None)
    check("the delete path takes the artist branch for music",
          "delete_artist(media_id)" in inspect.getsource(rec))

    print("\n-- moods and styles --")
    SRV.deleted.clear()
    ok = asyncio.run(jellyfin_music.run_music_tags(adapter=A))
    with conn_mod.get_db_session() as db:
        tags = {(t.level, t.item_key, t.kind, t.tag) for t in db.query(MusicTag).all()}
        secs = {t.section_key for t in db.query(MusicTag).all()}
    check("the walk reads the whole library", ok is True and secs == {LIB_MUS})
    check("an artist's genre is a style", ("artist", BJORK, "style", "Electronic") in tags)
    check("an album's tags are moods, its genres styles",
          {("album", HOMOGENIC, "mood", "Melancholic"), ("album", HOMOGENIC, "style", "Art Pop")} <= tags)
    check("a track's tag is a mood, its genres are not styles",
          ("track", HUNTER, "mood", "Intense") in tags and not any(t[1] == HUNTER and t[2] == "style" for t in tags))
    check("blank tags are dropped", jellyfin_music.tag_rows("track", [{"Id": HUNTER, "Tags": [" ", ""]}]) == [])

    print("\n-- likes as ratings --")
    jane = ServerUser(key=JANE, name="jane")
    asyncio.run(A.set_like(jane, JOGA, likes=False))
    asyncio.run(A.set_like(jane, HOMOGENIC, likes=True))
    rated = {r["key"]: r for r in asyncio.run(A.rated_music(jane))}
    check("a favourite is a 10, a dislike a 2, a liked film is not music",
          rated.get(HUNTER, {}).get("rating") == 10.0 and rated.get(JOGA, {}).get("rating") == 2.0
          and HEAT not in rated)
    check("the artist name rides along", rated.get(HUNTER, {}).get("artist") == "Björk")
    res = asyncio.run(jellyfin_history.sync_ratings(A, 1, JANE))
    with conn_mod.get_db_session() as db:
        rows = {r.plex_item_id: (r.rating, r.source, r.media_type) for r in db.query(PlexRating).all()}
    check("the likes are stored as ratings", rows.get(HUNTER) == (10.0, "jellyfin_like", "track")
          and rows.get(JOGA) == (2.0, "jellyfin_like", "track"))
    check("a Plex star on the same item wins and stays", rows.get(HOMOGENIC) == (6.0, None, "album"))
    check("a like Jellyfin no longer has is removed", STALE not in rows and res["removed"] == 1)
    SRV.dislikes.clear()
    SRV.likes.clear()
    asyncio.run(A.set_like(jane, JOGA, likes=True))
    res = asyncio.run(jellyfin_history.sync_ratings(A, 1, JANE))
    with conn_mod.get_db_session() as db:
        joga = db.query(PlexRating).filter(PlexRating.plex_item_id == JOGA).one().rating
    check("a dislike turned like is changed in place", joga == 10.0 and res["changed"] == 1 and res["added"] == 0)
    class RatingsDown:
        async def rated_music(self, user):
            raise RuntimeError("down")

    async def _rep(adapter, uid, key):
        return {"inserted": 1, "merged": 0}
    real_rep, real_poll = jellyfin_history.import_report, jellyfin_history.poll_user
    jellyfin_history.import_report = jellyfin_history.poll_user = _rep
    try:
        res = asyncio.run(jellyfin_history.sync_history(adapter=RatingsDown(), followup=False))
    finally:
        jellyfin_history.import_report, jellyfin_history.poll_user = real_rep, real_poll
    u = res["users"].get(1) or {}
    check("the history sync reads the ratings; their failure costs no plays",
          res["synced"] == 2 and "report" in u and u.get("ratings") == {"error": "RuntimeError"})

    print("\n-- subtitles --")
    info = asyncio.run(A.subtitle_streams(HEAT))
    st = {s["language"] + str(s["key"].rsplit("/", 2)[-2]): s for s in (info or {}).get("streams", [])}
    check("Heat's tracks in the Plex shape", set(st) == {"ger2", "eng3", "eng4", "fre5"}
          and st["eng4"]["forced"] and st["eng3"]["extendedDisplayTitle"] == "SDH"
          and st["ger2"]["external"] and not st["eng3"]["external"])
    check("with the IMDb id and the runtime", info["imdb_id"].startswith("tt") and info["duration_min"] > 60)
    pick = subtitle_signals.pick_subtitle_stream(info["streams"])
    check("the plain German track is picked over SDH, forced and bitmap", pick is not None and pick["language"] == "ger")
    got = asyncio.run(subtitle_signals.acquire_subtitle_text(HEAT))
    check("acquire reads it from Jellyfin as a sidecar", isinstance(got, tuple) and "[ger track 2]" in got[0]
          and got[1]["source"] == "jellyfin_sidecar" and got[1]["language"] == "ger")
    ser = asyncio.run(A.subtitle_streams(DARK))
    check("a series is measured on its first episode", ser is not None and ser["episode_ref"] == (1, 1))
    check("an item Jellyfin does not know is a clean miss",
          asyncio.run(subtitle_signals.acquire_subtitle_text(STALE)) == {})
    n = len(SRV.calls)
    try:
        asyncio.run(A.subtitle_text("/Items/x"))
        refused = False
    except Exception:  # noqa: BLE001 - JellyfinError expected
        refused = True
    check("a non-subtitle key is refused before a request", refused and len(SRV.calls) == n)

    print("\n-- redundancy, breakdown, labels, adapter choice --")
    v = asyncio.run(A.versions([HEAT, STALE])).get(HEAT) or []
    check("each version's file, resolution and size for the redundancy panel",
          len(v) == 1 and v[0]["resolution"] == "1080" and v[0]["size_gb"] > 6 and "/" not in v[0]["file"]
          and v[0]["file"])
    check("the redundancy report reads them on Jellyfin", "jf.versions(" in inspect.getsource(rec.get_redundancy))
    from src.routers import library as library_router
    check("the library breakdown counts Jellyfin's libraries",
          "adapter.count_items(lib)" in inspect.getsource(library_router.library_breakdown))
    from src.services import data_custodian
    labels = {t.job_id: t.label for t in data_custodian._registry()}
    check("Activity names the server", labels["plex_sync"] == "Jellyfin sync"
          and labels["lyrics_sync"] == "Lyrics from Jellyfin")
    from types import SimpleNamespace
    conf = SimpleNamespace(MEDIA_SERVER_TYPE=None, JELLYFIN_URL="http://jf:8096", JELLYFIN_API_KEY="k",
                           effective_plex_url="http://plex:32400", effective_plex_token="t")
    check("a proposal's server picks the adapter, not the active one",
          type(real_get(conf, kind="jellyfin")).__name__ == "JellyfinAdapter"
          and type(real_get(conf)).__name__ == "PlexAdapter"
          and "get_adapter(kind=\"jellyfin\")" in inspect.getsource(rec))

    print("\n-- hand-over --")
    check("lyrics, music tags and subtitles hand over on Jellyfin",
          "run_music_index" in inspect.getsource(lyrics.run_lyrics_sync)
          and "jellyfin_tags" in inspect.getsource(music_tags.run_music_tags)
          and "_acquire_jellyfin" in inspect.getsource(subtitle_signals.acquire_subtitle_text))
    check("the delete dialog knows Jellyfin deletes (no recycle bin, SoulSync ban)",
          "jellyfin" in rec._DELETE_SERVICES
          and asyncio.run(rec._arr_recycle_bin("jellyfin")) == {"recycle_bin": False, "cleanup_days": None}
          and '("plex", "jellyfin", "lidarr")' in inspect.getsource(rec.deletion_delete_target))
    check("the redundancy report links into Jellyfin", 'e["server_url"] = link' in inspect.getsource(rec.get_redundancy))
    from src.routers import library as library_router
    check("the Music page is told which server its index is from",
          'out[svc]["media_server"] = active_kind()' in inspect.getsource(library_router))
    js = {n: (ROOT / "frontend" / "js" / n).read_text(encoding="utf-8") for n in ("deletions.js", "arr.js", "state.js")}
    check("the deletion UI treats Jellyfin as a media server, not an arr",
          "new Set(['plex', 'jellyfin'])" in js["deletions.js"] and "jellyfin: 'Jellyfin'" in js["deletions.js"])
    check("the Music page names the server", "via ${serverLabel()}" in js["arr.js"]
          and "export function serverLabel()" in js["state.js"])
    check("the ratings' source column is migrated",
          '("plex_ratings",           "source", "VARCHAR(16)")' in inspect.getsource(conn_mod._migrate_columns))
finally:
    settings.MEDIA_SERVER_TYPE = None
    ms.get_adapter = real_get

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
