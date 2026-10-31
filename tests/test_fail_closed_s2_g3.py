"""Fail-open guards made loud, an unknown verdict kept unknown, and four
small read/clear races closed.

  * rate_limit.enforce fails open on an internal error (a limiter bug must
    not lock the owner out) but did so silently - the setup-code brute-force
    limit and every per-user budget were quietly off; now a WARNING
  * the sprint read an unreadable enrichment lock as "not running" and could
    start a second run beside it or end itself; now "running"
  * unload_llm_models fell back to the legacy embedding model alone when the
    active one was unreadable, leaving the real one in VRAM on game start
  * a dead judge made every pair a "tie" and the review's verdict "either",
    like a real draw; now "unreadable" pairs and "no verdict"
  * size_norms / embed_service caches: a read that began before an
    invalidation stored its stale result after the clear
  * the custodian's followup / recs flags were cleared unconditionally after
    minutes of work, losing a flag set meanwhile
  * two first logins racing past first_ever both became admin
  * facet backfill: a failed cursor READ restarted the whole backfill at 0

Each check fails on the code before this change.

    python tests/test_fail_closed_s2_g3.py
"""
import asyncio
import contextlib
import logging
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import (Base, CachedRecommendation, MediaSizeNorm,  # noqa: E402
                                 MediaTechProfile, User)
from src.services import app_state  # noqa: E402

Base.metadata.create_all(conn_mod.engine)

from _checks import check  # noqa: E402


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextlib.contextmanager
def captured(name):
    h = _Capture()
    lg = logging.getLogger(name)
    old = lg.level
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    try:
        yield h.records
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)


def warned(recs, text=""):
    return any(r.levelno >= logging.WARNING and text in r.getMessage() for r in recs)


# ── 1. rate limiter: open, but loud ─────────────────────────────────────────
print("rate_limit.enforce")
from src.services import rate_limit  # noqa: E402

rate_limit.reset("g3-probe")
rate_limit._BUCKETS[("g3-probe", "k")] = ["not-a-timestamp"]   # a limiter bug
with captured("src.services.rate_limit") as recs:
    try:
        rate_limit.enforce("g3-probe", "k", 5, 60)
        let_through = True
    except Exception:
        let_through = False
rate_limit.reset("g3-probe")
check("a limiter error still lets the request through (fail open on purpose)", let_through)
check("...and says so at WARNING", warned(recs, "budgets NOT enforced"))

# ── 2. sprint: an unreadable enrichment lock reads as running ───────────────
print("data_custodian._enrichment_running")
import src.services.data_custodian as dc  # noqa: E402
import src.services.scheduler as sched  # noqa: E402

_real_get = app_state.get_state


def _broken(key):
    raise RuntimeError("database is locked")


app_state.get_state = _broken
try:
    with captured("src.services.data_custodian") as recs:
        running = dc._enrichment_running()
finally:
    app_state.get_state = _real_get
check("unreadable lock -> 'running' (no second run, no early sprint end)", running is True)
check("...WARNING", warned(recs, "enrichment lock unreadable"))
app_state.set_state("enrichment_running", "0")
check("a readable lock is read as it is", dc._enrichment_running() is False)

# ── 3. unload on game start: the active embedder is not left in VRAM ────────
print("process_monitor._our_models")
from src.config import settings  # noqa: E402
from src.services import embed_service as es  # noqa: E402
from src.services import process_monitor as pm  # noqa: E402
from src.services.embedding_migration import V2_PROFILE  # noqa: E402

_real_eff = es.effective_embedding_model


def _eff_broken():
    raise RuntimeError("profile read crashed")


es.effective_embedding_model = _eff_broken
_real_legacy, settings.EMBEDDING_MODEL = settings.EMBEDDING_MODEL, "legacy-embedder"
try:
    with captured("src.services.process_monitor") as recs:
        ours = asyncio.run(pm._our_models()) if hasattr(pm, "_our_models") else []
finally:
    es.effective_embedding_model = _real_eff
    settings.EMBEDDING_MODEL = _real_legacy
check(f"unreadable profile -> both the profile model and the legacy one are unloaded ({ours})",
      V2_PROFILE["model"] in ours and "legacy-embedder" in ours)
check("...WARNING", warned(recs, "embedding model unreadable"))

# ── 4. model review: a dead judge is no verdict, not a draw ────────────────
print("model_update_eval.evaluate")
from src.services import model_update_eval as ev  # noqa: E402
import src.services.llm_priority as llm_priority  # noqa: E402

llm_priority.extend_bulk_hold = lambda *a, **k: None


async def _dead_judge(endpoint, model, system, user, schema):
    raise ConnectionError("ollama gone")


