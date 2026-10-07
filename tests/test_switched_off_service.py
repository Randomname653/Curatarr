"""Proposals of a switched-off service are retired, not parked in limbo.

2026-10-07: Lidarr had been switched off the evening before. "Glee Cast", a
proposal from the last Lidarr scan, was approved in chat; _probe_arr found
no Lidarr URL, reported "Lidarr is currently unreachable" and parked it in
limbo, where it would have stayed: the daily scan only supersedes pending
rows of the categories it proposes again. Nothing was deleted, and the
delete path follows the proposal's own service, so a Lidarr id never
reaches Plex.

    python tests/test_switched_off_service.py
"""
import asyncio
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import src.routers.recommendations as recs  # noqa: E402
from src.database.models import Base, DeletionProposal  # noqa: E402

_LIVE = types.SimpleNamespace(
    effective_plex_url="http://plex", effective_plex_token="t",
    RADARR_URL="http://radarr", RADARR_API_KEY="k",
    SONARR_URL="http://sonarr", SONARR_API_KEY="k",
    LIDARR_URL=None, LIDARR_API_KEY=None)


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    for i, (svc, status) in enumerate((("lidarr", "pending"), ("lidarr", "limbo"),
                                       ("lidarr", "deleted"), ("radarr", "pending"),
                                       ("plex", "pending")), 1):
        db.add(DeletionProposal(id=i, user_id=1, title=f"T{i}", service=svc, media_id=str(i),
                                reason="r", confidence=0.8, storage_mb=10, status=status,
                                category="music" if svc in ("lidarr", "plex") else "movie",
                                user_comment="Deleted after in-chat discussion" if i == 2 else None))
    db.commit()
    return db


def _with_settings(fn):
    real = recs.settings
    recs.settings = _LIVE
    try:
        return fn()
    finally:
        recs.settings = real


def test_which_services_are_set_up():
    def run():
        assert recs._service_configured("plex") and recs._service_configured("radarr")
        assert not recs._service_configured("lidarr")
        assert not recs._service_configured("whatever")
    _with_settings(run)


def test_the_scan_retires_open_proposals_of_a_switched_off_arr():
    db = _db()
    n = _with_settings(lambda: recs._retire_switched_off(db, 1))
    db.commit()
    status = {p.id: p.status for p in db.query(DeletionProposal).all()}
    assert n == 2
    assert status == {1: "superseded", 2: "superseded", 3: "deleted",
                      4: "pending", 5: "pending"}, status


def test_a_delete_says_switched_off_and_retires_the_proposal():
    db = _db()

    async def unreachable(service):
        return False

    async def never(*a, **k):
        raise AssertionError("nothing may be deleted")

    real = (recs._probe_arr, recs._delete_one_and_log)
    recs._probe_arr, recs._delete_one_and_log = unreachable, never
    try:
        out = _with_settings(lambda: asyncio.run(recs.approve_deletion(
            2, user=types.SimpleNamespace(id=1), db=db)))
    finally:
        recs._probe_arr, recs._delete_one_and_log = real
    p = db.query(DeletionProposal).get(2)
    assert out["ok"] is False and out["limbo"] is False and out["status"] == "superseded"
    assert "switched off" in out["error"] and "Nothing was deleted" in out["error"]
    assert p.status == "superseded" and p.resolved_at is not None
    assert p.user_comment == "Deleted after in-chat discussion", "the owner's note stays"


def test_a_set_up_but_unreachable_arr_still_parks_in_limbo():
    db = _db()

    async def unreachable(service):
        return False

    real = recs._probe_arr
    recs._probe_arr = unreachable
    try:
        out = _with_settings(lambda: asyncio.run(recs.approve_deletion(
            4, user=types.SimpleNamespace(id=1), db=db)))
    finally:
        recs._probe_arr = real
    assert out["ok"] is False and out["limbo"] is True and out["status"] == "limbo"
    assert "unreachable" in out["error"]


def test_the_wiring():
    src = (_ROOT / "src" / "routers" / "recommendations.py").read_text(encoding="utf-8")
    bulk = src[src.index("async def _bulk_delete_worker") if "async def _bulk_delete_worker" in src
               else src.index("ONE reachability probe per distinct arr"):]
    assert "if not reachable.get(p.service) and not _service_configured(p.service):" in bulk
    assert "_retire_switched_off(dbs, user.id)" in src
    sched = (_ROOT / "src" / "services" / "scheduler.py").read_text(encoding="utf-8")
    assert "_retire_switched_off(db, user_id)" in sched


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
