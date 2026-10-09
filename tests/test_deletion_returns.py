"""Deleted items that came back, and the SoulSync ban of a deleted artist.

2026-10-09, read-only check of 904 deletions since May: by TMDb/TVDb id
nothing was back (Radarr and Sonarr deletes carry an import-list exclusion
since Pass 58). One title match was real ("The Marvels", deleted 05-14, in
Radarr again 05-15; its proposal had no TMDb id); three were other films of
the same name, in the library long before the delete (Skin Deep, The
Enforcer, Ghost in the Shell). Hence: an id match counts whenever the item
is there, a title match only when the title is unique and the item was
added after the delete.

    python tests/test_deletion_returns.py
"""
import asyncio
import json
import pathlib
import sys
import types
from datetime import datetime

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import src.routers.recommendations as recs  # noqa: E402
import src.services.deletion_returns as dr  # noqa: E402
from src.database.models import Base, DeletionProposal  # noqa: E402

MAY14 = datetime(2026, 5, 14, 12, 0)


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _p(db, i, title, cat, service, **kw):
    p = DeletionProposal(id=i, user_id=kw.pop("user_id", 1), title=title, category=cat,
                         service=service, media_id=str(i), reason="r", confidence=0.6,
                         storage_mb=100, status=kw.pop("status", "deleted"),
                         resolved_at=kw.pop("resolved_at", MAY14), **kw)
    db.add(p)
    return p


def _movie(i, title, tmdb, added):
    return {"id": i, "title": title, "tmdbId": tmdb, "added": added,
            "statistics": {"sizeOnDisk": 2 * 1048576 * 1024}}


def _radarr(*items):
    return {"radarr": dr._Index("radarr", list(items)), "sonarr": None, "lidarr": None}


def test_an_id_match_counts_and_says_whether_it_ever_left():
    db = _db()
    back = _p(db, 1, "Dune", "movie", "radarr", tmdb_id=438631)
    stayed = _p(db, 2, "Heat", "movie", "radarr", tmdb_id=949)
    new = dr.apply_matches([back, stayed], _radarr(
        _movie(501, "Dune", 438631, "2026-05-20T08:00:00Z"),
        _movie(77, "Heat", 949, "2024-01-01T00:00:00Z")))
    assert new == ["Dune", "Heat"], new
    a, b = json.loads(back.returned_info), json.loads(stayed.returned_info)
    assert a["kind"] == "returned" and a["match"] == "id" and a["media_id"] == "501", a
    assert a["state"] == "open" and a["size_mb"] == 2048.0 and a["service"] == "radarr"
    assert back.returned_at == datetime(2026, 5, 20, 8, 0)
    assert b["kind"] == "still_there", b


def test_a_title_match_needs_a_unique_title_added_after_the_delete():
    db = _db()
    marvels = _p(db, 1, "The Marvels", "movie", "radarr")              # legacy: no tmdb id
    skin = _p(db, 2, "Skin Deep", "movie", "radarr")
    twins = _p(db, 3, "Ghost in the Shell", "movie", "radarr")
    other = _p(db, 4, "Solaris", "movie", "radarr", tmdb_id=593)
    idx = _radarr(
        _movie(10, "The Marvels", 609681, "2026-05-15T09:00:00Z"),
        _movie(11, "Skin Deep", 1, "2025-05-11T00:00:00Z"),            # another film, older
        _movie(12, "Ghost in the Shell", 9323, "2026-06-10T00:00:00Z"),
        _movie(13, "Ghost in the Shell", 315837, "2026-06-11T00:00:00Z"),
        _movie(14, "Solaris", 2103, "2026-06-01T00:00:00Z"))           # the 2002 one, not 1972
    new = dr.apply_matches([marvels, skin, twins, other], idx)
    assert new == ["The Marvels"], new
    assert json.loads(marvels.returned_info)["match"] == "title"
    assert skin.returned_info is None and twins.returned_info is None and other.returned_info is None


def test_kept_and_deleted_again_are_never_reopened_and_a_gone_return_clears():
    db = _db()
    kept = _p(db, 1, "Dune", "movie", "radarr", tmdb_id=438631,
              returned_info=json.dumps({"state": "kept"}))
    again = _p(db, 2, "Heat", "movie", "radarr", tmdb_id=949,
               returned_info=json.dumps({"state": "deleted_again", "new_proposal_id": 9}))
    gone = _p(db, 3, "Alien", "movie", "radarr", tmdb_id=348,
              returned_at=datetime(2026, 6, 1), returned_info=json.dumps({"state": "open"}))
    new = dr.apply_matches([kept, again, gone], _radarr(
        _movie(1, "Dune", 438631, "2026-06-01T00:00:00Z"),
        _movie(2, "Heat", 949, "2026-06-01T00:00:00Z")))
    assert new == []
    assert json.loads(kept.returned_info)["state"] == "kept"
    assert json.loads(again.returned_info)["state"] == "deleted_again"
    assert gone.returned_info is None and gone.returned_at is None, "left the library again"


