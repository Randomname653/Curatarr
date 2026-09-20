"""A lookup that found nothing must not be asked again tomorrow.

In-memory DB, no MusicBrainz. Covers the backoff ladder, the selection
filter, the success path that clears a miss, and the pipeline's done-check
now reading attempts instead of successes.

    python tests/test_music_misses.py
"""
import pathlib
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from sqlalchemy import create_engine                  # noqa: E402
from sqlalchemy.orm import sessionmaker               # noqa: E402

from src.database.models import Base, MusicLookupMiss  # noqa: E402
import src.database.connection as _conn                # noqa: E402

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
from src.services import music_misses as mm            # noqa: E402


def _reset():
    with _sess() as db:
        db.query(MusicLookupMiss).delete()


NOW = datetime(2026, 9, 20, 12, 0, 0)


def test_each_miss_earns_a_longer_wait():
    _reset()
    assert mm.record_miss("JuliensBlog", now=NOW) == 14
    assert mm.record_miss("JuliensBlog", now=NOW + timedelta(days=20)) == 30
    assert mm.record_miss("JuliensBlog", now=NOW + timedelta(days=60)) == 90
    assert mm.record_miss("JuliensBlog", now=NOW + timedelta(days=200)) == 90, "the last step repeats"
    with _sess() as db:
        row = db.query(MusicLookupMiss).filter(MusicLookupMiss.name == "JuliensBlog").one()
        assert row.attempts == 4 and row.kind == "artist_mbid"
        assert row.last_reason is None
    assert mm.record_miss("", now=NOW) == 0 and mm.record_miss("   ", now=NOW) == 0


def test_the_selection_skips_what_is_still_waiting():
    _reset()
    names = ["Real Artist", "DREWXHILL", "Hotline in Paris"]
    assert mm.due_filter(names, now=NOW) == names, "nothing recorded yet"
    mm.record_miss("DREWXHILL", now=NOW)
    mm.record_miss("Hotline in Paris", now=NOW)
    assert mm.due_filter(names, now=NOW + timedelta(days=1)) == ["Real Artist"]
    assert mm.due_filter(names, now=NOW + timedelta(days=15)) == names, "the wait ran out"
    assert mm.due_filter([], now=NOW) == []
    # order is preserved, so a batch cap still means "the first N that are due"
    many = [f"a{i}" for i in range(10)]
    mm.record_miss("a3", now=NOW)
    assert mm.due_filter(many, now=NOW + timedelta(days=1)) == [n for n in many if n != "a3"]


def test_a_later_success_clears_the_miss():
    _reset()
    mm.record_miss("Axwell", now=NOW)
    assert mm.due_filter(["Axwell"], now=NOW + timedelta(days=1)) == []
    mm.clear_miss("Axwell")
    assert mm.due_filter(["Axwell"], now=NOW + timedelta(days=1)) == ["Axwell"]
    with _sess() as db:
        assert db.query(MusicLookupMiss).count() == 0


def test_the_stats_say_how_much_is_parked():
    _reset()
    mm.record_miss("one", now=NOW)
    mm.record_miss("two", now=NOW)
    s = mm.stats(now=NOW + timedelta(days=1))
    assert s["missed"] == 2 and s["waiting"] == 2 and s["due"] == 0
    assert s["next_retry_at"].startswith("2026-10-04")
    s = mm.stats(now=NOW + timedelta(days=30))
    assert s["waiting"] == 0 and s["due"] == 2 and s["next_retry_at"] is None


def test_the_resolver_records_and_skips():
    src = (_ROOT / "src/services/music_matcher.py").read_text(encoding="utf-8")
    phase = src.split("async def resolve_artist_mbids")[1].split("\nasync def ")[0]
    assert "music_misses.due_filter(unresolved)" in phase, "the queue is filtered before the batch cap"
    assert 'music_misses.record_miss(name, reason="musicbrainz: no match")' in phase
    assert "music_misses.clear_miss(name)" in phase
    assert '"parked":       parked,' in phase
    assert phase.index("due_filter") < phase.index("batch and batch > 0"), \
        "filter first, then cap — otherwise the cap fills up with parked names"


def test_the_done_check_counts_attempts_not_successes():
    cust = (_ROOT / "src/services/data_custodian.py").read_text(encoding="utf-8")
    block = cust.split("async def _run_spotify")[1].split("\nasync def ")[0]
    assert 'mbid.get("queried", 0) or 0,' in block
    assert 'mbid.get("resolved", 0)' not in block, \
        "reading successes said drained while a full batch had just been burned"
    assert "return busy < batch" in block


def test_the_table_is_migrated_for_existing_installs():
    conn = (_ROOT / "src/database/connection.py").read_text(encoding="utf-8")
    assert '("music_lookup_misses",   "last_reason"' in conn
    models = (_ROOT / "src/database/models.py").read_text(encoding="utf-8")
    assert "class MusicLookupMiss(Base):" in models
    assert '__tablename__ = "music_lookup_misses"' in models


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