_real_ask = ev._ask
ev._ask = _dead_judge
comparison = {
    "summary": {"failures": {"old": 0, "new": 0}, "gpu_share_pct": {"new": 100},
                "gen_tok_s": {"old": 30.0, "new": 30.0}},
    "facts": {},
    "old": {"titles": [{"title": f"T{i}", "verdict": "KEEP", "monologue": "o"} for i in range(3)],
            "chat": [{"prompt": "q", "text": "old"}]},
    "new": {"titles": [{"title": f"T{i}", "verdict": "KEEP", "monologue": "n"} for i in range(3)],
            "chat": [{"prompt": "q", "text": "new"}]},
}
try:
    with captured("src.services.model_update_eval") as recs:
        out = asyncio.run(ev.evaluate(comparison, "e", "judge"))
finally:
    ev._ask = _real_ask
check(f"a dead judge -> 'no verdict', not 'either' ({out['recommendation']})",
      out["recommendation"] == "no verdict")
check("...the pairs count as unreadable, not as ties",
      out["tally"]["titles"].get("unreadable") == 3 and out["tally"]["titles"]["tie"] == 0)
check("...WARNING", warned(recs, "unreadable"))

# ── 5. size_norms: a stale read is not stored after the invalidation ───────
print("size_norms caches")
from src.services import size_norms as sn  # noqa: E402

with conn_mod.get_db_session() as db:
    db.add(MediaSizeNorm(class_key="movie|1080p|*", media_type="movie", median=10.0, p75=12.0, p90=15.0,
                         std=2.0, sample_count=20, computed_at=datetime(2026, 10, 1)))
    db.add(MediaTechProfile(plex_rating_key="1", media_type="movie", tmdb_id=7, size_mb=4000,
                            resolution="1080p", codec="h264", item_count=1))
    db.add(MediaTechProfile(plex_rating_key="2", media_type="movie", tmdb_id=7, size_mb=3000,
                            resolution="720p", codec="h264", item_count=1))
    db.commit()

_real_sess = sn.get_db_session


@contextlib.contextmanager
def _session_then_invalidate():
    # The recompute commits and invalidates right after this read's query
    # returned, before the reader stores what it got.
    with _real_sess() as db:
        real_query = db.query

        def query(*a, **kw):
            q = real_query(*a, **kw)
            real_all = q.all

            def all_then_invalidate():
                rows = real_all()
                sn.invalidate_norms_cache()
                return rows
            q.all = all_then_invalidate
            return q
        db.query = query
        yield db


sn.invalidate_norms_cache()
sn.get_db_session = _session_then_invalidate
try:
    norms = sn._load_norms()
    norms_cached = sn._NORMS_CACHE["data"]
    dup = sn._load_cross_dup()
finally:
    sn.get_db_session = _real_sess
check("norms: the read still answers", "movie|1080p|*" in (norms or {}))
check("norms: a read overtaken by an invalidation is not cached", norms_cached is None)
check("cross-dup: same", sn._CROSS_DUP["data"] is None and dup)
sn._load_norms()
check("norms: an undisturbed read is cached as before", sn._NORMS_CACHE["data"] is not None)

# ── 6. embed_service: a read overtaken by set_profile is not cached ────────
print("embed_service.get_profile")
import json  # noqa: E402

old_prof = dict(V2_PROFILE, model="old-embedder", collection="media_knowledge_old")
new_prof = dict(V2_PROFILE, model="new-embedder", collection="media_knowledge_new")
app_state.set_state(es.PROFILE_KEY, json.dumps(old_prof))
es._profile_cache["value"] = None


def _read_then_flip(key):
    value = _real_get(key)           # the reader saw the OLD profile ...
    if key == es.PROFILE_KEY:
        app_state.get_state = _real_get
        es.set_profile(new_prof)     # ... then the flip committed and cleared
    return value


app_state.get_state = _read_then_flip
try:
    first = es.get_profile()
finally:
    app_state.get_state = _real_get
check("the overtaken read answers with what it read", first["model"] == "old-embedder")
check("...but is not cached over the flip", es.get_profile()["model"] == "new-embedder")
es._profile_cache["value"] = None

# ── 7. custodian flags: compare-and-clear ───────────────────────────────────
print("data_custodian flags")
with conn_mod.get_db_session() as db:
    db.add(User(id=1, plex_user_id="p1", plex_username="admin", is_admin=True, is_active=True))
    db.commit()

_real = {"gaming": sched._gaming, "admin": dc._admin_id, "users": dc._active_user_ids,
         "chain": sched._recompute_and_cache_recs, "cache": sched._cache_recommendations}
sched._gaming = lambda: False
dc._admin_id = lambda: 1
dc._active_user_ids = lambda: [1]


async def _chain_with_new_plays(user_id=None):
    # A Plex sync finds new plays while the follow-up is still working.
    await app_state.aset_state(dc._FOLLOWUP_FLAG, "2026-10-19T12:00:01")


async def _chain(user_id=None):
    return None


async def _refresh_while_sampling(uid):
    await app_state.aset_state(f"recs_all_sampled:user_id={uid}", "movie,show")


