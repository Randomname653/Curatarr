"""Deletion safety: nothing the owner kept may be deleted, nothing deleted
may be "kept" afterwards, and an unclear outcome is never a dead end.

Covers the race / protection / timeout fixes on the delete path:
  * a Keep, a chat keep (ProtectedMedia) or a second approve that lands
    while a delete is in flight (Lidarr freshness guard: up to ~120 s) wins;
  * two concurrent approvals send exactly ONE DELETE (atomic claim);
  * the chat protection path closes LIMBO proposals too;
  * Plex-music proposals get a real reachability probe;
  * a delete that times out is verified by GET instead of marked "error";
  * Keep on a closed (deleted) proposal is refused;
  * the protection classifier only acts on titles the USER meant.

In-memory SQLite shared by several sessions (StaticPool) so "another
request" really is another session; arr/Plex HTTP via httpx.MockTransport.

    python tests/test_deletion_safety.py
"""
import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.routers.recommendations as recs
import src.services.episodic_memory as em
from src.config import settings
from src.database.models import (
    Base, CuratorResolutionLog, DeletionProposal, ProtectedMedia,
)

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
    finally:
        s.close()


_real_probe = recs._probe_arr
_real_exec = recs._execute_arr_delete
recs.get_db_session = fake_db_session
em.get_db_session = fake_db_session
recs._latest_curator_stance_for_proposal = (
    lambda db, uid, pid, fallback_pitch=None: (fallback_pitch or "s", "CONFIRMED"))

_next_id = [100]


def seed(title, service="radarr", status="pending", tmdb_id=None, category="movie"):
    _next_id[0] += 1
    with fake_db_session() as s:
        s.add(DeletionProposal(id=_next_id[0], user_id=1, title=title,
                               service=service, media_id=str(_next_id[0]),
                               reason="pitch", confidence=0.8, storage_mb=1024,
                               status=status, category=category, tmdb_id=tmdb_id))
        s.commit()
    return _next_id[0]


def status_of(pid):
    with fake_db_session() as s:
        return s.get(DeletionProposal, pid).status


exec_calls = []


async def fake_exec(p):
    exec_calls.append(p.title)
    return True


recs._execute_arr_delete = fake_exec

# ── 1. Keep / protection landing DURING the Lidarr freshness guard ───────────

pid = seed("Guard Keep Band", service="lidarr", category="music")


async def guard_then_keep(p):
    # the owner clicks Keep (another request / session) while we wait
    with fake_db_session() as s:
        s.query(DeletionProposal).filter_by(id=p.id).update({"status": "rejected"})
        s.commit()
    return None


recs._lidarr_freshness_guard = guard_then_keep
db = Session()
p = db.get(DeletionProposal, pid)
ok = asyncio.run(recs._delete_one_and_log(db, 1, p))
db.commit()
check("Keep during freshness guard -> NOT deleted, arr never called",
      ok is False and exec_calls == [] and status_of(pid) == "rejected")
db.close()

pid = seed("Guard Protect Band", service="lidarr", category="music")


async def guard_then_protect(p):
    # a chat "keep X" writes ProtectedMedia while we wait (it used to only
    # reject status=="pending"; here the row is still pending in the DB)
    with fake_db_session() as s:
        s.add(ProtectedMedia(user_id=1, identifier="Guard Protect Band",
                             title="Guard Protect Band", source="discussion"))
        s.commit()
    return None


recs._lidarr_freshness_guard = guard_then_protect
db = Session()
p = db.get(DeletionProposal, pid)
ok = asyncio.run(recs._delete_one_and_log(db, 1, p))
db.commit()
check("protection added during guard -> NOT deleted, proposal closed as rejected",
      ok is False and exec_calls == [] and status_of(pid) == "rejected")
db.close()

# judge protections are keyed by TMDB id (per category)
pid = seed("Judge Kept", tmdb_id=4242)
with fake_db_session() as s:
    s.add(ProtectedMedia(user_id=1, identifier="4242", category="movie",
                         source="judge", verdict="HARD_KEEP"))
    s.commit()
