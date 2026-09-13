"""Tech profiles must follow Plex; duplicate claims must be true.

2026-09-13, "Armor Shop for Ladies & Gentlemen": the curator told the owner he
kept two separate copies (2.7 GB redundant) of a series that exists once. The
second "copy" was a MediaTechProfile row under a rating key Plex had dropped
weeks earlier - 1,373 such phantom groups existed. This suite pins the fixes:
identical files under different keys are one copy, the tech sync prunes
rating keys Plex no longer has (complete fetches only), and the curator is
told it executes nothing itself.

    python tests/test_tech_profile_hygiene.py
"""
import asyncio
import pathlib
import sys
from contextlib import contextmanager

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.database.models import Base, MediaTechProfile  # noqa: E402
import src.database.connection as _conn  # noqa: E402
import src.services.size_norms as sn  # noqa: E402
import src.services.plex_sync as ps  # noqa: E402

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
sn.get_db_session = _sess
ps.get_db_session = _sess


def _reset(*rows):
    with _sess() as db:
        db.query(MediaTechProfile).delete()
        for r in rows:
            db.add(MediaTechProfile(**r))
    sn._CROSS_DUP["data"] = None


def _prof(key, **kw):
    base = dict(plex_rating_key=key, media_type="anime", title="Armor Shop", tvdb_id=353608,
                tmdb_id=82958, size_mb=2715.0, duration_min=70.3, mb_per_min=38.6,
                resolution="1080", codec="hevc", item_count=15, versions=1, redundant_mb=0.0)
    base.update(kw)
    return base


def test_identical_rows_under_stale_keys_are_one_copy():
    _reset(_prof("676153"), _prof("871390"))
    assert sn._cross_dup_note(82958, 353608, "anime") == "", "same files, two keys: not a duplicate"
    assert sn.duplicate_report()["cross_item"] == []
    # a genuinely different encode IS a second copy, and only its size is redundant
    _reset(_prof("676153"), _prof("871390"), _prof("900001", size_mb=1200.0, codec="h264"))
    note = sn._cross_dup_note(82958, 353608, "anime")
    assert "2 separate library copies" in note and "~1.2 GB redundant" in note, note
    rep = sn.duplicate_report()["cross_item"]
    assert len(rep) == 1 and rep[0]["count"] == 2 and rep[0]["redundant_gb"] == 1.2, rep


def test_prune_deletes_only_stale_rows_of_the_categories_given():
    _reset(_prof("A"), _prof("B"), _prof("M", media_type="movie", tvdb_id=None, tmdb_id=550, title="Fight Club"))
    assert ps._prune_stale_tech_profiles({"A"}, {"anime"}) == 1
    with _sess() as db:
        left = sorted(r.plex_rating_key for r in db.query(MediaTechProfile).all())
    assert left == ["A", "M"], left            # B gone, the movie untouched
    assert ps._prune_stale_tech_profiles(set(), set()) == 0, "no category, no prune"
    with _sess() as db:
        assert db.query(MediaTechProfile).count() == 2


class _Resp:
    def __init__(self, status, items):
        self.status_code, self._items = status, items

    def json(self):
        return {"MediaContainer": {"Metadata": self._items}}


class _Client:
    def __init__(self, script):
        self._script = list(script)

    async def get(self, *a, **k):
        nxt = self._script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def test_fetch_completeness_is_recorded_so_a_broken_walk_never_prunes():
    ok = _Client([_Resp(200, [{"ratingKey": "1"}])])           # one short page = complete
    items = asyncio.run(ps._fetch_section_all(ok, "http://plex", {}, "12", "4"))
    assert len(items) == 1 and ps._LAST_FETCH_COMPLETE[("12", "4")] is True
    broken = _Client([_Resp(200, [{"ratingKey": str(i)} for i in range(400)]), ConnectionError("plex down")])
    items = asyncio.run(ps._fetch_section_all(broken, "http://plex", {}, "12", "4"))
    assert len(items) == 400 and ps._LAST_FETCH_COMPLETE[("12", "4")] is False, "page 2 failed: partial"
    denied = _Client([_Resp(503, [])])
    asyncio.run(ps._fetch_section_all(denied, "http://plex", {}, "12", "4"))
    assert ps._LAST_FETCH_COMPLETE[("12", "4")] is False


def test_the_curator_is_told_it_executes_nothing():
    chat = (_ROOT / "src/routers/chat.py").read_text(encoding="utf-8")
    assert "You execute nothing yourself" in chat
    assert "announce exactly that" not in chat, "the old promise made the curator claim a watchlist add plex.tv had refused"
    from src.services.app_context import DISCUSSION_UI_BLOCK
    assert "executes nothing itself" in DISCUSSION_UI_BLOCK
    assert "trigger_type=\"protection_intent\"" in chat, "the hook's real outcome must reach the bell"


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
