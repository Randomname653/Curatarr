"""Lidarr optional, step 6: the Knowledge Base music row, the Discogs
artist universe, the settings hint and the docs.

    python tests/test_lidarr_optional.py
"""
import asyncio
import pathlib
import sys
import tempfile
from contextlib import contextmanager

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.database.models import Base, EnrichmentStatus  # noqa: E402
import src.database.connection as _conn  # noqa: E402
from src.services import lyrics as ly  # noqa: E402

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
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="curatarr-lidarr-optional-"))
_ORIG_DB = ly.LYRICS_DB_PATH


def _seed_index(name):
    ly.LYRICS_DB_PATH = ly.PLEX_MUSIC_DB_PATH = _TMP / f"{name}.db"

    def track(key, akey, aname, album):
        return {"ratingKey": key, "grandparentRatingKey": akey, "parentRatingKey": "al-" + album, "grandparentTitle": aname,
                "parentTitle": album, "title": "T" + key, "duration": 1, "Media": [{"Part": [{"size": 10, "Stream": []}]}]}
    tracks = [track("1", "a1", "The Band", "A"), track("2", "a1", "The Band", "B"), track("3", "a2", "Other", "C")]
    artists = [{"ratingKey": "a1", "title": "The Band", "Guid": [{"id": "mbid://aaaa-1"}]}, {"ratingKey": "a2", "title": "Other"}]

    async def lt(sec):
        return tracks

    async def la(sec):
        return artists

    async def ft(k):
        return None
    asyncio.run(ly.sync_lyrics([("14", "Music")], lt, ft, list_artists=la, db_path=ly.LYRICS_DB_PATH))


def test_the_knowledge_base_music_row_comes_from_the_index():
    from src.routers import enrichment as enr
    _seed_index("counts")
    try:
        with _sess() as db:
            db.query(EnrichmentStatus).delete()
            db.add(EnrichmentStatus(plex_rating_key="a1", title="the band", media_category="music", enriched=True))
            db.add(EnrichmentStatus(plex_rating_key="x", title="Nobody", media_category="music", enriched=True))
        c = enr._plex_music_counts(count_vectors=lambda svc: 7)
        assert c["source"] == "plex" and c["total"] == 2 and c["total_albums"] == 3 and c["downloaded"] == 2
        assert c["enriched"] == 1 and c["pct"] == 50 and c["vector_count"] == 7 and c["stale"] is False
        assert enr._plex_music_counts()["vector_count"] == 0, "no counter handed in: zero, never a NameError"
        ly.LYRICS_DB_PATH = ly.PLEX_MUSIC_DB_PATH = _TMP / "empty.db"
        assert enr._plex_music_counts() == {}, "no index: the caller keeps whatever it had"
    finally:
        ly.LYRICS_DB_PATH = ly.PLEX_MUSIC_DB_PATH = _ORIG_DB


def test_the_readers_and_the_docs_know_the_index():
    enr = (_ROOT / "src/routers/enrichment.py").read_text(encoding="utf-8")
    assert "if not (settings.LIDARR_URL and settings.LIDARR_API_KEY):" in enr and "_plex_music_counts(_count_vectors)" in enr
    disc = (_ROOT / "src/services/discogs_offline.py").read_text(encoding="utf-8")
    assert "from src.services.lyrics import plex_artists" in disc
    js = (_ROOT / "frontend/js/library_settings.js").read_text(encoding="utf-8")
    assert "Optional. Without Lidarr the Music page" in js
    arch = (_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "| Lidarr optional |" in arch and "**Lidarr optional (2026-09-15).**" in arch
    assert "**Lidarr is optional.**" in (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    usage = (_ROOT / "docs/USAGE.md").read_text(encoding="utf-8")
    assert "| Music without Lidarr |" in usage and "| Wanted artists (no Lidarr) |" in usage
    assert "| **Plex music index** |" in (_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Without Lidarr,\n  music deletions go through Plex" in (_ROOT / "SECURITY.md").read_text(encoding="utf-8")


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