db = Session()
ok = asyncio.run(recs._delete_one_and_log(db, 1, db.get(DeletionProposal, pid)))
db.commit()
check("tmdb-id protection (judge) blocks the delete",
      ok is False and exec_calls == [] and status_of(pid) == "rejected")
db.close()

pid = seed("Other Type Same Id", tmdb_id=4242, category="show", service="sonarr")
db = Session()
ok = asyncio.run(recs._delete_one_and_log(db, 1, db.get(DeletionProposal, pid)))
db.commit()
check("tmdb-id protection does not leak across categories (movie 4242 != tv 4242)",
      ok is True and status_of(pid) == "deleted")
db.close()
exec_calls.clear()

# ── 1b. two concurrent approvals -> exactly one DELETE ───────────────────────

pid = seed("Double Click")
slow_calls = []


async def slow_exec(p):
    slow_calls.append(p.title)
    await asyncio.sleep(0.05)
    return True


recs._execute_arr_delete = slow_exec


async def _two_approvals():
    s1, s2 = Session(), Session()
    try:
        return await asyncio.gather(
            recs._delete_one_and_log(s1, 1, s1.get(DeletionProposal, pid)),
            recs._delete_one_and_log(s2, 1, s2.get(DeletionProposal, pid)),
        ), s1, s2
    except Exception:
        s1.close(); s2.close()
        raise


(results, s1, s2) = asyncio.run(_two_approvals())
s1.commit(); s2.commit(); s1.close(); s2.close()
check("concurrent approvals: exactly one DELETE sent", slow_calls == ["Double Click"])
check("...one wins, the loser reports failure without touching status",
      sorted(results) == [False, True] and status_of(pid) == "deleted")
with fake_db_session() as s:
    n_logs = s.query(CuratorResolutionLog).filter_by(title="Double Click").count()
check("...and only one resolution-log row", n_logs == 1)
recs._execute_arr_delete = fake_exec

# approve endpoint on an already-claimed / closed proposal -> 409, no probe
probed = []


async def fake_probe(svc):
    probed.append(svc)
    return True


recs._probe_arr = fake_probe
pid = seed("Mid Delete", status="deleting")


class _U:
    id = 1


db = Session()
try:
    asyncio.run(recs.approve_deletion(pid, user=_U(), db=db))
    code = None
except HTTPException as e:
    code = e.status_code
check("approve on a row already being deleted -> 409, arr not probed",
      code == 409 and probed == [])
db.close()

# approve endpoint on a protected pending row -> not deleted, clear error
pid = seed("Endpoint Protected")
with fake_db_session() as s:
    s.add(ProtectedMedia(user_id=1, identifier="endpoint protected",
                         title="Endpoint Protected", source="manual"))
    s.commit()
db = Session()
r = asyncio.run(recs.approve_deletion(pid, user=_U(), db=db))
check("approve on a protected title -> ok False + error, nothing deleted",
      r["ok"] is False and r["status"] == "rejected" and "error" in r
      and exec_calls == [])
db.close()

# ── 2. chat protection closes LIMBO proposals too ────────────────────────────

pid_limbo = seed("Limbo Show", status="limbo")
pid_pend = seed("Limbo Show")
asyncio.run(em.handle_protection_intent(
    1, "ACTION: PROTECT_MEDIA | TITLE: Limbo Show | REASON: keep"))
check("protection rejects the limbo proposal (was still approvable)",
      status_of(pid_limbo) == "rejected")
check("protection still rejects the pending proposal",
      status_of(pid_pend) == "rejected")

# ── HTTP-level fixes (probe / delete timeouts) via MockTransport ─────────────

_RealClient = httpx.AsyncClient


@contextmanager
def mock_http(handler):
    def factory(*a, **kw):
        kw.pop("transport", None)
        return _RealClient(*a, transport=httpx.MockTransport(handler), **kw)
    httpx.AsyncClient = factory
    try:
        yield
    finally:
        httpx.AsyncClient = _RealClient


