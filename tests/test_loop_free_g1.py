"""Enrichment, auth and the small routers keep their database work off the
event loop (src/database/loop_guard.py in raise mode).

  * _write_enrichment_db: status row, arr mirror, series fan-out, cache
  * _run_enrichment (nothing to do): settings/flags written, lock released
  * _audit_enrichments: a zero-rating profile becomes a finding, its cache
    row is purged and its status rows are flipped back
  * enrichment_status: per-category counts and the running flag, no request
    session any more
  * poll_plex_pin: first login creates the admin, the next one stamps it,
    a deactivated account still gets 403

Runs against a temp file database bound to the app's session factory and a
temp metadata cache; plex.tv and the arrs are stubbed.

    python tests/test_loop_free_g1.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import BackgroundTasks, HTTPException  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from src.config import settings  # noqa: E402

if not settings.effective_jwt_secret:
    from pydantic import SecretStr
    settings.JWT_SECRET = SecretStr("test-only-secret-never-shipped-32-bytes-min-per-rfc7518")

import src.database.connection as conn_mod  # noqa: E402
from src.database import loop_guard  # noqa: E402
from src.database.connection import get_db_session  # noqa: E402
from src.database.models import (ArrEnrichmentStatus, Base, EnrichmentFinding,  # noqa: E402
                                 EnrichmentStatus, User, WatchHistoryEntry)
from src.services import app_state  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


tmp = Path(tempfile.mkdtemp())
eng = create_engine(f"sqlite:///{(tmp / 'g1.db').as_posix()}",
                    connect_args={"check_same_thread": False})
Base.metadata.create_all(eng)
conn_mod.SessionLocal.configure(bind=eng)
conn_mod.engine = eng
loop_guard.install(eng)
loop_guard.serving(True)
settings.ENRICHMENT_CACHE = tmp / "cache.db"

import src.routers.auth as auth  # noqa: E402
import src.routers.enrichment as en  # noqa: E402
from src.cache.metadata_cache import MetadataCache  # noqa: E402

MINE = ("src/routers/enrichment.py", "src/routers/auth.py", "src/routers/history.py",
        "src/routers/libraries.py", "src/routers/stats.py")


def run_guarded(coro_fn):
    """Run one coroutine with the guard raising; returns (result, error, my_sites)."""
    loop_guard.reset()
    os.environ["CURATARR_LOOP_GUARD"] = "raise"
    res = err = None
    try:
        res = asyncio.run(coro_fn())
    except Exception as e:  # noqa: BLE001 - reported through the checks below
        err = e
    finally:
        os.environ.pop("CURATARR_LOOP_GUARD", None)
    sites = [s["site"] for s in loop_guard.snapshot()["sites"] if s["site"].startswith(MINE)]
    return res, err, sites


def _clean(err):
    return err is None or not isinstance(err, loop_guard.LoopBlockingQuery)


# ── 1. _write_enrichment_db ─────────────────────────────────────────────────
print("_write_enrichment_db")
with get_db_session() as db:
    for i, pid in enumerate(("501", "502", "503"), 1):
        db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id=pid, title=f"Ep {i}",
                                 series_title="Dark", media_type="show",
                                 viewed_at=datetime.now(timezone.utc)))
    db.add(EnrichmentStatus(plex_rating_key="502", title="Dark", media_category="show",
                            enriched=False))
    db.add(ArrEnrichmentStatus(service="sonarr", arr_id=9, category="show", title="Dark"))

item = {"plex_rating_key": "sonarr:9", "title": "Dark", "series_title": "Dark",
        "service": "sonarr", "arr_id": 9, "media_type": "show"}
profile = {"source": "tmdb+llm", "title": "Dark", "rating": 8.7,
           "match_basis": "tvdb_id", "match_confidence": 1.0}
res, err, sites = run_guarded(lambda: en._write_enrichment_db(item, dict(profile), "show",
                                                              fresh_attempt=True))
check("runs without a statement on the loop", _clean(err) and err is None and sites == [])
with get_db_session() as db:
    st = db.query(EnrichmentStatus).filter_by(plex_rating_key="sonarr:9").one_or_none()
    arr = db.query(ArrEnrichmentStatus).filter_by(service="sonarr", arr_id=9).one()
    ep = db.query(EnrichmentStatus).filter_by(plex_rating_key="502").one()
    check("the status row is written as LLM-enriched",
          st is not None and st.enriched is True and st.error is None and st.match_basis == "tvdb_id")
    check("the arr row mirrors it", arr.enriched is True and arr.error is None)
    check("the series' other episodes are marked enriched", ep.enriched is True)
with MetadataCache() as mc:
    check("the profile is cached for the item and each episode",
          mc.get_cache("enriched:show:sonarr:9") is not None
          and all(mc.get_cache(f"enriched:show:{p}") is not None for p in ("501", "502", "503")))
check("a polished write counts no attempt", res is False)

# ── 2. _run_enrichment, nothing left to do ──────────────────────────────────
print("_run_enrichment")
with get_db_session() as db:
    db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id="11", title="Heat", media_type="movie",
                             viewed_at=datetime.now(timezone.utc)))
    db.add(EnrichmentStatus(plex_rating_key="11", title="Heat", media_category="movie",
                            enriched=True, error=None))
with MetadataCache() as mc:
    mc.set_cache("enriched:movie:11", {"source": "tmdb+llm", "title": "Heat"}, days=30)
app_state.set_state("enrichment_running", "0")
check("the route's lock is taken", app_state.acquire_state_lock("enrichment_running"))
taste = []


async def _no_taste(uid, cats=None):
    taste.append((uid, cats))


real_taste = en._run_taste_computation
en._run_taste_computation = _no_taste
try:
    _, err, sites = run_guarded(lambda: en._run_enrichment(1, ["movie"], "watch_history", None))
finally:
    en._run_taste_computation = real_taste
check("runs without a statement on the loop", err is None and sites == [])
check("the lock is released", app_state.get_state("enrichment_running") == "0")
check("the run settings are persisted",
      json.loads(app_state.get_state("enrichment_last_settings") or "{}").get("categories") == ["movie"])
check("nothing to do: interrupted cleared, left 0, last run stamped",
      app_state.get_state("enrichment_interrupted") == "0"
      and app_state.get_state("enrichment_left") == "0"
      and bool(app_state.get_state("last_enrichment_at")))
check("the live-cached title was filtered out and taste ran once", taste == [(1, ["movie"])])

# ── 3. _audit_enrichments ───────────────────────────────────────────────────
print("_audit_enrichments")
with get_db_session() as db:
    db.add(EnrichmentStatus(plex_rating_key="radarr:7", title="Ghost", media_category="movie",
                            enriched=True))
    db.add(ArrEnrichmentStatus(service="radarr", arr_id=7, category="movie", title="Ghost",
                               enriched=True))
with MetadataCache() as mc:
    mc.set_cache("enriched:movie:radarr:7",
                 {"source": "tmdb+llm", "title": "Ghost", "rating": 0,
                  "plex_rating_key": "radarr:7"}, days=30)


async def _no_arr(cats):
    return []


real_collect = en._collect_arr_items
en._collect_arr_items = _no_arr
try:
    out, err, sites = run_guarded(lambda: en._audit_enrichments(dry_run=False))
finally:
    en._collect_arr_items = real_collect
check("runs without a statement on the loop", err is None and sites == [])
check("the zero-rating profile is requeued",
      (out or {}).get("requeued", 0) >= 1 and (out or {}).get("by_reason", {}).get("zero_rating", 0) >= 1)
with get_db_session() as db:
    st = db.query(EnrichmentStatus).filter_by(plex_rating_key="radarr:7").one()
    arr = db.query(ArrEnrichmentStatus).filter_by(service="radarr", arr_id=7).one()
    f = db.query(EnrichmentFinding).filter_by(plex_rating_key="radarr:7").one_or_none()
    check("its status rows are flipped back", st.enriched is False and arr.enriched is False)
    check("and it is recorded as a finding", f is not None and f.kind == "zero_rating")
with MetadataCache() as mc:
    check("its cache row is purged", mc.get_cache("enriched:movie:radarr:7") is None)

# ── 4. enrichment_status ────────────────────────────────────────────────────
print("enrichment_status")
app_state.set_state("enrichment_running", "1")
app_state.set_state("arr_counts_cache", json.dumps({"radarr": {"total": 3}}))
me = User(id=1, plex_username="owner", is_admin=True)
body, err, sites = run_guarded(lambda: en.enrichment_status(user=me, quick=True))
check("runs without a statement on the loop or a request session", err is None and sites == [])
cats = (body or {}).get("categories", {})
check("per-category counts come back",
      cats.get("show", {}).get("total_unique") == 1 and cats.get("show", {}).get("enriched") == 1
      and cats.get("movie", {}).get("total_in_history") == 1)
check("with the cached arr counts and the running flag",
      (body or {}).get("arr") == {"radarr": {"total": 3}} and (body or {}).get("running") is True)
app_state.set_state("enrichment_running", "0")

# ── 5. poll_plex_pin ────────────────────────────────────────────────────────
print("poll_plex_pin")


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


plex_user = {"id": 4242, "username": "owner"}


class _FakeClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, **kw):
        if "/pins/" in url:
            return _Resp(200, {"authToken": "tok-" + str(plex_user["id"])})
        return _Resp(200, dict(plex_user))


class _Req:
    def __init__(self, nonce):
        self.client = type("C", (), {"host": "192.168.1.20"})()
        self.headers = {"X-Plex-Pin-Nonce": nonce}


def _poll(pin_id):
    nonce = auth._bind_pin(pin_id, "g1-client")
    return lambda: auth.poll_plex_pin(pin_id, _Req(nonce), BackgroundTasks())


real_client, real_member = auth.httpx.AsyncClient, settings.PLEX_LOGIN_REQUIRE_MEMBERSHIP
auth.httpx.AsyncClient = _FakeClient
settings.PLEX_LOGIN_REQUIRE_MEMBERSHIP = False
try:
    first, err, sites = run_guarded(_poll(7001))
    check("first login runs without a statement on the loop", err is None and sites == [])
    check("and creates the admin", (first or {}).get("status") == "ok"
          and (first or {}).get("user", {}).get("is_admin") is True
          and (first or {}).get("user", {}).get("username") == "owner"
          and bool((first or {}).get("token")))
    again, err, sites = run_guarded(_poll(7002))
    check("a returning login runs without a statement on the loop", err is None and sites == [])
    with get_db_session() as db:
        u = db.query(User).filter_by(plex_user_id="4242").one()
        check("and stamps the same user", (again or {}).get("user", {}).get("id") == u.id
              and u.last_login is not None and u.plex_token == "tok-4242")
        u.is_active = False
    _, err, sites = run_guarded(_poll(7003))
    check("a deactivated account still gets 403",
          isinstance(err, HTTPException) and err.status_code == 403 and sites == [])
    plex_user = {"id": 5151, "username": "guest"}
    guest, err, sites = run_guarded(_poll(7004))
    check("a second account is created as a member",
          err is None and sites == [] and (guest or {}).get("user", {}).get("is_admin") is False)
finally:
    auth.httpx.AsyncClient, settings.PLEX_LOGIN_REQUIRE_MEMBERSHIP = real_client, real_member
    auth._pin_bindings.clear()

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
