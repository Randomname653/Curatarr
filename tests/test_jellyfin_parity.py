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
from src.database.models import Base, LibraryConfig, MusicTag, PlexRating, User  # noqa: E402
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
STALE = "ffffffffffffffffffffffffffffffff"

with conn_mod.get_db_session() as db:
    db.add(User(id=1, plex_user_id=JANE, plex_username="jane", is_admin=True))
    db.commit()
    db.add(LibraryConfig(plex_section_key=LIB_MUS, plex_section_title="Music",
                         plex_section_type="artist", media_category="music"))
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
    check("the history sync reads the ratings too",
          "sync_ratings(adapter, uid, key)" in inspect.getsource(jellyfin_history.sync_history))

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

    print("\n-- hand-over --")
    check("lyrics, music tags and subtitles hand over on Jellyfin",
          "run_music_index" in inspect.getsource(lyrics.run_lyrics_sync)
          and "jellyfin_tags" in inspect.getsource(music_tags.run_music_tags)
          and "_acquire_jellyfin" in inspect.getsource(subtitle_signals.acquire_subtitle_text))
    check("the ratings' source column is migrated",
          '("plex_ratings",           "source", "VARCHAR(16)")' in inspect.getsource(conn_mod._migrate_columns))
finally:
    settings.MEDIA_SERVER_TYPE = None
    ms.get_adapter = real_get

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