# ── 3. Plex probe ────────────────────────────────────────────────────────────

recs_fresh = recs                     # from here on: the REAL http paths
recs._probe_arr = _real_probe
recs._execute_arr_delete = _real_exec
recs_fresh._DELETE_VERIFY_INTERVAL_S = 0
recs_fresh._DELETE_VERIFY_POLLS = 3

settings.PLEX_URL = None
settings.PLEX_TOKEN = None
settings.PLEX_BASE_URL = "http://plex.test:32400"
settings.PLEX_AUTH_TOKEN = "tok"
seen = []


def plex_ok(req):
    seen.append((req.url.path, req.headers.get("X-Plex-Token")))
    return httpx.Response(200, json={"MediaContainer": {}})


with mock_http(plex_ok):
    up = asyncio.run(recs_fresh._probe_arr("plex"))
check("Plex probe: reachable Plex -> True (was always False -> limbo)",
      up is True and seen == [("/identity", "tok")])


def plex_down(req):
    raise httpx.ConnectError("refused", request=req)


with mock_http(plex_down):
    check("Plex probe: unreachable -> False", asyncio.run(recs_fresh._probe_arr("plex")) is False)
settings.PLEX_AUTH_TOKEN = ""
with mock_http(plex_ok):
    check("Plex probe: no token -> False", asyncio.run(recs_fresh._probe_arr("plex")) is False)

# ── 4. delete timeouts ───────────────────────────────────────────────────────

settings.RADARR_URL = "http://radarr.test"
settings.RADARR_API_KEY = "k"


class _P:
    service = "radarr"
    media_id = "7"
    title = "Big Movie"


check("delete read timeout is generous (the old flat 15 s marked NAS deletes error)",
      recs_fresh._DELETE_TIMEOUT.read >= 120)

gets = []


def timeout_then_gone(req):
    if req.method == "DELETE":
        raise httpx.ReadTimeout("slow NAS", request=req)
    gets.append(req.url.path)
    return httpx.Response(404 if len(gets) >= 2 else 200)


with mock_http(timeout_then_gone):
    res = asyncio.run(recs_fresh._execute_arr_delete(_P()))
check("timed-out DELETE + item later 404s -> counted as deleted",
      res is True and gets == ["/api/v3/movie/7", "/api/v3/movie/7"])


def timeout_still_there(req):
    if req.method == "DELETE":
        raise httpx.ReadTimeout("slow NAS", request=req)
    return httpx.Response(200, json={"id": 7})


with mock_http(timeout_still_there):
    res = asyncio.run(recs_fresh._execute_arr_delete(_P()))
check("timed-out DELETE + item still present -> unknown (None), not False",
      res is None)


def refused(req):
    return httpx.Response(500, text="boom")


with mock_http(refused):
    check("arr answers 500 -> False (real failure)",
          asyncio.run(recs_fresh._execute_arr_delete(_P())) is False)


def ok204(req):
    return httpx.Response(200)


with mock_http(ok204):
    check("arr answers 200 -> True", asyncio.run(recs_fresh._execute_arr_delete(_P())) is True)

# end to end: unknown outcome parks in limbo (listed + retryable), not error
pid = seed("Unknown Outcome")
with mock_http(timeout_still_there):
    db = Session()
    ok = asyncio.run(recs_fresh._delete_one_and_log(db, 1, db.get(DeletionProposal, pid)))
    db.commit()
    db.close()
check("unknown delete outcome -> limbo (retryable), not a dead-end error",
      ok is False and status_of(pid) == "limbo")

# ── 5. Keep on a closed proposal ─────────────────────────────────────────────

pid = seed("Already Gone", status="deleted")
db = Session()
try:
    asyncio.run(recs_fresh.reject_deletion(pid, user=_U(), db=db))
    code = None
except HTTPException as e:
    code = e.status_code
db.close()
with fake_db_session() as s:
    kept_logs = s.query(CuratorResolutionLog).filter_by(title="Already Gone").count()
check("Keep on a deleted proposal -> 409, status stays deleted, no false log",
      code == 409 and status_of(pid) == "deleted" and kept_logs == 0)

