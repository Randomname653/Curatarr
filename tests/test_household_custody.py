"""Household boundaries and maintenance custody (2026-10-19 audit).

Each block pins one finding of the shadow-module / household audit:

* reclassifying a show as anime left its protections under "show", and the
  category-scoped deletion scan stopped seeing them;
* a member's "keep this" wrote a protection the owner's deletion run never
  read — now it is a Pillar III claim (never a CUT);
* principle capture ran for members, whose principles nobody can review;
* orphan repair credited every household play to the admin;
* the owner's SoulSync follows were mixed into every member's music vector;
* a casual reply in a verification thread was stored as the answer;
* a failing custodian task re-ran every 30 minutes for good, and the OMDb
  task stamped itself done over a backlog;
* the catalog sync re-added artists the owner had just deleted;
* a Retry of a lost delete answered 404 and became a dead-end error.

    python tests/test_household_custody.py
"""
import asyncio
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

PASS = FAIL = 0


def check(name, cond, info=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  [{info}]" if info is not None else ""))


import src.database.connection as conn  # noqa: E402
from src.database.models import (  # noqa: E402
    ArrEnrichmentStatus, Base, DeletionProposal, EnrichmentStatus, ProtectedMedia,
    User, WatchHistoryEntry)

ROOT = Path(__file__).resolve().parents[1]


def _fresh_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine)

    @contextmanager
    def session():
        s = make()
        try:
            yield s
        finally:
            s.close()
    return make, session


def _household(s):
    s.add_all([
        User(id=1, plex_username="owner", plex_user_id="1", is_admin=True, is_active=True),
        User(id=2, plex_username="ana", plex_user_id="222", is_admin=False, is_active=True),
        User(id=3, plex_username="gone", plex_user_id="333", is_admin=False, is_active=False),
    ])
    s.commit()


make, session = _fresh_db()
_real_session = conn.get_db_session
conn.get_db_session = session
with session() as s:
    _household(s)

# ── household.is_admin_user ─────────────────────────────────────────────────
from src.services.household import is_admin_user  # noqa: E402

check("is_admin_user: the owner is admin, a member is not",
      is_admin_user(1) is True and is_admin_user(2) is False)
check("is_admin_user: unknown / None user reads as a member (fails closed)",
      is_admin_user(99) is False and is_admin_user(None) is False)

# ── C. reclassify moves protections with the item ───────────────────────────
import src.services.library_sorting as ls  # noqa: E402
import src.cache.metadata_cache as mcache  # noqa: E402
import src.vector_store.chromadb_wrapper as cw  # noqa: E402

with session() as s:
    s.add(EnrichmentStatus(plex_rating_key="sonarr:42", title="Frieren",
                           media_category="show"))
    s.add(ArrEnrichmentStatus(service="sonarr", arr_id=42, category="show",
                              title="Frieren", tmdb_id=209867))
    s.add_all([
        ProtectedMedia(user_id=1, identifier="Frieren", category="show"),
        ProtectedMedia(user_id=2, identifier="209867", category="show"),
        # Same title in another domain: must not move.
        ProtectedMedia(user_id=1, identifier="Frieren", category="music"),
        ProtectedMedia(user_id=1, identifier="Other Show", category="show"),
    ])
    s.commit()


class _NoCache:
    def get_cache(self, k):
        return None

    def set_cache(self, *a, **k):
        pass


class _NoChroma:
    def get_by_id(self, k):
        return None


_saved = (mcache.MetadataCache, cw.get_chroma_db)
mcache.MetadataCache = _NoCache
cw.get_chroma_db = lambda: _NoChroma()
errs: list = []
old = ls._recategorize_local(42, "anime", errs)
mcache.MetadataCache, cw.get_chroma_db = _saved
with session() as s:
    cats = {(p.user_id, p.identifier, p.category) for p in s.query(ProtectedMedia)}
check("reclassify reports the old category, no step failed", old == "show" and not errs, errs)
check("protections by title AND tmdb id move to the new category (every member's)",
      (1, "Frieren", "anime") in cats and (2, "209867", "anime") in cats
      and (1, "Frieren", "show") not in cats, cats)
