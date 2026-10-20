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
        r = (lib.add_wish(lib.WishRequest(title="Neelix", source="rec"), ADMIN))
        assert r["ok"] and not r["already"] and r["in_library"] is False
        again = (lib.add_wish(lib.WishRequest(title="  neelix ", source="backlog"), ADMIN))
        assert again["already"] and again["id"] == r["id"], "same artist by name: one open wish"
        have = (lib.add_wish(lib.WishRequest(title="the band", mbid="aaaa-1", source="backlog"), ADMIN))
        assert have["in_library"] is True, "Plex has it: the caller says so instead of wishing"
        listed = (lib.list_wishes(ADMIN))
        assert listed["total"] == 2 and [w["title"] for w in listed["wishes"]] == ["the band", "Neelix"]
        assert [w["in_library"] for w in listed["wishes"]] == [True, False]
        assert (lib.remove_wish(r["id"], ADMIN)) == {"ok": True}
        assert (lib.list_wishes(ADMIN))["total"] == 1
        try:
            (lib.remove_wish(99999, ADMIN))
            assert False, "expected 404"
        except Exception as e:
            assert "no such wish" in str(e)
        try:
            (lib.add_wish(lib.WishRequest(title="   "), ADMIN))
            assert False, "expected 400"
        except Exception as e:
            assert "title required" in str(e)
    finally:
        ly.LYRICS_DB_PATH = ly.PLEX_MUSIC_DB_PATH = _ORIG_DB


def test_members_wish_under_their_name_and_only_take_back_their_own():
    """Owner decision 2026-10-20: household members may wish too. The wish
    carries their name; the owner removes any wish, a member only theirs;
    members have an hourly budget and a cap on open wishes."""
    from fastapi import HTTPException
    from src.database.models import User
    from src.routers import library as lib
    from src.services import rate_limit as rl
    member = types.SimpleNamespace(id=2, is_admin=False)
    other = types.SimpleNamespace(id=3, is_admin=False)
    _seed_index("member-wishes")
    rl.reset()
    try:
        with _sess() as db:
            db.query(MusicWish).delete()
            db.query(User).delete()
            db.add_all([User(id=1, plex_user_id="1", plex_username="owner", is_admin=True),
                        User(id=2, plex_user_id="2", plex_username="ana", is_admin=False),
                        User(id=3, plex_user_id="3", plex_username="bo", is_admin=False)])
        mine = lib.add_wish(lib.WishRequest(title="Neelix", source="rec"), member)
        owners = lib.add_wish(lib.WishRequest(title="Boards of Canada"), ADMIN)
        assert mine["ok"] and not mine["already"], "a member can wish"
        seen = {w["title"]: w for w in lib.list_wishes(member)["wishes"]}
        assert seen["Neelix"]["wished_by"] == "ana" and seen["Neelix"]["own"] is True
        assert seen["Neelix"]["can_remove"] is True
        assert seen["Boards of Canada"]["wished_by"] == "owner"
        assert seen["Boards of Canada"]["can_remove"] is False, "not the member's wish"
        assert all(w["can_remove"] for w in lib.list_wishes(ADMIN)["wishes"]), "the owner removes any"
        for who, wid in ((member, owners["id"]), (other, mine["id"])):
            try:
                lib.remove_wish(wid, who)
                assert False, "a member removed someone else's wish"
            except HTTPException as e:
                assert e.status_code == 404, "no hint that the wish exists"
        assert lib.remove_wish(mine["id"], member) == {"ok": True}
        # The cap on open wishes, then the hourly budget.
        saved = (rl.MEMBER_OPEN_WISHES, rl.MEMBER_WISHES_PER_HOUR)
        rl.MEMBER_OPEN_WISHES, rl.MEMBER_WISHES_PER_HOUR = 2, 3
        try:
            rl.reset()
            lib.add_wish(lib.WishRequest(title="A1"), other)
            lib.add_wish(lib.WishRequest(title="A2"), other)
            try:
                lib.add_wish(lib.WishRequest(title="A3"), other)
                assert False, "open-wish cap not applied"
            except HTTPException as e:
                assert e.status_code == 429 and "open wishes" in e.detail
            rl.MEMBER_OPEN_WISHES = 50
            lib.add_wish(lib.WishRequest(title="A4"), other)
            try:
                lib.add_wish(lib.WishRequest(title="A5"), other)
                assert False, "hourly budget not applied"
            except HTTPException as e:
                assert e.status_code == 429
            assert lib.add_wish(lib.WishRequest(title="A6"), ADMIN)["ok"], "the owner has no budget"
            dup = lib.add_wish(lib.WishRequest(title="a1"), member)
            assert dup["already"], "an existing wish is returned, never filed twice"
        finally:
            rl.MEMBER_OPEN_WISHES, rl.MEMBER_WISHES_PER_HOUR = saved
            rl.reset()
    finally:
        ly.LYRICS_DB_PATH = ly.PLEX_MUSIC_DB_PATH = _ORIG_DB


def test_members_get_a_wish_button_and_the_list_names_who_asked():
    recs = (_ROOT / "frontend/js/recs.js").read_text(encoding="utf-8")
    assert "act('onWishRec', EL)" in recs and "_membersWishViaPlex" in recs
    assert "rec.category === 'music' && _membersWishViaPlex" in recs
    arr = (_ROOT / "frontend/js/arr.js").read_text(encoding="utf-8")
    assert "w.can_remove ?" in arr and "wished by ${w.own ? 'you'" in arr
    app = (_ROOT / "frontend/js/app.js").read_text(encoding="utf-8")
    assert "  onWishRec," in app
    lib = (_ROOT / "src/routers/library.py").read_text(encoding="utf-8")
    add = lib[lib.index('@router.post("/wish")'):lib.index('@router.delete("/wish/{wish_id}")')]
    assert "Depends(get_current_user)" in add and "require_admin" not in add


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
