"""Artists MusicBrainz could not place (2026-11-02).

1,676 Spotify artists sat as "musicbrainz: no match" on the owner's install,
1,108 of them without a genre on any play. The name search asked
artist:"<name>" only - no alias, no escaping, the first hit won - and found 1
of the 40 most played; with aliases, escaping, the parts of "X fka Y" and a
name check it found 12. Now:

- the search builds that query and only takes a hit whose name, sort name or
  alias matches;
- the resolver asks most played first, one-play artists wait, and the old
  misses get one fresh look with the new search;
- plays Last.fm had nothing for (genres "") take MusicBrainz's genres, when
  the MBID is found and in a phase of their own for artists that have one.

    python tests/test_music_artist_match.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio
from datetime import datetime, timedelta

conn = app_db()
from src.database.models import MusicLookupMiss, User, WatchHistoryEntry  # noqa: E402
from src.services import app_state  # noqa: E402
from src.services import music_matcher as mm  # noqa: E402
from src.services import music_metadata as meta  # noqa: E402
from src.services import music_misses  # noqa: E402

# ── the query and the name check ────────────────────────────────────────────
check.table("name variants", [
    ("fka: both names", "Ferdinand fka Left Boy", ["Ferdinand fka Left Boy", "Ferdinand", "Left Boy"]),
    ("a.k.a.", "POL1Z1STENS0HN a.k.a. Jan Böhmermann",
     ["POL1Z1STENS0HN a.k.a. Jan Böhmermann", "POL1Z1STENS0HN", "Jan Böhmermann"]),
    ("a bar", "Creeds | Hardside", ["Creeds | Hardside", "Creeds", "Hardside"]),
    ("parentheses", "EAV (Erste Allgemeine Verunsicherung)",
     ["EAV (Erste Allgemeine Verunsicherung)", "EAV", "Erste Allgemeine Verunsicherung"]),
    ("/\\ as the lambda MusicBrainz writes", "Axwell /\\ Ingrosso",
     ["Axwell /\\ Ingrosso", "Axwell Λ Ingrosso"]),
    ("a plain name stays one", "Turbonegro", ["Turbonegro"]),
], meta.mb_name_variants)
q = meta.mb_artist_query("Axwell /\\ Ingrosso")
check("the query asks name and alias, with Lucene's specials escaped",
      'artist:"Axwell \\/\\\\ Ingrosso"' in q and 'alias:"Axwell Λ Ingrosso"' in q, q)

HITS = [{"id": "x", "name": "Turbonegro Tribute", "aliases": []},
        {"id": "t", "name": "Turboneger", "sort-name": "Turboneger",
         "aliases": [{"name": "Turbonegro"}]}]
check.table("a hit counts only when its name or an alias matches", [
    ("the alias", (HITS, "Turbonegro", ()), "t"),
    ("never just the first hit", ([HITS[0]], "Turbonegro", ()), None),
    ("an excluded id is skipped", (HITS, "Turbonegro", {"t"}), None),
    ("case and punctuation folded", ([{"id": "f", "name": "FiNCH",
                                       "aliases": [{"name": "Finch Asozial"}]}], "FiNCH ASOZiAL", ()), "f"),
    ("a fka part", ([{"id": "l", "name": "Left Boy"}], "Ferdinand fka Left Boy", ()), "l"),
], lambda a: (meta.mb_pick_artist(*a) or {}).get("id"))

# ── the resolver ────────────────────────────────────────────────────────────
with conn.get_db_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True))
    db.commit()
    t0 = datetime(2026, 10, 1)
    for artist, n, genres in (("Big", 5, ""), ("Middle", 3, None), ("Once", 1, None),
                              ("Gone", 2, None)):
        for i in range(n):
            db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id=f"spotify:{artist}{i}",
                                     title=f"{artist} {i}", series_title=artist, media_type="music",
                                     source="spotify", viewed_at=t0 + timedelta(hours=i),
                                     genres=genres))
    db.add(MusicLookupMiss(kind="artist_mbid", name="Gone", attempts=2,
                           last_attempt_at=t0, next_retry_at=datetime(2027, 1, 1),
                           last_reason="musicbrainz: no match"))
    db.commit()

asked = []


async def fake_mb(name, mbid=None, rejected=None):
    asked.append(name)
    if name == "Big":
        return {"mbid": "mb-big", "genres": ["hardtekk", "techno"], "tags": []}
    return None


saved = meta.fetch_musicbrainz_artist
meta.fetch_musicbrainz_artist = fake_mb
try:
    res = asyncio.run(mm.resolve_artist_mbids(1, batch=10))
    first = list(asked)
    asked.clear()
    asyncio.run(mm.resolve_artist_mbids(1, batch=10))
    second = list(asked)
finally:
    meta.fetch_musicbrainz_artist = saved

check("most played first, the one-play artist waits, the old miss gets its fresh look",
      first == ["Big", "Middle", "Gone"], first)
check("...once: the second run leaves the misses to their wait", second == [], second)
with conn.get_db_session() as db:
    big = db.query(WatchHistoryEntry.artist_mbid, WatchHistoryEntry.genres).filter(
        WatchHistoryEntry.series_title == "Big").distinct().all()
    gone = db.query(MusicLookupMiss.attempts).filter_by(name="Gone").scalar()
check("the MBID is written, and MusicBrainz's genres go to the plays Last.fm had none for",
      big == [("mb-big", "hardtekk,techno")], big)
check("the reopened miss keeps its attempt count and earns the next wait", gone == 3, gone)
check("the reopen is remembered per search version",
      app_state.get_state(mm._REOPEN_KEY) == str(meta._MB_QUERY_VERSION))

# ── phase 2.5: genres for plays with an MBID that Last.fm left empty ────────
with conn.get_db_session() as db:
    for artist, mbid, n in (("Known", "mb-known", 3), ("Bare", "mb-bare", 2)):
        for i in range(n):
            db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id=f"spotify:{artist}{i}",
                                     title=f"{artist} {i}", series_title=artist, media_type="music",
                                     source="spotify", viewed_at=t0, genres="", artist_mbid=mbid))
    db.commit()


async def fake_by_id(name, mbid=None, rejected=None):
    asked.append(mbid)
    return {"mbid": mbid, "genres": ["synthwave"]} if mbid == "mb-known" else {"mbid": mbid, "genres": [], "tags": []}

meta.fetch_musicbrainz_artist = fake_by_id
asked.clear()
try:
    r = asyncio.run(mm.fill_genres_from_musicbrainz(1, batch=10))
    again = list(asked)
    asked.clear()
    asyncio.run(mm.fill_genres_from_musicbrainz(1, batch=10))
finally:
    meta.fetch_musicbrainz_artist = saved
check("Last.fm's empty plays with an MBID take MusicBrainz's genres",
      r["filled_plays"] == 3 and r["no_genres"] == 1, r)
check("...most played first", again == ["mb-known", "mb-bare"], again)
check("an artist without genres there waits like a miss, the filled one is done",
      asked == [], asked)

check.done()