def test_an_unreadable_library_leaves_its_returns_alone():
    db = _db()
    p = _p(db, 1, "Fringe", "show", "sonarr", tvdb_id=82066,
           returned_at=datetime(2026, 6, 1), returned_info=json.dumps({"state": "open"}))
    assert dr.apply_matches([p], {"sonarr": None}) == []
    assert json.loads(p.returned_info)["state"] == "open"


def test_shows_and_anime_match_on_tvdb_music_on_the_plex_index():
    db = _db()
    show = _p(db, 1, "Fringe", "anime", "sonarr", tvdb_id=82066)
    art = _p(db, 2, "Bishop Briggs", "music", "plex",
             resolved_at=datetime(2026, 10, 7, 19, 7))
    mbid = _p(db, 3, "Nate Ruess", "music", "lidarr",
              soulsync_ban=json.dumps({"ok": True, "mbid": "m-1"}))
    idx = {
        "sonarr": dr._Index("sonarr", [{"id": 5, "title": "Fringe", "tvdbId": 82066,
                                        "added": "2026-07-01T00:00:00Z"}]),
        "lidarr": dr._Index("lidarr", [
            {"id": "915999", "artistName": "Bishop Briggs", "foreignArtistId": None,
             "added": "2026-10-08T03:00:00", "_source": "plex",
             "statistics": {"sizeOnDisk": 400 * 1048576}},
            {"id": "77", "artistName": "Nate Ruess (Live)", "foreignArtistId": "m-1",
             "added": "2026-01-01T00:00:00", "_source": "plex"}]),
    }
    new = dr.apply_matches([show, art, mbid], idx)
    assert sorted(new) == ["Bishop Briggs", "Fringe", "Nate Ruess"], new
    b = json.loads(art.returned_info)
    assert b["service"] == "plex" and b["media_id"] == "915999" and b["match"] == "title", b
    m = json.loads(mbid.returned_info)
    assert m["match"] == "id" and m["kind"] == "still_there", m


def test_open_returns_lists_only_the_owners_open_ones():
    db = _db()
    _p(db, 1, "Dune", "movie", "radarr", returned_at=datetime(2026, 6, 2),
       returned_info=json.dumps({"state": "open", "kind": "returned", "service": "radarr",
                                 "match": "id", "size_mb": 2048}))
    _p(db, 2, "Heat", "movie", "radarr", returned_at=datetime(2026, 6, 3),
       returned_info=json.dumps({"state": "kept"}))
    _p(db, 3, "Alien", "movie", "radarr", user_id=2, returned_at=datetime(2026, 6, 4),
       returned_info=json.dumps({"state": "open"}))
    _p(db, 4, "Fringe", "show", "sonarr", returned_at=datetime(2026, 6, 5),
       returned_info=json.dumps({"state": "open", "kind": "still_there"}))
    db.commit()
    out = dr.open_returns(db, 1)
    assert [o["title"] for o in out] == ["Fringe", "Dune"], out
    assert out[1]["size_gb"] == 2.0 and out[1]["deleted_at"].startswith("2026-05-14")
    assert [o["title"] for o in dr.open_returns(db, 1, "movie")] == ["Dune"]


def _patch_ss(writes=True, info=None, result=None):
    calls = []

    async def artist_info(name):
        calls.append(("info", name))
        return info

    async def block_artist(name, mbid=None, *, client=None):
        calls.append(("ban", name, mbid))
        return result or {"ok": True, "id": 41, "error": None}
    real = (dr_ss.writes_allowed, dr_ss.artist_info, dr_ss.block_artist)
    dr_ss.writes_allowed = lambda: writes
    dr_ss.artist_info, dr_ss.block_artist = artist_info, block_artist
    return calls, real


def _unpatch(real):
    dr_ss.writes_allowed, dr_ss.artist_info, dr_ss.block_artist = real


import src.services.soulsync_client as dr_ss  # noqa: E402


def test_a_deleted_artist_is_banned_with_its_musicbrainz_id():
    db = _db()
    p = _p(db, 1, "Bishop Briggs", "music", "plex")
    calls, real = _patch_ss(info={"musicbrainz_id": "mb-9"})
    try:
        asyncio.run(dr.ban_deleted_artist(p))
    finally:
        _unpatch(real)
    assert calls == [("info", "Bishop Briggs"), ("ban", "Bishop Briggs", "mb-9")], calls
    ban = json.loads(p.soulsync_ban)
    assert ban["ok"] and ban["id"] == 41 and ban["mbid"] == "mb-9" and ban["error"] is None
    assert dr.ban_held(p) and dr.ban_warning(p) is None


def test_a_failed_ban_is_recorded_and_worded_for_the_owner():
    db = _db()
    p = _p(db, 1, "Mwk", "music", "lidarr")
    calls, real = _patch_ss(result={"ok": False, "id": None, "error": "SoulSync refused the ban (HTTP 401)"})
    try:
        asyncio.run(dr.ban_deleted_artist(p))
    finally:
        _unpatch(real)
    assert not dr.ban_held(p)
    w = dr.ban_warning(p)
    assert "HTTP 401" in w and "Mwk" in w and "SoulSync" in w, w


