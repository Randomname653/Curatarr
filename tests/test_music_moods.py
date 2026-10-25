"""Music moods and the player of the night.

Nothing said how the music felt (track genres were a comma string) and no
play said where it ran, so "mornings ran energetic, nights melancholy" and
"after midnight it was the phone" could not be told. Plex's own moods and
styles are now walked per section (``music_tags``), a play takes its
track's moods, else its album's, else its artist's, and the Wrap-Up reads
the moods by part of the day and the player by hours.

    python tests/test_music_moods.py
"""
import asyncio
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.services.music_tags as mt  # noqa: E402
import src.services.wrapup_facts as wf  # noqa: E402
import src.services.wrapup_insights as wi  # noqa: E402
from src.database.models import Base, MusicTag, User, WatchHistoryEntry as W  # noqa: E402
from src.services.wrapup_story import render_fact_sheet  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def fake_db_session():
    s = Session()
    try:
        yield s
        s.commit()
    finally:
        s.close()


mt.get_db_session = fake_db_session

# ── 1. the walk ──────────────────────────────────────────────────────────────
print("\n-- the walk reads Plex's filter lists and the items' own tags --")


class Resp:
    def __init__(self, status, payload=None):
        self.status_code, self._p = status, payload or {}

    def json(self):
        return self._p


DIRS = {("mood", 8): [{"key": "11", "title": "Brooding"}],
        ("mood", 9): [{"key": "/library/sections/3/all?mood=12", "title": "Energetic"}],
        ("mood", 10): [{"key": "13", "title": "Melancholy"}],
        ("style", 8): [], ("style", 9): [{"key": "21", "title": "Shoegaze"}]}
UNDER = {("mood", "11"): ["a1"], ("mood", "12"): ["al1"], ("mood", "13"): ["t2"],
         ("style", "21"): ["al1"]}
fail = {"path": None}


class Client:
    async def get(self, url, params=None, headers=None):
        path = url.split("http://plex", 1)[1]
        if fail["path"] and fail["path"] in path:
            return Resp(500)
        t = int(params.get("type"))
        if path.endswith("/all"):
            for kind in ("mood", "style"):
                if kind in params:
                    return Resp(200, {"MediaContainer": {"Metadata": [
                        {"ratingKey": k} for k in UNDER.get((kind, params[kind]), [])]}})
            if t == 9:        # a server that puts the tags on the item too
                return Resp(200, {"MediaContainer": {"Metadata": [
                    {"ratingKey": "al2", "Mood": [{"tag": "Calm"}], "Style": {"tag": "Ambient"}}]}})
            return Resp(200, {"MediaContainer": {"Metadata": []}})
        kind = path.rsplit("/", 1)[-1]
        return Resp(200, {"MediaContainer": {"Directory": DIRS.get((kind, t), [])}})


rows = asyncio.run(mt.walk_section(Client(), "http://plex", {}, "3"))
check("tags under each filter, at the level they were listed for",
      ("artist", "a1", "mood", "Brooding") in rows and ("album", "al1", "mood", "Energetic") in rows
      and ("track", "t2", "mood", "Melancholy") in rows and ("album", "al1", "style", "Shoegaze") in rows)
check("...and the tags a listing carries on the item itself",
      ("album", "al2", "mood", "Calm") in rows and ("album", "al2", "style", "Ambient") in rows)
fail["path"] = "/style"
check("one unreadable listing: the section is not stored at all",
      asyncio.run(mt.walk_section(Client(), "http://plex", {}, "3")) is None)
fail["path"] = None

mt.store_section("3", rows)
mt.store_section("4", [("album", "x", "mood", "Gone")])
mt.store_section("4", [("album", "y", "mood", "Fresh")])
with fake_db_session() as db:
    tags = {(r.section_key, r.item_key, r.tag) for r in db.query(MusicTag)}
check("a complete walk replaces only its own section's rows",
      ("4", "y", "Fresh") in tags and ("4", "x", "Gone") not in tags and ("3", "a1", "Brooding") in tags)

# ── 2. a play's moods: track > album > artist ────────────────────────────────
print("\n-- a play takes its track's moods, else its album's, else its artist's --")
_real_parents = mt._parents
mt._parents = lambda keys, names=None: {"t1": ("al1", "a1"), "t2": ("al1", "a1"), "t3": ("al9", "a1"),
                            "t4": ("al9", "a9")}
with fake_db_session() as db:
    got = mt.tags_for_tracks(db, ["t1", "t2", "t3", "t4"])
check("the album's mood when the track has none", got["t1"]["mood"] == ["Energetic"])
check("the track's own mood first", got["t2"]["mood"] == ["Melancholy"])
check("the artist's when neither has one", got["t3"]["mood"] == ["Brooding"])
check("styles from the album", got["t1"]["style"] == ["Shoegaze"] and got["t3"]["style"] == [])
check("a track with nothing is left out", "t4" not in got)

