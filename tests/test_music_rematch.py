"""Music that arrives later must find the history that was waiting for it.

In-memory watch history, a throwaway copy of the Plex track index, no Plex
and no network.

    python tests/test_music_rematch.py
"""
import pathlib
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from sqlalchemy import create_engine                       # noqa: E402
from sqlalchemy.orm import sessionmaker                    # noqa: E402

from src.database.models import Base, WatchHistoryEntry    # noqa: E402
import src.database.connection as _conn                    # noqa: E402

_engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
Base.metadata.create_all(_engine)
_Session = sessionmaker(bind=_engine)


@contextmanager
def _sess():
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


_conn.get_db_session = _sess
from src.services import music_rematch as rm               # noqa: E402

_STATE = {}


def _gs(k):
    return _STATE.get(k)


def _ss(k, v):
    _STATE[k] = v


def _play(pid, artist, title, matched=False):
    return WatchHistoryEntry(
        id=pid, user_id=1, plex_user_id=1, viewed_at=datetime(2026, 9, 21, 12, 0),
        media_type="music", source="spotify",
        series_title=artist, title=title,
        plex_item_id=("42" if matched else f"spotify:track:{pid}"))


def _seed_plays(rows):
    with _sess() as db:
        db.query(WatchHistoryEntry).delete()
        for r in rows:
            db.add(r)
    _STATE.clear()


def _index(tracks):
    """A throwaway copy of the collector's track table."""
    path = pathlib.Path(tempfile.mkdtemp()) / "plex_music.db"
    con = sqlite3.connect(path)
    con.execute("create table track_lyrics (plex_rating_key text, artist text, "
                "title text, added_at integer)")
    con.executemany("insert into track_lyrics values (?,?,?,?)", tracks)
    con.commit()
    con.close()
    return str(path)


def _pid_state():
    with _sess() as db:
        return {p.id: p.plex_item_id for p in db.query(WatchHistoryEntry).all()}


def test_an_arrival_claims_every_play_that_was_waiting():
    _seed_plays([
        _play(1, "Lauder", "Fear"),
        _play(2, "Lauder", "Fear"),        # same track, played twice
        _play(3, "lauder ", " FEAR"),      # normalisation still finds it
        _play(4, "Lauder", "Worry"),
        _play(5, "Someone Else", "Fear"),  # same title, other artist
    ])
    path = _index([("955212", "Lauder", "Fear", 1789826132)])
    r = rm.rematch_arrivals(1, db_path=path, get_state=_gs, set_state=_ss)
    assert r["arrivals"] == 1 and r["matched_tracks"] == 1 and r["matched_plays"] == 3, r
    state = _pid_state()
    assert state[1] == state[2] == state[3] == "955212"
    assert state[4].startswith("spotify:") and state[5].startswith("spotify:")
    assert _STATE["music_rematch_added_at:1"] == "1789826132"


def test_a_second_run_only_looks_at_what_is_new():
    _seed_plays([_play(1, "Lauder", "Fear"), _play(2, "Nova", "Drift")])
    path = _index([("100", "Lauder", "Fear", 1000),
                   ("200", "Nova", "Drift", 2000)])
    first = rm.rematch_arrivals(1, db_path=path, get_state=_gs, set_state=_ss)
    assert first["arrivals"] == 2 and first["matched_plays"] == 2
    second = rm.rematch_arrivals(1, db_path=path, get_state=_gs, set_state=_ss)
    assert second == {"arrivals": 0, "matched_plays": 0, "matched_tracks": 0,
                      "since": 2000}, "a quiet run is one query and nothing else"


def test_an_already_matched_play_is_never_overwritten():
    _seed_plays([_play(1, "Lauder", "Fear", matched=True)])
    path = _index([("999", "Lauder", "Fear", 5000)])
    r = rm.rematch_arrivals(1, db_path=path, get_state=_gs, set_state=_ss)
    assert r["matched_plays"] == 0 and r["matched_tracks"] == 0
    assert _pid_state()[1] == "42", "the existing match stands"
    assert _STATE["music_rematch_added_at:1"] == "5000", "the stamp still advances"


def test_nothing_to_match_still_advances_the_stamp():
    _seed_plays([])
    path = _index([("1", "Ghost", "Track", 7000), ("2", "Ghost", "Other", 7100)])
    r = rm.rematch_arrivals(1, db_path=path, get_state=_gs, set_state=_ss)
    assert r["arrivals"] == 2 and r["matched_plays"] == 0
    assert _STATE["music_rematch_added_at:1"] == "7100", \
        "an empty history must not make the same arrivals repeat forever"


def test_a_missing_or_broken_index_is_survivable():
    _seed_plays([_play(1, "Lauder", "Fear")])
    assert rm.arrivals(0, db_path=str(pathlib.Path(tempfile.mkdtemp()) / "nope.db")) == []
    r = rm.rematch_arrivals(1, db_path="/definitely/not/here.db", get_state=_gs, set_state=_ss)
    assert r["arrivals"] == 0 and r["matched_plays"] == 0
    assert _pid_state()[1].startswith("spotify:"), "nothing touched"


def test_blank_artists_and_titles_are_skipped():
    _seed_plays([_play(1, "", "Fear"), _play(2, "Lauder", "")])
    path = _index([("1", "", "Fear", 10), ("2", "Lauder", "", 20), ("3", None, None, 30)])
    r = rm.rematch_arrivals(1, db_path=path, get_state=_gs, set_state=_ss)
    assert r["arrivals"] == 0, "the index query drops them before we get here"


def test_the_pipeline_runs_it_as_phase_1b():
    src = (_ROOT / "src/services/music_matcher.py").read_text(encoding="utf-8")
    body = src.split("async def run_music_pipeline")[1]
    assert "from src.services.music_rematch import rematch_arrivals" in body
    assert '"phase1b_arrivals":     phase1b,' in body
    assert body.index("rematch_arrivals(user_id)") > body.index("match_spotify_to_plex(user_id)"), \
        "the incremental pass first, then the arrivals"
    guarded = body.split("phase1b = rematch_arrivals(user_id)")[0].rsplit("try:", 1)[-1]
    assert "import rematch_arrivals" in guarded, "import and call sit inside the same try"
    assert len(guarded.splitlines()) <= 3, "nothing else crept between the try and the call"
    assert 'phase1b = {"error": str(e)}' in body, "a broken index must not abort the pipeline"


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    sys.exit(1 if fails else 0)
