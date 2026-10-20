"""Daily reconcile of deletions with an unknown outcome (2026-10 audit).

A limbo row used to leave limbo only by a Retry click, so a delete that had
gone through stayed there for good — no resolution log, no storage credit.
And a row stuck in "deleting" (status write failed three times) stayed
hidden until the next boot. reconcile_limbo runs the real delete paths
(Radarr and Plex) against a mock transport here: an item the service no
longer has is recorded exactly as a Retry would, one still there is never
re-deleted.

    python tests/test_limbo_reconcile.py
"""
import asyncio
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.database.connection as conn_mod
import src.routers.recommendations as recs
import src.services.episodic_memory as em
import src.services.limbo_reconcile as lr
import src.services.lyrics as lyrics
from src.config import settings
from src.database.models import (Base, CuratorResolutionLog, DeletionProposal,
                                 ProactiveMessage)

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)


@contextmanager
def fake_db_session():
    s = Session()
    try:
        yield s
        s.commit()
    finally:
        s.close()


recs.get_db_session = fake_db_session
conn_mod.get_db_session = fake_db_session
settings.RADARR_URL, settings.RADARR_API_KEY = "http://radarr.test", "rk"
settings.PLEX_URL, settings.PLEX_TOKEN, settings.PLEX_AUTH_TOKEN = "http://plex.test", None, "pt"


async def fake_probe(service):
    return service in up


def fake_stance(db, user_id, pid, fallback_pitch=None):
    return (fallback_pitch or "stance", "CONFIRMED")


async def fake_analyze(user_id, title, comment, media_category="show"):
    return False


recs._probe_arr = fake_probe
recs._latest_curator_stance_for_proposal = fake_stance
em.analyze_deletion_comment = fake_analyze
lyrics.mark_artist_gone = lambda key: None

up = {"radarr", "plex"}
on_server = {"radarr": {"2"}, "plex": set()}   # what each service still has
requests = []


def handler(request: httpx.Request) -> httpx.Response:
    svc = "plex" if request.url.host == "plex.test" else "radarr"
    item = request.url.path.rstrip("/").rsplit("/", 1)[-1]
    requests.append((request.method, svc, item))
    if request.method == "DELETE" and item in on_server[svc]:
        on_server[svc].discard(item)
        return httpx.Response(200)
    return httpx.Response(200 if item in on_server[svc] else 404)


_RealClient = httpx.AsyncClient


def mock_client(*a, **kw):
    kw.pop("verify", None)
    return _RealClient(transport=httpx.MockTransport(handler), timeout=kw.get("timeout"))


httpx.AsyncClient = mock_client

with fake_db_session() as db:
    for pid, title, svc, mid, status in [
        (1, "Gone Movie", "radarr", 1, "limbo"),
        (2, "Kept Movie", "radarr", 2, "limbo"),
        (3, "Gone Artist", "plex", 3, "limbo"),
        (4, "Hidden Claim", "radarr", 4, "deleting"),
        (5, "Live Claim", "radarr", 5, "deleting"),
        (6, "Down Show", "sonarr", 6, "limbo"),
    ]:
        db.add(DeletionProposal(id=pid, user_id=1, title=title, service=svc,
                                media_id=str(mid), reason="pitch", confidence=0.8,
                                storage_mb=1024, status=status,
                                category="music" if svc == "plex" else "movie"))

recs._DELETE_CLAIMS.clear()
recs._DELETE_CLAIMS[5] = time.monotonic()      # a delete of this process is running
lr._SEEN_DELETING.clear()

stats = asyncio.run(lr.reconcile_limbo())


def status(pid):
    with fake_db_session() as db:
        return db.get(DeletionProposal, pid).status


def logged(title):
    with fake_db_session() as db:
        return db.query(CuratorResolutionLog).filter_by(title=title).count()


check("a limbo movie Radarr no longer has is recorded as deleted",
      status(1) == "deleted" and logged("Gone Movie") == 1)
check("a limbo Plex artist that answers 404 is recorded as deleted",
      status(3) == "deleted" and logged("Gone Artist") == 1)
check("an item still on the server stays in limbo",
      status(2) == "limbo" and logged("Kept Movie") == 0)
check("... and is NEVER sent a DELETE by the reconciler",
      ("DELETE", "radarr", "2") not in requests and "2" in on_server["radarr"])
check("an unreachable service's row is left alone", status(6) == "limbo")
check("a 'deleting' row is not adopted on the pass that first sees it "
      "(its delete may not have noted the claim yet)",
      status(4) == "deleting" and stats["adopted"] == 0)
check("a 'deleting' row a running delete holds is not touched", status(5) == "deleting")
check("stats count every outcome",
      stats["resolved"] == 2 and stats["still_present"] == 1
      and stats["unanswered"] == 1)
check("the claim map is cleared once outcomes are committed",
      not ({1, 3} & set(recs._DELETE_CLAIMS)))

with fake_db_session() as db:
    bubbles = [m.message for m in
               db.query(ProactiveMessage).filter_by(trigger_type="limbo_deletions")]
check("the owner hears about the deletion still waiting",
      len(bubbles) == 1 and "Kept Movie" in bubbles[0])
stats = asyncio.run(lr.reconcile_limbo())
with fake_db_session() as db:
    n = db.query(ProactiveMessage).filter_by(trigger_type="limbo_deletions").count()
check("... at most once a week", n == 1)
check("next pass: the 'deleting' row no delete holds is parked in limbo and "
      "then recorded, being gone from Radarr",
      stats["adopted"] == 1 and status(4) == "deleted" and logged("Hidden Claim") == 1)
check("... the held one still is not", status(5) == "deleting")

# A stale claim (older than any real delete) is adopted too.
recs._DELETE_CLAIMS[5] = time.monotonic() - 2 * lr._CLAIM_STALE_S
asyncio.run(lr.reconcile_limbo())
check("a claim older than any delete can run is adopted", status(5) != "deleting")

httpx.AsyncClient = _RealClient

from src.services.data_custodian import _registry  # noqa: E402

t = next((t for t in _registry() if t.job_id == "limbo_reconcile"), None)
check("registered daily as a background wrapper task",
      t is not None and t.cadence_h == 24.0 and t.takes_task and not t.needs_llm)
src = (Path(__file__).resolve().parents[1] / "src/routers/recommendations.py").read_text(encoding="utf-8")
check("the claim is recorded right after the conditional 'deleting' UPDATE",
      src.index('_transition_if_open, "deleting")') < src.index("_DELETE_CLAIMS[p.id] = "))
check("_commit_outcome releases the claim even when its writes fail",
      "finally:\n        _DELETE_CLAIMS.pop(p.id, None)" in src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