def test_no_ban_for_films_or_without_soulsync_writes():
    db = _db()
    film = _p(db, 1, "Dune", "movie", "radarr")
    art = _p(db, 2, "Mwk", "music", "lidarr")
    calls, real = _patch_ss()
    try:
        asyncio.run(dr.ban_deleted_artist(film))
        dr_ss.writes_allowed = lambda: False
        asyncio.run(dr.ban_deleted_artist(art))
    finally:
        _unpatch(real)
    assert calls == [] and film.soulsync_ban is None and art.soulsync_ban is None


def test_retry_bans_sets_only_the_bans_that_never_held():
    db = _db()
    _p(db, 1, "Mwk", "music", "lidarr")
    _p(db, 2, "Third Day", "music", "lidarr",
       soulsync_ban=json.dumps({"ok": False, "error": "down"}))
    _p(db, 3, "Nate Ruess", "music", "lidarr", soulsync_ban=json.dumps({"ok": True, "id": 3}))
    _p(db, 4, "Dune", "movie", "radarr")
    _p(db, 5, "Hozier", "music", "plex", status="pending")
    db.commit()
    calls, real = _patch_ss()
    try:
        out = asyncio.run(dr.retry_bans(db))
    finally:
        _unpatch(real)
    assert out == {"tried": 2, "held": 2, "failed": []}, out
    assert [c[1] for c in calls if c[0] == "ban"] == ["Mwk", "Third Day"], calls


def test_the_daily_run_only_reads():
    src = (_ROOT / "src" / "services" / "deletion_returns.py").read_text(encoding="utf-8")
    daily = src[src.index("async def run_daily"):src.index("# ── The Deletions view")]
    assert "retry_bans" not in daily and "ban_deleted_artist" not in daily
    import src.services.data_custodian as dc
    t = next(t for t in dc._registry() if t.job_id == "deletion_returns")
    assert t.cadence_h == 24.0 and not t.needs_llm and t.takes_task


def test_keep_and_delete_again_endpoints():
    db = _db()
    info = {"state": "open", "kind": "returned", "service": "radarr", "media_id": "501",
            "title": "Dune", "size_mb": 2048, "match": "id"}
    _p(db, 1, "Dune", "movie", "radarr", tmdb_id=438631, returned_at=datetime(2026, 6, 2),
       returned_info=json.dumps(info))
    _p(db, 2, "Heat", "movie", "radarr", returned_info=json.dumps({**info, "title": "Heat"}))
    db.commit()
    user = types.SimpleNamespace(id=1)
    assert asyncio.run(recs.keep_returned(2, user=user, db=db)) == {"ok": True}
    assert json.loads(db.get(DeletionProposal, 2).returned_info)["state"] == "kept"

    seen = {}

    async def fake_approve(proposal_id, user=None, db=None):
        seen["id"] = proposal_id
        return {"ok": True, "limbo": False, "status": "deleted"}
    real = recs.approve_deletion
    recs.approve_deletion = fake_approve
    try:
        out = asyncio.run(recs.delete_returned(1, user=user, db=db))
    finally:
        recs.approve_deletion = real
    assert out["ok"]
    new = db.get(DeletionProposal, seen["id"])
    assert (new.media_id, new.service, new.title, new.status, new.tmdb_id) == \
        ("501", "radarr", "Dune", "pending", 438631)
    assert "deleted on 2026-05-14" in new.reason and new.storage_mb == 2048
    old = json.loads(db.get(DeletionProposal, 1).returned_info)
    assert old["state"] == "deleted_again" and old["new_proposal_id"] == new.id

    from fastapi import HTTPException
    try:
        asyncio.run(recs.keep_returned(1, user=user, db=db))
        raise AssertionError("a closed return is not open any more")
    except HTTPException as e:
        assert e.status_code == 404


def test_a_successful_music_delete_bans_and_warns_when_the_ban_failed():
    db = _db()
    _p(db, 1, "Bishop Briggs", "music", "plex", status="pending", resolved_at=None)
    db.commit()

    async def reachable(service):
        return True

    async def deleted(p):
        return True

    async def failed_ban(p):
        p.soulsync_ban = json.dumps({"ok": False, "error": "SoulSync could not be reached (ConnectError)"})
    real = (recs._probe_arr, recs._execute_arr_delete, dr.ban_deleted_artist,
            recs._latest_curator_stance_for_proposal)
    recs._probe_arr, recs._execute_arr_delete, dr.ban_deleted_artist = reachable, deleted, failed_ban
    recs._latest_curator_stance_for_proposal = lambda *a, **k: ("pitch", "CONFIRMED")
    try:
        out = asyncio.run(recs.approve_deletion(1, user=types.SimpleNamespace(id=1), db=db))
    finally:
        (recs._probe_arr, recs._execute_arr_delete, dr.ban_deleted_artist,
         recs._latest_curator_stance_for_proposal) = real
    assert out["ok"] and out["status"] == "deleted", out
    assert "could not be reached" in out["warning"] and "Bishop Briggs" in out["warning"], out


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