# A play whose key the index does not know (most track events in Plex's
# history have no ratingKey) is found by its artist and album names.
import tempfile  # noqa: E402
import src.services.lyrics as ly  # noqa: E402
ly.LYRICS_DB_PATH = Path(tempfile.mkdtemp()) / "plex_music.db"
con = ly._connect()
con.execute("INSERT INTO track_lyrics (plex_rating_key, artist_key, album_key, artist, album, title, "
            "checked_at) VALUES ('t1', 'a1', 'al1', 'Slowdive', 'Souvlaki', 'Alison', 'x')")
con.execute("INSERT INTO plex_artists (artist_key, name, updated_at) VALUES ('a7', 'Low', 'x')")
con.commit()
con.close()
got = _real_parents(["t1", "s1", "s2", "s3"], {"s1": ("slowdive", "SOUVLAKI"), "s2": ("Low", "Things We Lost"),
                                              "s3": ("Nobody", None)})
check("by the track's key first", got.get("t1") == ("al1", "a1"))
check("else by artist and album names (case and spacing aside)", got.get("s1") == ("al1", "a1"))
check("else by the artist's name alone", got.get("s2") == (None, "a7"))
check("a name the index does not know stays unknown", "s3" not in got)

# ── 3. the Wrap-Up's facts ───────────────────────────────────────────────────
print("\n-- moods by part of the day, players by hours --")
with fake_db_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True))
day = datetime(2026, 6, 1)


def play(rk, hour, minutes, mtype="music", player=None, platform=None, n=1):
    with fake_db_session() as db:
        for i in range(n):
            db.add(W(user_id=1, plex_user_id="1", plex_item_id=rk, title=rk, media_type=mtype,
                     viewed_at=day + timedelta(days=i, hours=hour), duration_ms=minutes * 60_000,
                     view_offset_ms=minutes * 60_000, completed=True, player=player,
                     platform=platform))


play("t1", 8, 60, n=3)                   # mornings: Energetic (album)
play("t2", 23, 60, n=3)                  # nights: Melancholy (track)
play("m1", 23, 120, mtype="movie", player="Living Room TV", n=2)
play("m2", 0, 120, mtype="movie", platform="iOS 17.1 (iPhone14,2)", n=3)
play("m3", 15, 60, mtype="movie", n=1)   # no player known


def facts(types):
    with fake_db_session() as db:
        base = (W.user_id == 1, W.viewed_at >= day, W.viewed_at < day + timedelta(days=30),
                W.media_type.in_(types), wf.a_play(W))
        ms = wf.played_ms_sql(W)
        return wf._moods(db, W, ms, base, W.viewed_at), wf._devices(db, W, ms, base, W.viewed_at)


moods, devices = facts(["music", "movie"])
check("moods over the tagged time", moods["coverage"] == 1.0
      and {x["mood"] for x in moods["top_moods"]} == {"Energetic", "Melancholy"})
check("each part of the day has its leading mood",
      moods["by_daypart"]["morning"]["mood"] == "Energetic"
      and moods["by_daypart"]["night"]["mood"] == "Melancholy")
check("styles over the music time (both tracks are on the Shoegaze album)",
      moods["top_styles"] == [{"style": "Shoegaze", "share": 1.0}])
check("players by hours, a Spotify-style platform read as its family",
      [x["player"] for x in devices["top"]] == ["iPhone", "Living Room TV"]
      and devices["known_share"] == round(10 / 17, 2))
check("the player of the night", devices["night"]["player"] == "iPhone"
      and devices["night"]["share"] == 0.6 and devices["night"]["hours"] == 10)
play("t4", 12, 600, n=2)                 # untagged music: coverage under a third
check("too little tagged music says nothing", facts(["music"])[0] == {})
check("platform family", wf.platform_family("Android OS 13 API 33 (Google, Pixel 7)") == "Android"
      and wf.platform_family("Roku") == "Roku" and wf.platform_family(None) is None)

# ── 4. cards and the fact sheet ──────────────────────────────────────────────
print("\n-- worth saying --")
f = {"music": {"top_artists": [], "moods": moods},
     "devices": {"known_share": 0.9, "top": [{"player": "iPhone", "hours": 12, "share": 0.6}],
                 "night": {"player": "iPhone", "share": 0.7, "hours": 9}}}
cards = wi._moods(f) + wi._devices(f)
check("two soundtracks a day apart is a contrast card",
      any(c["kind"] == "contrast" and "Energetic" in c["claim"] and "Melancholy" in c["claim"]
          for c in cards))
check("the player of the night is a rhythm card",
      any(c["kind"] == "rhythm" and "iPhone" in c["claim"] for c in cards))
check("a night spread over many players says nothing",
      wi._devices({"devices": {"night": {"player": "TV", "share": 0.4, "hours": 9}}}) == [])
sheet, _ = render_fact_sheet(f)
check("the fact sheet carries the moods, the parts of the day and the players",
      "Moods (Plex's tags" in sheet and "In the night: mostly Melancholy" in sheet
      and "Played on: iPhone" in sheet and "After 23:00 mostly on iPhone" in sheet)
check("the facts' shape changed: FACTS_VERSION moved", wf.FACTS_VERSION >= 10)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