check("a same-named protection in another domain, and other titles, stay put",
      (1, "Frieren", "music") in cats and (1, "Other Show", "show") in cats)

# ── Pillar III: a member's keep is a household claim ────────────────────────
from src.services import pillars  # noqa: E402

with session() as s:
    s.add_all([
        ProtectedMedia(user_id=2, identifier="Severance", category="show"),
        ProtectedMedia(user_id=1, identifier="Owner Only", category="show"),
        ProtectedMedia(user_id=3, identifier="Inactive Keep", category="show"),
        ProtectedMedia(user_id=2, identifier="Legacy Keep", category=None),
        ProtectedMedia(user_id=2, identifier="Heat", category="movie"),
    ])
    s.commit()
    keeps = pillars._other_users_keeps
    check("a member's keep of this title names the member",
          keeps(s, 1, {"title": "Severance"}, "show") == ["ana"])
    check("the owner's own protection is not a household claim",
          keeps(s, 1, {"title": "Owner Only"}, "show") == [])
    check("an inactive member's keep does not count",
          keeps(s, 1, {"title": "Inactive Keep"}, "show") == [])
    check("legacy NULL-category keeps count in every category",
          keeps(s, 1, {"title": "Legacy Keep"}, "movie") == ["ana"])
    check("a keep in another category does not cross domains",
          keeps(s, 1, {"title": "Heat"}, "show") == [])
    check("a keep stored by tmdb id matches the item's tmdb id",
          keeps(s, 1, {"title": "Frieren", "tmdb_id": 209867}, "anime") == ["ana"])

src_p = (ROOT / "src/services/pillars.py").read_text(encoding="utf-8")
check("build_evidence turns a member keep into household_claim (the CUT floor)",
      'flags["household_claim"] = f"{keeps[0]} (asked to keep it)"' in src_p)
