"""Music context: skips kept, albums and the export's own account of a play.

The Spotify import dropped every play under 30 seconds — which is where skips
live — and the album, platform, end reason and shuffle state of every play.
A re-uploaded file after matching read every play as new, because dedup keyed
on plex_item_id, which the matcher rewrites. Plex tracks carried no album.

    python tests/test_music_context.py
"""
import json
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from _harness import check  # noqa: E402  (first: temp stores and .env, never the live data/)
import src.database.connection as conn  # noqa: E402
import src.services.spotify_import as si  # noqa: E402
from src.database.models import Base, MusicSkip, User, WatchHistoryEntry  # noqa: E402


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, autoflush=False)


@contextmanager
def fake_session():
    db = Session()
    try:
        yield db
    finally:
        db.close()


conn.get_db_session = fake_session
with fake_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True))
    db.add(User(id=2, plex_user_id="2", plex_username="member"))
    db.commit()


def play(ts, track, ms, uri, reason="trackdone", album="Blonde", platform="ios", shuffle=False,
         skipped=False):
    return {"ts": ts, "master_metadata_track_name": track,
            "master_metadata_album_artist_name": "Frank Ocean",
            "master_metadata_album_album_name": album, "spotify_track_uri": uri,
            "ms_played": ms, "reason_end": reason, "platform": platform, "shuffle": shuffle,
            "skipped": skipped}


FILE = [
    play("2026-03-01T22:00:00Z", "Ivy", 249_000, "spotify:track:ivy"),
    play("2026-03-01T22:05:00Z", "Solo", 12_000, "spotify:track:solo", reason="fwdbtn", skipped=True),
    play("2026-03-01T22:06:00Z", "Nights", 307_000, "spotify:track:nights", platform="windows",
         shuffle=True),
    {"ts": "2026-03-01T22:12:00Z", "episode_name": "A podcast", "ms_played": 900_000},
]
TMP = Path(tempfile.mkdtemp(prefix="curatarr-music-ctx-"))
(TMP / "Streaming_History_Audio_2026.json").write_text(json.dumps(FILE), encoding="utf-8")
st = si.run_import(2, TMP)


def plays(uid=2):
    with fake_session() as db:
        return sorted(((r.title, r.album, r.listened_ms, r.platform, r.reason_end, r.shuffle)
                       for r in db.query(WatchHistoryEntry).filter_by(user_id=uid)))


def skips(uid=2):
    with fake_session() as db:
        return [(s.title, s.artist, s.album, s.ms_played, s.reason_end, s.platform)
                for s in db.query(MusicSkip).filter_by(user_id=uid).order_by(MusicSkip.at)]


check("a play keeps its album, how long it ran, on what, why it ended, shuffled or not",
      plays() == [("Ivy", "Blonde", 249_000, "ios", "trackdone", False),
                  ("Nights", "Blonde", 307_000, "windows", "trackdone", True)])
check("a play under 30 s is a skip, kept apart from the plays",
      skips() == [("Solo", "Frank Ocean", "Blonde", 12_000, "fwdbtn", "ios")]
      and st["imported"] == 2 and st["skips"] == 1 and st["skipped"] == 1)

# the matcher rewrites plex_item_id to the Plex ratingKey; then the same
# export is uploaded again
with fake_session() as db:
    for r in db.query(WatchHistoryEntry).all():
        r.plex_item_id = f"rk{r.id}"
    db.commit()
(TMP / "Streaming_History_Audio_2026.json").write_text(json.dumps(FILE), encoding="utf-8")
st = si.run_import(2, TMP)
check("re-uploading the export after matching adds nothing — dedup keys on the URI now",
      st["imported"] == 0 and st["duplicates"] == 2 and st["skips"] == 0 and len(plays()) == 2)

# ── what earlier imports dropped, read again from imported/ ─────────────────
with fake_session() as db:
    for r in db.query(WatchHistoryEntry).all():
        r.album = r.listened_ms = r.platform = r.reason_end = r.shuffle = None
    db.query(MusicSkip).delete()
    db.commit()
