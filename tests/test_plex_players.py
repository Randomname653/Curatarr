"""The player of every Plex play, and the moods of every artist (2026-10-25).

Measured on the real server:
- every history event names its device (deviceID; /devices: "iPhone" /
  "iOS"); nothing stored it, so "phone after midnight" was impossible;
- most music events no longer carry a ratingKey (the track was re-added),
  so a stored play is found by its second and title as well;
- Plex keeps moods and styles per artist, only in the artist's own metadata.

    python tests/test_plex_players.py
"""
import calendar
import json
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from src.database.models import Base, WatchHistoryEntry as W  # noqa: E402
import src.services.plex_players as pp  # noqa: E402
from src.services import lyrics  # noqa: E402
from src.services import plex_sync as ps  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


PLAYERS = {7: ("iPhone", "iOS"), 9: ("Desktop", "Windows")}
T = datetime(2026, 10, 3, 23, 30)
TS = calendar.timegm(T.utctimetuple())

# ── one play's player, in the sync ──────────────────────────────────────────
check("a history event names its own device",
      ps.player_of(7, "100", TS, {}, PLAYERS) == ("iPhone", "iOS"))
check("a listed play takes the device of the same item's event within 90 s",
      ps.player_of(None, "100", TS, {"100": [(TS + 40, 9), (TS + 3000, 7)]}, PLAYERS) == ("Desktop", "Windows"))
check("no event near it, or an unknown device: nothing",
      ps.player_of(None, "100", TS, {"100": [(TS + 300, 9)]}, PLAYERS) == (None, None)
      and ps.player_of(55, "100", TS, {}, PLAYERS) == (None, None))
check("the history walk keeps the device", "deviceID" in ps._HISTORY_EVENT_KEYS)
src = (ROOT / "src/services/plex_sync.py").read_text(encoding="utf-8")
check("the sync writes the player and the platform of a new play",
      "player=player, platform=platform," in src and "players = await fetch_players(plex_url, headers)" in src)
orphan = (ROOT / "src/services/orphan_repair.py").read_text(encoding="utf-8")
check("...and so does the import of history plays", "player=_player[0], platform=_player[1]," in orphan)

# ── the plays already stored ────────────────────────────────────────────────
engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
Base.metadata.create_all(engine)
S = sessionmaker(bind=engine)


@contextmanager
def fake_session():
    db = S()
    try:
        yield db
    finally:
        db.close()


pp.get_db_session = fake_session
db = S()
db.add(W(user_id=1, plex_user_id="1", plex_item_id="100", title="Pilot", media_type="show", viewed_at=T, completed=True))
db.add(W(user_id=1, plex_user_id="1", plex_item_id="555", title="Schattenfraß", media_type="music",
         series_title="ASP", viewed_at=T, completed=True))
db.add(W(user_id=1, plex_user_id="1", plex_item_id="556", title="Another Song", media_type="music",
         viewed_at=T, completed=True))
db.add(W(user_id=1, plex_user_id="1", plex_item_id="sp", title="Pilot", media_type="music", source="spotify",
         viewed_at=T, completed=True, platform="android"))
db.commit()
db.close()
by_rk = {"100": [(TS - 30, 7)]}
by_ts = {TS + 1: [(9, "schattenfraß")]}         # the track was re-added: no key, same second
n = pp.fill_players(by_rk, PLAYERS, by_ts)
db = S()
got = {r.plex_item_id: (r.player, r.platform) for r in db.query(W)}
db.close()
check("a stored play gets the player of its item's event",
      got["100"] == ("iPhone", "iOS"))
check("a re-added track's play is found by its second and title",
      got["555"] == ("Desktop", "Windows") and got["556"] == (None, None) and n == 2)
check("Spotify keeps its own platform", got["sp"] == (None, "android"))
check("the backfill runs once (a state key), the custodian holds it, no model",
      pp.PLAYERS_DONE_KEY == "plex_players_backfilled")

# ── an artist's moods ───────────────────────────────────────────────────────
tmp = Path(tempfile.mkdtemp()) / "lyrics.db"
con = lyrics._connect(tmp)
for key, tracks, tags_at in (("a1", 50, None), ("a2", 80, "2026-01-01T00:00:00"),
                             ("a3", 10, "2026-10-20T00:00:00"), ("a4", 0, None)):
    con.execute("INSERT INTO plex_artists (artist_key, name, tracks, updated_at, tags_at) VALUES (?,?,?,?,?)",
                (key, key, tracks, "x", tags_at))
con.commit()
con.close()
due = pp._due_artists(10, now=datetime(2026, 10, 25), db_path=tmp)
check("artists without tags, or with tags older than 90 days, the most tracks first; none without tracks",
      due == ["a2", "a1"])
md = {"Mood": [{"tag": "Atmospheric"}, {"tag": "Melodic"}], "Style": [{"tag": "Indie Folk"}],
      "Genre": [{"tag": "Folk"}], "Country": [{"tag": "Germany"}]}
t = pp.tags_of(md)
check("moods, styles, genres and the country of an artist's metadata",
      t == {"moods": ["Atmospheric", "Melodic"], "styles": ["Indie Folk"], "genres": ["Folk"], "country": "Germany"})
pp.save_artist_tags({"a1": t}, now=datetime(2026, 10, 25), db_path=tmp)
con = lyrics._connect(tmp)
row = con.execute("SELECT moods, styles, country, tags_at FROM plex_artists WHERE artist_key='a1'").fetchone()
con.close()
check("...stored on the artist in the music index",
      json.loads(row[0]) == ["Atmospheric", "Melodic"] and json.loads(row[1]) == ["Indie Folk"]
      and row[2] == "Germany" and row[3].startswith("2026-10-25"))
check("...and not due again", pp._due_artists(10, now=datetime(2026, 10, 26), db_path=tmp) == ["a2"])

from src.services.data_custodian import _registry  # noqa: E402

jobs = {t.job_id: t for t in _registry()}
check("both jobs run in the custodian without a model",
      all(j in jobs and not jobs[j].needs_llm for j in ("plex_players", "artist_tags")))
conn = (ROOT / "src/database/connection.py").read_text(encoding="utf-8")
check("existing installs get the player column", '("watch_history",          "player",' in conn)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