try:
    app_state.set_state(dc._FOLLOWUP_FLAG, "2026-10-19T12:00:00")
    sched._recompute_and_cache_recs = _chain_with_new_plays
    done = asyncio.run(dc._run_plex_followup())
    check("followup: a flag set during the run survives (not cleared to 0)",
          app_state.get_state(dc._FOLLOWUP_FLAG) == "2026-10-19T12:00:01")
    check("...and the task stays due", done is False)
    sched._recompute_and_cache_recs = _chain
    done = asyncio.run(dc._run_plex_followup())
    check("followup: an unchanged flag is cleared as before",
          done is True and app_state.get_state(dc._FOLLOWUP_FLAG) == "0")

    with conn_mod.get_db_session() as db:
        db.add(CachedRecommendation(user_id=1, category="movie", title="x",
                                    cached_at=datetime(2026, 1, 1)))
        db.commit()
    app_state.set_state("recs_all_sampled:user_id=1", "movie")
    sched._cache_recommendations = _refresh_while_sampling
    ok = asyncio.run(dc._run_recs())
    check("recs: a category sampled during the refresh keeps its flag",
          app_state.get_state("recs_all_sampled:user_id=1") == "movie,show" and ok is False)
    sched._cache_recommendations = _chain
    ok = asyncio.run(dc._run_recs())
    check("recs: an unchanged flag is cleared as before",
          ok is True and app_state.get_state("recs_all_sampled:user_id=1") == "")
finally:
    sched._gaming, dc._admin_id, dc._active_user_ids = _real["gaming"], _real["admin"], _real["users"]
    sched._recompute_and_cache_recs = _real["chain"]
    sched._cache_recommendations = _real["cache"]

# ── 8. first login: one admin, even when two race ───────────────────────────
print("auth.poll_plex_pin")
if not settings.effective_jwt_secret:
    from pydantic import SecretStr
    settings.JWT_SECRET = SecretStr("test-only-secret-never-shipped-32-bytes-min-per-rfc7518")
from fastapi import BackgroundTasks  # noqa: E402
import src.routers.auth as auth  # noqa: E402

with conn_mod.get_db_session() as db:
    db.query(CachedRecommendation).delete()
    db.query(User).delete()
    db.commit()


class _Resp:
    def __init__(self, body):
        self.status_code, self._body = 200, body

    def json(self):
        return self._body


class _Plex:
    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, **kw):
        if url.endswith("/user"):
            return _Resp({"id": 2002, "username": "second"})
        return _Resp({"authToken": "tok"})


_real_auth = (auth.httpx.AsyncClient, auth._check_pin_binding, auth._enforce_poll_rate_limit,
              auth.run_write, settings.PLEX_LOGIN_REQUIRE_MEMBERSHIP)


def _other_first_login():
    with conn_mod.get_db_session() as db:
        db.add(User(plex_user_id="1001", plex_username="first", is_admin=True, is_active=True))
        db.commit()


async def _run_write_racing(fn, *a, **kw):
    if fn.__name__ == "_create_user":
        # The other first login committed between first_ever and this insert.
        await _real_auth[3](_other_first_login)
    return await _real_auth[3](fn, *a, **kw)


auth.httpx.AsyncClient = _Plex
auth._check_pin_binding = lambda pin_id, nonce: "cid"
auth._enforce_poll_rate_limit = lambda pin_id: None
auth.run_write = _run_write_racing
settings.PLEX_LOGIN_REQUIRE_MEMBERSHIP = False
rate_limit.reset("plex-poll-ip")
try:
    req = SimpleNamespace(headers={"X-Plex-Pin-Nonce": "n"}, client=SimpleNamespace(host="10.0.0.9"))
    res = asyncio.run(auth.poll_plex_pin(77, req, BackgroundTasks()))
finally:
    (auth.httpx.AsyncClient, auth._check_pin_binding, auth._enforce_poll_rate_limit,
     auth.run_write, settings.PLEX_LOGIN_REQUIRE_MEMBERSHIP) = _real_auth
    rate_limit.reset("plex-poll-ip")
with conn_mod.get_db_session() as db:
    admins = db.query(User).filter(User.is_admin == True).count()  # noqa: E712
check("the loser of a first-login race is a regular user",
      res["status"] == "ok" and res["user"]["is_admin"] is False)
check("...one admin in the table", admins == 1)

# ── 9. facet backfill: a failed cursor read does not restart at 0 ──────────
print("facet_index.run_facet_backfill")
from src.services import facet_index as fi  # noqa: E402
import src.vector_store.chromadb_wrapper as cw  # noqa: E402

asked = []
_real_chroma = cw.get_chroma_db
cw.get_chroma_db = lambda: SimpleNamespace(get_count=lambda: asked.append("count") or 0,
                                           facets_count=lambda: 0)


def _cursor_broken(key):
    if key == fi._BACKFILL_CURSOR_KEY:
        raise RuntimeError("database is locked")
    return _real_get(key)


app_state.get_state = _cursor_broken
try:
    try:
        asyncio.run(fi.run_facet_backfill())
        raised = False
    except RuntimeError:
        raised = True
finally:
    app_state.get_state = _real_get
    cw.get_chroma_db = _real_chroma
check("an unreadable cursor stops the tick (the custodian logs it), no restart at 0",
      raised and asked == [])


check.done()