pid = seed("Keep Me", status="limbo")
db = Session()
r = asyncio.run(recs_fresh.reject_deletion(pid, user=_U(), db=db))
db.close()
with fake_db_session() as s:
    kept_logs = s.query(CuratorResolutionLog).filter_by(title="Keep Me", outcome="kept").count()
check("Keep on an open (limbo) proposal still works + logs",
      r.get("ok") and status_of(pid) == "rejected" and kept_logs == 1)

db = Session()
r = asyncio.run(recs_fresh.reject_deletion(pid, user=_U(), db=db))
db.close()
check("Keep twice stays idempotent", r.get("already_rejected") is True)

# stale card comment on a deleted proposal must not trigger the keep analysis
analyzed = []


async def fake_analyze(*a, **kw):
    analyzed.append(a)
    return True


em.analyze_deletion_comment = fake_analyze
pid = seed("Stale Comment", status="deleted")
db = Session()
r = asyncio.run(recs_fresh.update_comment(pid, "Keeping: love it", user=_U(), db=db))
db.close()
check("Keeping-comment on a deleted proposal is not analysed (no false keep)",
      analyzed == [] and r["is_kept"] is False)

# ── 7. protection classifier grounding ───────────────────────────────────────

g = em._ground_protection_actions
L = "ACTION: PROTECT_MEDIA | TITLE: {} | REASON: r | RESOLUTION: override"

out = g(L.format("Evil Title") + "\n" + L.format("Some Show"), "keep it", "Some Show")
check("injected title (not anchor, not in user msg) is dropped",
      "Evil Title" not in out and "TITLE: Some Show" in out)
out = g(L.format("school live"), "sounds good, I'll watch it", "School-Live!")
check("anchor match is normalised and rewritten to the anchor's spelling",
      "TITLE: School-Live!" in out)
out = g(L.format("Breaking Bad"), "please keep breaking bad", None)
check("free-chat: title literally in the user's message is kept",
      "TITLE: Breaking Bad" in out)
out = g(L.format("Breaking"), "keep breakingbad", None)
check("partial-word match is rejected", "PROTECT_MEDIA" not in out)
many = "\n".join(L.format(t) for t in ["A1", "B2", "C3", "D4", "E5"])
out = g(many, "keep a1 b2 c3 d4 e5", None)
check("per-turn cap on protection actions",
      out.count("ACTION: PROTECT_MEDIA") == em._MAX_PROTECT_ACTIONS_PER_TURN)
check("NO_ACTION passes through untouched", g("NO_ACTION", "hi", None) == "NO_ACTION")

# end to end through detect_and_handle_protection with a planted title
pid_evil = seed("Planted Title")
pid_anchor = seed("Anchor Show")


async def fake_classifier(prompt):
    return (L.format("Anchor Show") + " | WATCHLIST: -\n"
            + L.format("Planted Title") + " | WATCHLIST: yes")


em._call_protection_classifier = fake_classifier
em._fetch_recent_thread_turns = lambda uid, tid: ""
res = asyncio.run(em.detect_and_handle_protection(
    1, "ok fine, keep it", "It stays.", anchor_title="Anchor Show",
    anchor_category="show", thread_id="deletion_proposal:1"))
with fake_db_session() as s:
    planted_prot = s.query(ProtectedMedia).filter_by(identifier="Planted Title").count()
check("detector: anchor keep applied",
      status_of(pid_anchor) == "rejected" and res and "Anchor Show" in res)
check("detector: planted title NOT protected, its proposal untouched",
      planted_prot == 0 and status_of(pid_evil) == "pending"
      and "Planted Title" not in (res or ""))

# ── wiring: boot reset for claims interrupted by a crash ─────────────────────

mn = (Path(__file__).resolve().parents[1] / "src/main.py").read_text(encoding="utf-8")
check("boot parks interrupted 'deleting' claims in limbo",
      '_DP.status == "deleting"' in mn and '{"status": "limbo"}' in mn)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