eng = (ROOT / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
check("the deletion loop floors any household_claim CUT at STAGNANT",
      'if v == "CUT" and _claim:' in eng and '"verdict": "STAGNANT"' in eng)

# ── principle capture: owner only ───────────────────────────────────────────
em_src = (ROOT / "src/services/episodic_memory.py").read_text(encoding="utf-8")
_cap = em_src[em_src.index('(thread_id or "").startswith("deletion_proposal:")'):
              em_src.index("capture_principles_from_thread(user_id")]
check("principle capture from deletion debates is gated on the owner",
      "await run_read(is_admin_user, user_id)" in _cap)
_msg = em_src[em_src.index("if protected_titles:"):]
check("a member's keep is announced as 'protected for you', the owner deciding",
      "if await run_read(is_admin_user, user_id):" in _msg
      and "now protected for you" in _msg)

# ── D. orphan repair attributes plays per Plex account ──────────────────────
import httpx  # noqa: E402
import src.services.orphan_repair as orph  # noqa: E402
import src.services.plex_sync as ps  # noqa: E402

from src.config import settings  # noqa: E402

settings.PLEX_URL, settings.PLEX_TOKEN, settings.PLEX_AUTH_TOKEN = "http://plex.test", None, "t"
orph.get_db_session = session
VIEWED = int(datetime(2026, 9, 1).timestamp())
HIST = [
    {"librarySectionID": 9, "ratingKey": "500", "viewedAt": VIEWED, "accountID": 222,
     "title": "Ep 1", "grandparentTitle": "Severance", "type": "episode"},
    {"librarySectionID": 9, "ratingKey": "501", "viewedAt": VIEWED, "accountID": 1,
     "title": "Ep 2", "grandparentTitle": "Severance", "type": "episode"},
    {"librarySectionID": 9, "ratingKey": "502", "viewedAt": VIEWED, "accountID": 777,
     "title": "Ep 3", "grandparentTitle": "Severance", "type": "episode"},
]
_RealClient = httpx.AsyncClient


def _hist_handler(req):
    return httpx.Response(200, json={"MediaContainer": {"Metadata": HIST}})


def _client(*a, **kw):
    kw.pop("transport", None)
    return _RealClient(*a, transport=httpx.MockTransport(_hist_handler), **kw)


async def _accounts(url, headers, users):
    by = {u.id: u for u in users}
    return {"1": by[1], "222": by[2]}


_saved_acct = ps._fetch_account_to_user_map
ps._fetch_account_to_user_map = _accounts
orph.httpx.AsyncClient = _client
try:
    res = asyncio.run(orph.apply_orphan_mapping(
        [{"section_id": "9", "category": "show", "title": "Old Shows"}]))
    res2 = asyncio.run(orph.apply_orphan_mapping(
        [{"section_id": "9", "category": "show", "title": "Old Shows"}]))
finally:
    orph.httpx.AsyncClient = _RealClient
    ps._fetch_account_to_user_map = _saved_acct
with session() as s:
    rows = {r.plex_item_id: (r.user_id, r.plex_user_id)
            for r in s.query(WatchHistoryEntry)}
check("a member's orphaned play is credited to the member, not the admin",
      rows.get("500") == (2, "222"), rows)
check("the owner's play stays the owner's", rows.get("501") == (1, "1"), rows)
check("only an unresolvable account is parked on the admin",
      rows.get("502", (None,))[0] == 1, rows)
check("a re-run adds nothing (dedup by the resolved user)",
      res.get("synced") == 3 and res2.get("synced") == 0, (res, res2))

# ── E. SoulSync signals only shape the owner's vector ───────────────────────
te_src = (ROOT / "src/services/taste_engine.py").read_text(encoding="utf-8")
check("taste engine applies SoulSync follows/likes only for the owner",
      "if await run_read(is_admin_user, user_id) else {}" in te_src
      and te_src.index("is_admin_user, user_id") < te_src.index("followed_new = 0"))

# ── F. a verification reply must actually answer ────────────────────────────
import src.services.verification_session as vs  # noqa: E402

LLM = {"content": ""}
WRITES: list = []


def _llm_handler(req):
    return httpx.Response(200, json={"message": {"content": LLM["content"]}})


def _llm_client(*a, **kw):
    kw.pop("transport", None)
    return _RealClient(*a, transport=httpx.MockTransport(_llm_handler), **kw)


async def _no_wait(*a, **k):
    return None


WRITE_RESULT = {"id": 7}


async def _write_memory(**kw):
    WRITES.append(kw)
    return WRITE_RESULT["id"]


import src.services.llm_priority as lp  # noqa: E402
_saved_v = (vs.httpx.AsyncClient, vs.write_memory, lp.wait_for_curator)
vs.httpx.AsyncClient = _llm_client
vs.write_memory = _write_memory
lp.wait_for_curator = _no_wait
Q = {"question": "You bailed on Dark at 30% — what killed it?", "title": "Dark",
     "context": {"media_type": "show"}}


def _answer(payload, reply="whatever"):
    LLM["content"] = json.dumps(payload)
    WRITES.clear()
    return asyncio.run(vs.process_verification_response(1, reply, Q))


try:
    ok = _answer({"answers_question": False, "sentiment": "positive",
                  "key_insight": "Likes filler"}, "wait, which show?")
    check("a clarifying question back is not an answer: nothing stored, claim reverts",
          ok is False and WRITES == [])
    ok = _answer({"sentiment": "negative", "key_insight": "Too slow"}, "yeah lol")
    check("a reply the model does not mark as an answer is not stored",
          ok is False and WRITES == [])
    ok = _answer({"answers_question": True, "sentiment": "unclear",
                  "key_insight": "Found the timeline confusing"})
    check("an unknown sentiment label is stored as neutral feedback, never explicit",
          ok is True and WRITES and WRITES[0]["memory_type"] == "feedback"
          and WRITES[0]["metadata"]["sentiment"] == "neutral", WRITES)
    ok = _answer({"answers_question": "true", "sentiment": "Negative",
                  "key_insight": "The pacing dragged in season one"})
    check("a real answer is stored as an explicit statement",
          ok is True and WRITES[0]["memory_type"] == "explicit_statement")
    WRITE_RESULT["id"] = None
    ok = _answer({"answers_question": True, "sentiment": "negative",
                  "key_insight": "The pacing dragged"})
    check("an answer whose memory write failed keeps the question open",
          ok is False and len(WRITES) == 1)
finally:
    vs.httpx.AsyncClient, vs.write_memory, lp.wait_for_curator = _saved_v

# ── G. custodian: failing tasks back off; OMDb is done only when drained ────
import src.services.app_state as app_state  # noqa: E402
import src.services.data_custodian as dc  # noqa: E402
import src.services.scheduler as sched  # noqa: E402

STATE: dict = {}
_saved_state = (app_state.get_state, app_state.set_state,
                sched._record_job_run, sched._job_overdue, dc._gaming)
app_state.get_state = lambda k: STATE.get(k)
app_state.set_state = lambda k, v: STATE.__setitem__(k, v) or True
recorded: list = []
sched._record_job_run = lambda job_id: recorded.append(job_id)
sched._job_overdue = lambda job_id, cadence_h: True
dc._gaming = lambda: False
CALLS = {"n": 0, "fail": True}


async def _flaky():
    CALLS["n"] += 1
    if CALLS["fail"]:
        raise dc.TaskFailed("no collection designs produced")
    return True


task = dc.Task("flaky", "Flaky", 168.0, _flaky, reports_own=True)
try:
    r1 = asyncio.run(dc._run_queue(dc.QUEUE_BACKGROUND, [task], False, False))
    rec = json.loads(STATE.get("job_fail:flaky") or "{}")
    check("a failure is recorded as JSON text in app_state",
          isinstance(STATE.get("job_fail:flaky"), str) and rec.get("n") == 1
          and r1["actions"][0]["result"].startswith("error"), STATE)
    r2 = asyncio.run(dc._run_queue(dc.QUEUE_BACKGROUND, [task], False, False))
    check("the next tick backs off instead of re-running it",
          CALLS["n"] == 1 and r2["actions"][0]["result"].startswith("backing off"), r2)
    asyncio.run(dc._run_queue(dc.QUEUE_BACKGROUND, [task], True, False))
    check("'Run maintenance now' (force) ignores the backoff",
          CALLS["n"] == 2 and json.loads(STATE["job_fail:flaky"])["n"] == 2)
    check("backoff doubles: 2 failures wait an hour, not 30 minutes",
          not dc._backoff_due("flaky", 168.0))
    STATE["job_fail:flaky"] = json.dumps(
        {"n": 2, "at": (datetime.utcnow() - timedelta(minutes=61)).isoformat()})
    check("...and the task is due again once the wait is over",
          dc._backoff_due("flaky", 168.0))
    STATE["job_fail:flaky"] = json.dumps(
        {"n": 30, "at": (datetime.utcnow() - timedelta(hours=25)).isoformat()})
    check("the wait never exceeds the task's own cadence",
          dc._backoff_due("flaky", 24.0) and not dc._backoff_due("flaky", 168.0))
    STATE["job_fail:flaky"] = "not json"
    check("an unreadable record counts as no failures", dc._backoff_due("flaky", 24.0))
    STATE["job_fail:flaky"] = json.dumps({"n": 1, "at": datetime.utcnow().isoformat()})
    CALLS["fail"] = False
    asyncio.run(dc._run_queue(dc.QUEUE_BACKGROUND, [task], True, False))
    check("a successful run clears the streak (written as '')",
          STATE["job_fail:flaky"] == "" and "flaky" in recorded
          and dc._backoff_due("flaky", 24.0))

    # OMDb: done only when nothing is left beyond the run's budget.
    import src.services.media_enricher as me
    _saved_omdb = me.run_omdb_backfill

    async def _omdb_backlog(task=None, limit=None):
        return {"candidates": limit, "enriched": limit, "remaining": 300}

    async def _omdb_drained(task=None, limit=None):
        return {"candidates": 12, "enriched": 12, "remaining": 0}
    me.run_omdb_backfill = _omdb_backlog
    left = asyncio.run(dc._run_omdb())
    me.run_omdb_backfill = _omdb_drained
    drained = asyncio.run(dc._run_omdb())
    me.run_omdb_backfill = _saved_omdb
    check("OMDb top-up stays due while a backlog remains past the limit",
          left is False and drained is True)
finally:
    (app_state.get_state, app_state.set_state,
     sched._record_job_run, sched._job_overdue, dc._gaming) = _saved_state

dc_src = (ROOT / "src/services/data_custodian.py").read_text(encoding="utf-8")
check("collections and Discogs failures raise TaskFailed (backoff), not False",
      'raise TaskFailed("no collection designs produced")' in dc_src
      and 'raise TaskFailed(f"discogs styles:' in dc_src)

# OMDb backfill passes the row's own cache id (AniList-keyed anime rows).
import src.services.media_enricher as me  # noqa: E402

ROWS = [("v2:raw:anime:154587", json.dumps({"title": "Frieren", "imdb_id": "tt22248376",
                                             "tmdb_id": 209867})),
        ("v2:raw:movie:603", json.dumps({"title": "The Matrix", "imdb_id": "tt0133093"})),
        ("v2:raw:movie:604", json.dumps({"title": "Matrix 2", "imdb_id": "tt0234215"}))]
TOPUPS: list = []


async def _topup(title, cat, **kw):
    TOPUPS.append((title, cat, kw))
    return True

_saved_me = (me._with_handle, me.topup_omdb)
me._with_handle = lambda h, fn: ROWS
me.topup_omdb = _topup
try:
    r = asyncio.run(me.run_omdb_backfill(limit=2))
finally:
    me._with_handle, me.topup_omdb = _saved_me
anime = next((t for t in TOPUPS if t[1] == "anime"), None)
check("the OMDb top-up gets the raw row's own id (AniList-keyed anime is stamped)",
      anime is not None and anime[2].get("cache_id") == "154587", TOPUPS)
check("the backfill reports what its limit left over",
      r.get("candidates") == 2 and r.get("remaining") == 1, r)

# ── A. catalog sync never resurrects a deleted artist ───────────────────────
import src.services.music_catalog_sync as mcs  # noqa: E402

with session() as s:
    s.add_all([
        DeletionProposal(id=101, user_id=1, media_id="11", title="Nickelback",
                         service="lidarr", category="music", status="deleted",
                         soulsync_ban=json.dumps({"ok": True, "mbid": "mb-nb"})),
        DeletionProposal(id=102, user_id=1, media_id="12", title="Beatles, The",
                         service="plex", category=None, status="limbo"),
        DeletionProposal(id=103, user_id=1, media_id="13", title="Kept Back",
                         service="lidarr", category="music", status="deleted",
                         returned_info=json.dumps({"state": "kept"})),
        DeletionProposal(id=104, user_id=1, media_id="14", title="Heat",
                         service="radarr", category="movie", status="deleted"),
        DeletionProposal(id=105, user_id=1, media_id="15", title="Pending Band",
                         service="lidarr", category="music", status="pending"),
    ])
    s.commit()
mbids, names = mcs.music_tombstones()
from src.services.soulsync_client import norm_name  # noqa: E402
check("deleted (and limbo) music artists are tombstoned by MBID and name",
      "mb-nb" in mbids and norm_name("Nickelback") in names
      and norm_name("Beatles, The") in names, (mbids, names))
check("a returned artist the owner kept, other categories and open proposals are not",
      norm_name("Kept Back") not in names and norm_name("Heat") not in names
      and norm_name("Pending Band") not in names, names)

# ── limbo Retry: a 404 means the earlier attempt deleted it ─────────────────
import src.routers.recommendations as recs  # noqa: E402
from src.config import settings  # noqa: E402

settings.RADARR_URL, settings.RADARR_API_KEY = "http://radarr.test", "k"


def _gone(req):
    return httpx.Response(404)


def _with(handler):
    def factory(*a, **kw):
        kw.pop("transport", None)
        return _RealClient(*a, transport=httpx.MockTransport(handler), **kw)
    return factory


recs.httpx.AsyncClient = _with(_gone)
try:
    first = SimpleNamespace(service="radarr", media_id="7", title="Big Movie")
    retry = SimpleNamespace(service="radarr", media_id="7", title="Big Movie",
                            retry_delete=True)
    check("a 404 on a first delete is still a failure",
          asyncio.run(recs._execute_arr_delete(first)) is False)
    check("a 404 on a Retry from limbo counts as deleted",
          asyncio.run(recs._execute_arr_delete(retry)) is True)
finally:
    recs.httpx.AsyncClient = _RealClient


class _PlexClient:
    def __init__(self, delete_status):
        self.delete_status = delete_status

    async def delete(self, url, headers=None):
        return httpx.Response(self.delete_status)

    async def get(self, url, headers=None):
        return httpx.Response(200)


import src.services.lyrics as lyr  # noqa: E402
_saved_gone = lyr.mark_artist_gone
lyr.mark_artist_gone = lambda key: None
try:
    if settings.effective_plex_url and settings.effective_plex_token:
        check("Plex: a 404 on a Retry delete is gone",
              asyncio.run(recs._plex_delete_artist("101", client=_PlexClient(404),
                                                   gone_ok=True)) is True)
        check("Plex: a 404 on a first delete is still a failure",
              asyncio.run(recs._plex_delete_artist("101", client=_PlexClient(404))) is False)
    else:
        check("Plex test configuration applies", False)
finally:
    lyr.mark_artist_gone = _saved_gone

# Lidarr: the guard reads the artist first — MBID for the ban, 404 on Retry = gone.
settings.LIDARR_URL, settings.LIDARR_API_KEY = "http://lidarr.test", "k"
LIDARR = {"status": 200}


def _lidarr(req):
    if req.url.path == "/api/v1/artist/11":
        if LIDARR["status"] == 404:
            return httpx.Response(404)
        return httpx.Response(200, json={"id": 11, "foreignArtistId": "mb-lidarr"})
    return httpx.Response(500)          # the refresh: not reached / fails closed


recs.httpx.AsyncClient = _with(_lidarr)
try:
    p = SimpleNamespace(service="lidarr", media_id="11", title="Beatles, The",
                        retry_delete=False, already_gone=False)
    drift = asyncio.run(recs._lidarr_freshness_guard(p))
    check("the freshness guard records Lidarr's MBID for the ban",
          p.lidarr_mbid == "mb-lidarr" and drift)        # refresh 500 → still blocked
    LIDARR["status"] = 404
    p = SimpleNamespace(service="lidarr", media_id="11", title="Beatles, The",
                        retry_delete=True, already_gone=False)
    drift = asyncio.run(recs._lidarr_freshness_guard(p))
    check("a Retry whose artist is already gone from Lidarr passes as deleted",
          drift is None and p.already_gone is True)
    p = SimpleNamespace(service="lidarr", media_id="11", title="Beatles, The",
                        retry_delete=False, already_gone=False)
    check("...but a first attempt on a missing artist stays blocked",
          asyncio.run(recs._lidarr_freshness_guard(p)) and not p.already_gone)
finally:
    recs.httpx.AsyncClient = _RealClient

# The ban uses Lidarr's MBID before any SoulSync name search.
import src.services.deletion_returns as dr  # noqa: E402
import src.services.soulsync_client as ssc  # noqa: E402

BANS: list = []


async def _artist_info(name):
    BANS.append(("search", name))
    return {"musicbrainz_id": "mb-wrong-namesake"}


async def _block(name, mbid=None, *, client=None):
    BANS.append(("ban", name, mbid))
    return {"ok": True, "id": 5}

_saved_ss = (ssc.writes_allowed, ssc.artist_info, ssc.block_artist)
ssc.writes_allowed, ssc.artist_info, ssc.block_artist = (lambda: True), _artist_info, _block
try:
    p = SimpleNamespace(title="Beatles, The", category="music", service="lidarr",
                        soulsync_ban=None, lidarr_mbid="mb-lidarr")
    asyncio.run(dr.ban_deleted_artist(p))
    check("the SoulSync ban carries Lidarr's MBID, no name search",
          BANS == [("ban", "Beatles, The", "mb-lidarr")]
          and json.loads(p.soulsync_ban)["mbid"] == "mb-lidarr", BANS)
    BANS.clear()
    p = SimpleNamespace(title="Plex Artist", category="music", service="plex",
                        soulsync_ban=None)
    asyncio.run(dr.ban_deleted_artist(p))
    check("without a Lidarr MBID the SoulSync search still supplies one",
          BANS == [("search", "Plex Artist"), ("ban", "Plex Artist", "mb-wrong-namesake")])
finally:
    ssc.writes_allowed, ssc.artist_info, ssc.block_artist = _saved_ss

conn.get_db_session = _real_session
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
