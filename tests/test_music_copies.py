"""One song played is one play (plex_sync + the schema 4 migration).

Reading the owner's music library whole (2026-11-15) stored 962 copies of
songs played once: Plex gives every copy of a track (album, remaster,
compilation) the same lastViewedAt second, while its history holds one
play ("Lovely Day" three times at 00:46:51). And a song stored from the
listing and from the history landed twice, the two stamps 147 s apart - the
90-second same-play rule is for video. On a copy of the owner's backup the
migration took 8,577 such rows out of 400k and left Spotify and video alone.

    python tests/test_music_copies.py
"""
from _harness import check  # noqa: I001 - first: temp stores

from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

import src.database.connection as conn_mod
import src.services.plex_sync as ps
from src.database.models import Base, User, WatchHistoryEntry as W

# ── the sync keeps one copy per song and second ─────────────────────────────
T = 1794703611
items = [
    {"ratingKey": "1007446", "lastViewedAt": T, "title": "Lovely Day", "grandparentTitle": "Bill Withers",
     "Genre": [{"tag": "Soul"}], "_category": "music"},
    {"ratingKey": "1007354", "lastViewedAt": T, "title": "Lovely Day", "grandparentTitle": "Bill Withers",
     "_category": "music"},
    {"ratingKey": "1007264", "lastViewedAt": T, "title": "lovely day ", "grandparentTitle": "BILL WITHERS",
     "_category": "music"},
    {"ratingKey": "2001", "lastViewedAt": T, "title": "Another Song", "grandparentTitle": "Bill Withers",
     "_category": "music"},
    {"ratingKey": "3001", "lastViewedAt": T + 400, "title": "Lovely Day", "grandparentTitle": "Bill Withers",
     "_category": "music"},
]
n = ps.drop_music_copies(items)
keys = [i["ratingKey"] for i in items]
check("three copies of one song at one second are one play, the one with a genre kept",
      n == 2 and "1007446" in keys and "1007354" not in keys and "1007264" not in keys, keys)
check("another song at that second, and the same song later, stay", "2001" in keys and "3001" in keys)
check("...so the three-in-a-second mark rule no longer counts copies",
      ps.mark_bulk_marked(items) == 0)

# ── one play from the listing and the history is one ────────────────────────
song = {"type": "track", "_category": "music", "duration": 251_133}
check("a song's same-play window is its length; video keeps 90 s; a history event without a length a song's",
      ps._same_play_s(song) == 251 and ps._same_play_s({"type": "movie", "duration": 7_200_000}) == 90
      and ps._same_play_s({"type": "track"}) == 360)

listing = [{"ratingKey": "1015214", "lastViewedAt": T, "title": "Cozy", "grandparentTitle": "Next",
            "type": "track", "_category": "music", "duration": 251_133},
           {"ratingKey": "1007446", "lastViewedAt": T, "title": "Lovely Day", "grandparentTitle": "Bill Withers",
            "type": "track", "_category": "music", "duration": 256_159}]
libs = {"14": ("music", "artist", "Music")}


def ev(rk, at, title, artist):
    d = {"ratingKey": rk, "viewedAt": at, "type": "track", "title": title, "grandparentTitle": artist,
         "librarySectionID": "14"}
    return tuple(d.get(k) for k in ps._HISTORY_EVENT_KEYS)


extra = ps._owner_history_entries(
    [ev("1015214", T + 147, "Cozy", "Next"),                         # the same play, its other end
     ev("1007264", T + 30, "Lovely Day", "Bill Withers"),            # the play, on another copy
     ev("1015214", T + 2000, "Cozy", "Next")], libs, listing)        # a second play, later
check("the history's stamp of a listed song, 147 s off, is the same play",
      not any(e["viewedAt"] == T + 147 for e in extra), extra)
check("a play the history holds on another copy of a listed song is the same play",
      not any(e["ratingKey"] == "1007264" for e in extra), extra)
check("a later play of the song is its own", [e["viewedAt"] for e in extra] == [T + 2000], extra)

# ── the migration repairs what the first read stored ────────────────────────
from _checks import memory_engine  # noqa: E402
eng = memory_engine()
conn_mod.engine = eng
Base.metadata.create_all(bind=eng)
with eng.begin() as c:
    c.exec_driver_sql("PRAGMA user_version = 3")
S = sessionmaker(bind=eng)
db = S()
db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True))
at = datetime(2026, 11, 15, 0, 46, 51)


def row(rk, title, artist, when, source=None, genres=None, dur=256_000, mt="music"):
    db.add(W(user_id=1, plex_user_id="1", plex_item_id=rk, title=title, series_title=artist, media_type=mt,
             viewed_at=when, source=source, genres=genres, duration_ms=dur, completed=True))


row("1007446", "Lovely Day", "Bill Withers", at, "marked", "Soul")
row("1007354", "Lovely Day", "Bill Withers", at, "marked", "Soul")
row("1007264", "Lovely Day", "Bill Withers", at, None, None)
row("1015214", "Cozy", "Next", at + timedelta(minutes=10), None, "R&B", 251_133)
row("1015214", "Cozy", "Next", at + timedelta(minutes=10, seconds=147), None, "R&B", 251_133)
row("1015214", "Cozy", "Next", at + timedelta(minutes=30), None, "R&B", 251_133)          # played again later
row("sp1", "Lovely Day", "Bill Withers", at, "spotify", "Soul")                            # Spotify: not a copy
row("e1", "Ep 1", "Show", at, "marked", mt="show")
row("e2", "Ep 2", "Show", at, "marked", mt="show")
db.commit()
db.close()
conn_mod._migrate_columns()
db = S()
lovely = db.query(W).filter(W.title == "Lovely Day", W.source.isnot("spotify") | W.source.is_(None)).all()
plex_lovely = [r for r in lovely if r.source != "spotify"]
check("the three copies are one row, with its genre, counted as a play",
      len(plex_lovely) == 1 and plex_lovely[0].genres == "Soul" and plex_lovely[0].source is None,
      [(r.plex_item_id, r.source, r.genres) for r in plex_lovely])
cozy = sorted(r.viewed_at for r in db.query(W).filter(W.title == "Cozy").all())
check("one play stored twice 147 s apart is one; the play twenty minutes later stays",
      cozy == [at + timedelta(minutes=10), at + timedelta(minutes=30)], cozy)
check("Spotify plays and video marks are left alone",
      db.query(W).filter(W.source == "spotify").count() == 1
      and db.query(W).filter(W.media_type == "show").count() == 2)
check("the migration runs once (schema generation 4)",
      db.execute(text("PRAGMA user_version")).scalar() == conn_mod.SCHEMA_VERSION >= 4)
db.close()

check.done()
