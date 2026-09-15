"""Lidarr optional, step 4: the wanted list.

    python tests/test_music_wishes.py
"""
import asyncio
import pathlib
import sys
import tempfile
import types
from contextlib import contextmanager

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.database.models import Base, MusicWish  # noqa: E402
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
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="curatarr-wishes-"))
_ORIG_DB = ly.LYRICS_DB_PATH
ADMIN = types.SimpleNamespace(id=1, is_admin=True)


def _seed_index(name):
    ly.LYRICS_DB_PATH = ly.PLEX_MUSIC_DB_PATH = _TMP / f"{name}.db"
    tracks = [{"ratingKey": "1", "grandparentRatingKey": "a1", "parentRatingKey": "al1", "grandparentTitle": "The Band",
               "parentTitle": "First", "title": "One", "duration": 1, "Media": [{"Part": [{"size": 10, "Stream": []}]}]}]
    artists = [{"ratingKey": "a1", "title": "The Band", "Guid": [{"id": "mbid://aaaa-1"}]}]

    async def lt(sec):
        return tracks

    async def la(sec):
        return artists

    async def ft(k):
        return None
    asyncio.run(ly.sync_lyrics([("14", "Music")], lt, ft, list_artists=la, db_path=ly.LYRICS_DB_PATH))


def test_wishes_are_added_once_listed_with_their_library_state_and_removed():
    from src.routers import library as lib
    _seed_index("wishes")
    try:
        with _sess() as db:
            db.query(MusicWish).delete()
        r = asyncio.run(lib.add_wish(lib.WishRequest(title="Neelix", source="rec"), ADMIN))
        assert r["ok"] and not r["already"] and r["in_library"] is False
        again = asyncio.run(lib.add_wish(lib.WishRequest(title="  neelix ", source="backlog"), ADMIN))
        assert again["already"] and again["id"] == r["id"], "same artist by name: one open wish"
        have = asyncio.run(lib.add_wish(lib.WishRequest(title="the band", mbid="aaaa-1", source="backlog"), ADMIN))
        assert have["in_library"] is True, "Plex has it: the caller says so instead of wishing"
        listed = asyncio.run(lib.list_wishes(ADMIN))
        assert listed["total"] == 2 and [w["title"] for w in listed["wishes"]] == ["the band", "Neelix"]
        assert [w["in_library"] for w in listed["wishes"]] == [True, False]
        assert asyncio.run(lib.remove_wish(r["id"], ADMIN)) == {"ok": True}
        assert asyncio.run(lib.list_wishes(ADMIN))["total"] == 1
        try:
            asyncio.run(lib.remove_wish(99999, ADMIN))
            assert False, "expected 404"
        except Exception as e:
            assert "no such wish" in str(e)
        try:
            asyncio.run(lib.add_wish(lib.WishRequest(title="   "), ADMIN))
            assert False, "expected 400"
        except Exception as e:
            assert "title required" in str(e)
    finally:
        ly.LYRICS_DB_PATH = ly.PLEX_MUSIC_DB_PATH = _ORIG_DB


def test_the_pages_write_and_show_wishes():
    arr = (_ROOT / "frontend/js/arr.js").read_text(encoding="utf-8")
    assert "{ id: 'wanted', label: 'Wanted' }" in arr and "export async function renderWanted" in arr
    assert "act('wishArtist', svc, idx, EL)" in arr and "act('removeWish', w.id, EL)" in arr
    assert "['all', 'backlog', 'wanted'].includes(t.id)" in arr and "t.id !== 'wanted'" in arr
    recs = (_ROOT / "frontend/js/recs.js").read_text(encoding="utf-8")
    assert "lid.music_source === 'plex'" in recs and "'/api/library/wish'" in recs
    app = (_ROOT / "frontend/js/app.js").read_text(encoding="utf-8")
    for name in ("wishArtist", "renderWanted", "removeWish"):
        assert f"  {name}," in app, name
    lib = (_ROOT / "src/routers/library.py").read_text(encoding="utf-8")
    assert '"in_library":      bool(r.mbid) and r.mbid in in_lidarr' in lib
    assert "in_lidarr.update(a[\"mbid\"] for a in plex_artists() if a.get(\"mbid\"))" in lib


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