files = si.imported_files(TMP)
check("the imported files are still there to read", [f.name for f in files] == [
    "Streaming_History_Audio_2026.json"])
got = si.reread_file(files[0])
check("re-reading fills the stored plays' empty columns",
      got["filled"] == 2 and plays()[0] == ("Ivy", "Blonde", 249_000, "ios", "trackdone", False))
check("...and brings the skips in, for the user whose plays the file holds",
      got["skips"] == 1 and skips() == [("Solo", "Frank Ocean", "Blonde", 12_000, "fwdbtn", "ios")]
      and skips(1) == [])
again = si.reread_file(files[0])
check("re-reading twice changes nothing", again["filled"] == 0 and again["skips"] == 0)
stray = TMP / "imported" / "Streaming_History_Audio_2019.json"
stray.write_text(json.dumps([play("2019-01-01T10:00:00Z", "Lost", 5_000, "spotify:track:lost")]),
                 encoding="utf-8")
got = si.reread_file(stray)
check("skips of a file none of whose plays are stored are not guessed onto anyone",
      got["unattributed_skips"] == 1 and got["skips"] == 0)

# ── the Wrap-Up's skip rate comes from the skips ────────────────────────────
from datetime import timezone as _tz  # noqa: E402

import src.services.user_time as ut  # noqa: E402
import src.services.wrapup_facts as wf  # noqa: E402

wf.get_db_session = fake_session
wf._verified_lookup = lambda items: {}
ut.server_zone = lambda: _tz.utc
with fake_session() as db:
    for i in range(3):
        db.add(MusicSkip(user_id=2, at=datetime(2026, 3, 2, 9, i), title="Solo", artist="Frank Ocean",
                         spotify_uri=f"spotify:track:solo{i}", ms_played=5_000))
    for i in range(6):      # the owner listens on Plexamp only: every row is written finished
        db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id=f"t{i}", title=f"T{i}",
                                 series_title="Aphex Twin", media_type="music", completed=True,
                                 duration_ms=300_000, viewed_at=datetime(2026, 3, 5, 20, i)))
    db.commit()
m = wf.build_wrapup_facts(2, "music", "year:2026", now=datetime(2026, 12, 31))["music"]
check("Spotify's skip rate counts the skips: 4 skips over 2 plays and 4 skips",
      m["skip_rate"] == round(4 / 6, 2))
check("...and names the track skipped most", m["most_skipped"] == {
    "track": "Solo", "artist": "Frank Ocean", "skips": 4})
owner = wf.build_wrapup_facts(1, "music", "year:2026", now=datetime(2026, 12, 31))["music"]
check("a Plexamp-only listener gets no skip rate instead of 'skipped 0%'",
      "skip_rate" not in owner and owner["top_artists"][0]["artist"] == "Aphex Twin")
check("facts carry the new generation", wf.FACTS_VERSION >= 4)

# ── Plex tracks carry their album ───────────────────────────────────────────
import src.services.plex_sync as ps  # noqa: E402

check("the owner's history events keep the album", "parentTitle" in ps._HISTORY_EVENT_KEYS)
ev = ps._member_entry({"ratingKey": "9", "viewedAt": 1, "parentTitle": "Blonde", "librarySectionID": 3},
                      {"3": ("music", "artist", "Music")}, 2002)
check("a history event as a sync entry carries the album", ev["parentTitle"] == "Blonde")
src = (Path(__file__).resolve().parents[1] / "src/services/plex_sync.py").read_text(encoding="utf-8")
check("the sync writes a track's album, and none for video",
      'album=((entry.get("parentTitle") or "")[:512] or None)' in src
      and 'if media_type == "music" else None' in src)

from src.services.data_custodian import _registry  # noqa: E402

job = next((t for t in _registry() if t.job_id == "spotify_reread"), None)
check("the custodian re-reads the imported files (once; no model)",
      job is not None and not job.needs_llm)

check.done()
