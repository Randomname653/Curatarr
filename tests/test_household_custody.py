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

from sqlalchemy.orm import sessionmaker  # noqa: E402

PASS = FAIL = 0


from _checks import memory_engine  # noqa: E402
def check(name, cond, info=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  [{info}]" if info is not None else ""))


import _harness  # noqa: E402,F401  (first: temp stores and .env, never the live data/)
import src.database.connection as conn  # noqa: E402
from src.database.models import (  # noqa: E402
    ArrEnrichmentStatus, Base, DeletionProposal, EnrichmentStatus, ProtectedMedia,
    User, WatchHistoryEntry)

ROOT = Path(__file__).resolve().parents[1]


def _fresh_db():
    engine = memory_engine()
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine)

    @contextmanager
    def session():
        # The contract of connection.get_db_session, which this stands in
        # for: commit on a clean exit, roll back on any exception. Without the
        # commit every write the app made through it (app_state among them)
        # silently vanished.
        s = make()
        try:
            yield s
            s.commit()
        except BaseException:
            s.rollback()
            raise
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

from _checks import real_state  # noqa: E402
STATE = real_state()          # the real app_state table, not a dict
_saved_state = (app_state.get_state, app_state.set_state,
                sched._record_job_run, sched._job_overdue, dc._gaming)
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

# ══ Low findings ════════════════════════════════════════════════════════════

# ── proactive messages: one run per user at a time, hourly cap applied ──────
import src.services.proactive_messages as pm  # noqa: E402
from src.database.models import EnrichmentFinding, MediaMatchOverride, ProactiveMessage  # noqa: E402

_saved_gen = pm._generate_messages
RUNS: list = []


async def _slow_gen(uid):
    RUNS.append(uid)
    await asyncio.sleep(0.05)
    return 3


async def _twice():
    return await asyncio.gather(pm.check_and_generate_messages(1),
                                pm.check_and_generate_messages(1),
                                pm.check_and_generate_messages(2))
pm._generate_messages = _slow_gen
try:
    out = asyncio.run(_twice())
finally:
    pm._generate_messages = _saved_gen
check("two overlapping runs for one user generate once; other users are not blocked",
      sorted(out) == [0, 3, 3] and sorted(RUNS) == [1, 2], (out, RUNS))
check("the guard is released after a run", 1 not in pm._GENERATING)

pm.get_db_session = session
TRIGGER_CALLS: list = []
_saved_pm = (pm._run_all_triggers, pm._get_sonarr_index, pm.get_disabled_triggers,
             pm._load_asked_subjects)


async def _no_index():
    return {}
pm._run_all_triggers = lambda *a, **k: TRIGGER_CALLS.append(1) or None
pm._get_sonarr_index = _no_index
pm.get_disabled_triggers = lambda uid: set()
pm._load_asked_subjects = lambda uid: {}
try:
    with session() as s:
        s.add(WatchHistoryEntry(user_id=2, plex_user_id="222", plex_item_id="9",
                                title="Ep", media_type="show", viewed_at=datetime.utcnow()))
        now = datetime.utcnow()
        for i in range(pm._MAX_PER_HOUR):
            s.add(ProactiveMessage(user_id=2, trigger_type=f"t{i}", message="m", read=True,
                                   created_at=now - timedelta(minutes=5)))
        s.commit()
    asyncio.run(pm._generate_messages(2))
    check("a full hour's worth of messages stops generation (the cap was never applied)",
          TRIGGER_CALLS == [])
    with session() as s:
        s.query(ProactiveMessage).filter(ProactiveMessage.user_id == 2).update(
            {"created_at": datetime.utcnow() - timedelta(hours=2)})
        s.commit()
    asyncio.run(pm._generate_messages(2))
    check("...and resumes once the hour has passed", TRIGGER_CALLS == [1])
finally:
    (pm._run_all_triggers, pm._get_sonarr_index, pm.get_disabled_triggers,
     pm._load_asked_subjects) = _saved_pm

# ── verification sessions: one at a time, re-checked at write, they expire ──
vs.get_db_session = session
_saved_vs = (vs.generate_verification_questions, vs.write_memory, vs.httpx.AsyncClient,
             lp.wait_for_curator, vs.detect_user_language)


async def _questions(uid):
    return [{"type": "abandoned", "title": "Dark", "question": "Why did you stop Dark?"}]


async def _wm(**kw):
    return 1


def _llm_down(req):
    return httpx.Response(500)
vs.generate_verification_questions = _questions
vs.write_memory = _wm
vs.httpx.AsyncClient = _with(_llm_down)
lp.wait_for_curator = _no_wait
vs.detect_user_language = lambda uid, db: "en"
try:
    async def _both():
        return await asyncio.gather(vs.start_verification_session(1),
                                    vs.start_verification_session(1))
    a, b = asyncio.run(_both())
    with session() as s:
        qs = [(q.expires_at, q.created_at) for q in s.query(ProactiveMessage).filter(
            ProactiveMessage.user_id == 1, ProactiveMessage.trigger_type == "verification")]
    check("two concurrent starts store ONE verification question",
          len(qs) == 1 and sorted([bool(a), bool(b)]) == [False, True], (a, b, len(qs)))
    check("a verification question expires (7 days), it no longer blocks the bell forever",
          qs and qs[0][0] is not None
          and timedelta(days=6) < qs[0][0] - qs[0][1] <= timedelta(days=7))
    # The write-time re-check: another process stored one during the LLM call.
    vsrc = (ROOT / "src/services/verification_session.py").read_text(encoding="utf-8")
    _st = vsrc[vsrc.index("    def _store() -> bool:"):vsrc.index("if not await run_write(_store):")]
    check("the store re-checks the 12-hour window before writing",
          "if _asked_recently(db, user_id):" in _st)
finally:
    (vs.generate_verification_questions, vs.write_memory, vs.httpx.AsyncClient,
     lp.wait_for_curator, vs.detect_user_language) = _saved_vs

# ── external-id harvest leaves pinned entries to the pin ────────────────────
import sqlite3  # noqa: E402
import src.services.external_ids as ei  # noqa: E402

with session() as s:
    s.add(MediaMatchOverride(service="radarr", arr_id=1, tmdb_id=5, title="Pinned Film"))
    s.commit()
cdb = sqlite3.connect(":memory:", check_same_thread=False)
cdb.execute("CREATE TABLE api_cache (cache_key TEXT, response TEXT, expires_at TEXT)")
for k, raw in (("v2:raw:movie:5", {"title": "Wrong Name", "tmdb_id": 5, "media_type": "movie"}),
               ("v2:raw:movie:pf", {"title": "pinned film", "media_type": "movie"}),
               ("v2:raw:movie:9", {"title": "Free Film", "tmdb_id": 9, "media_type": "movie"})):
    cdb.execute("INSERT INTO api_cache VALUES (?,?,?)", (k, json.dumps(raw), "2999-01-01"))
HARVEST_WRITES: list = []


async def _index(failed=None):
    return {"tv": {"by_name": {}, "by_id": {}},
            "movie": {"by_name": {"x": 1}, "by_id": {}}}
_saved_ei = (ei.build_arr_id_index, ei._match, ei._recheck, mcache.write_fields)
ei.build_arr_id_index = _index
ei._match = lambda raw, index, fams: ({"imdb_id": "tt9"}, "title")
ei._recheck = lambda cache, index, rows: 0
mcache.write_fields = lambda cache, key, raw, fields, **k: HARVEST_WRITES.append(key)
try:
    hres = asyncio.run(ei.harvest(SimpleNamespace(conn=cdb)))
finally:
    ei.build_arr_id_index, ei._match, ei._recheck, mcache.write_fields = _saved_ei
check("harvest skips entries a match pin covers (by tmdb id and by title)",
      HARVEST_WRITES == ["raw:movie:9"] and hres.get("added") == 1, (HARVEST_WRITES, hres))

# ── music misses: one row per name whatever its case; errors park ───────────
import src.services.music_misses as mm  # noqa: E402
from src.database.models import MusicLookupMiss  # noqa: E402


@contextmanager
def _committing():
    s = make()
    try:
        yield s
        s.commit()
    finally:
        s.close()
conn.get_db_session = _committing
try:
    mm.record_miss("Foo Fighters ")
    mm.record_miss("foo fighters")
    with session() as s:
        rows = [(r.name, r.attempts) for r in s.query(MusicLookupMiss)]
    check("a case variant updates the same miss row (backoff keeps growing)",
          len(rows) == 1 and rows[0][1] == 2, rows)
    mm.clear_miss("FOO FIGHTERS")
    with session() as s:
        check("clearing matches case-insensitively", s.query(MusicLookupMiss).count() == 0)
    for _ in range(mm._ERROR_PARK_AFTER - 1):
        mm.record_error("Raiser", error="KeyError")
    check("a lookup that raised stays due below the threshold",
          mm.due_filter(["Raiser"]) == ["Raiser"])
    mm.record_error("raiser", error="KeyError")
    check("...and is parked once it raised that many times in a row",
          mm.due_filter(["Raiser"]) == [])
finally:
    conn.get_db_session = session
mmsrc = (ROOT / "src/services/music_matcher.py").read_text(encoding="utf-8")
check("the MusicBrainz resolver counts lookups that raise",
      "music_misses.record_error(" in mmsrc)

# ── reclassify drops the old-category profile ───────────────────────────────
DELETED_KEYS: list = []


class _Conn:
    def execute(self, sql, params=()):
        if sql.startswith("DELETE"):
            DELETED_KEYS.append(params[0])

    def commit(self):
        pass


class _ProfileCache(_NoCache):
    conn = _Conn()

    def get_cache(self, k):
        return {"response": {"plot_summary": "x"}} if k == "enriched:anime:sonarr:42" else None
mcache.MetadataCache = _ProfileCache
cw.get_chroma_db = lambda: _NoChroma()
try:
    errs = []
    ls._recategorize_local(42, "show", errs)
finally:
    mcache.MetadataCache, cw.get_chroma_db = _saved
check("re-filing copies the profile and deletes the old-category key",
      DELETED_KEYS == ["v2:enriched:anime:sonarr:42"] and not errs, (DELETED_KEYS, errs))

# ── audit: a failed requeue gives the finding its automatic chance back ─────
from src.services import enrichment_state as es_mod  # noqa: E402

with session() as s:
    s.add(EnrichmentFinding(plex_rating_key="sonarr:7", kind="wrong_entity", status="pending",
                            severity="warn", detail=json.dumps({"requeued_at": "2026-10-19Z",
                                                                "x": 1})))
    s.commit()
    n = es_mod.unmark_requeued(s, [("sonarr:7", "wrong_entity"), ("nope", "wrong_entity")])
    s.commit()
    row = s.query(EnrichmentFinding).filter_by(plex_rating_key="sonarr:7").first()
    check("unmark_requeued removes requeued_at and keeps the rest of the detail",
          n == 1 and json.loads(row.detail) == {"x": 1} and not es_mod.was_requeued(row))
en_src = (ROOT / "src/routers/enrichment.py").read_text(encoding="utf-8")
check("the audit restores the chance when the purge or the status flip fails",
      en_src.count("await _undo_triage_requeues(e)") == 2)

# ── anime mapping: every AniDB entry survives a save + load ─────────────────
import tempfile  # noqa: E402
import src.services.anime_mapping as am  # noqa: E402

_saved_path = am.MAPPING_DB_PATH
with tempfile.TemporaryDirectory() as td:
    am.MAPPING_DB_PATH = Path(td) / "anime_mapping.db"
    try:
        m = am.AnimeMapping()
        for anidb in (100, 101, 102):         # three seasons, one TVDB series
            m.anidb_to_tvdb[anidb] = 5000
        m.tvdb_to_anidb[5000] = 100           # the parse's own pick
        m.anidb_to_tvdb[200] = 6000
        m.tvdb_to_anidb[6000] = 200
        m.anidb_to_anilist[100] = 7
        m.anidb_to_anilist[101] = 8
        m.total_entries = 4
        am._save_to_db(m)
        back = am._load_from_db()
        check("every season's AniDB entry is kept (one row per AniDB id)",
              set(back.anidb_to_tvdb) == {100, 101, 102, 200}, back.anidb_to_tvdb)
        check("the series lookup resolves to the same entry the parse chose",
              back.tvdb_to_anidb == {5000: 100, 6000: 200}
              and back.tvdb_to_anilist == {5000: 7}, (back.tvdb_to_anidb, back.tvdb_to_anilist))
        # A cache written before is_primary still loads.
        c = sqlite3.connect(am.MAPPING_DB_PATH)
        c.execute("DROP TABLE anime_mapping")
        c.execute("CREATE TABLE anime_mapping (anidb_id INTEGER PRIMARY KEY, tvdb_id INTEGER, "
                  "anilist_id INTEGER)")
        c.execute("INSERT INTO anime_mapping VALUES (100, 5000, 7)")
        c.commit()
        c.close()
        old = am._load_from_db()
        check("an old-format cache still loads", old.tvdb_to_anidb == {5000: 100})
    finally:
        am.MAPPING_DB_PATH = _saved_path

# ── backlog and wish list flag artists the owner deleted ────────────────────
import src.routers.library as lib  # noqa: E402

_saved_tomb = mcs.music_tombstones
mcs.music_tombstones = lambda: ({"mb-nb"}, {norm_name("Beatles, The")})
try:
    is_dead = lib._deleted_artist_check()
    check("deleted-artist check matches by MBID and by normalised name",
          is_dead("Anyone", "mb-nb") and is_dead("beatles, the") and not is_dead("Heat", "mb-x"))

    def _boom():
        raise RuntimeError("db locked")
    mcs.music_tombstones = _boom
    check("an unreadable table flags nothing", lib._deleted_artist_check()("Nickelback") is False)
finally:
    mcs.music_tombstones = _saved_tomb
arr_js = (ROOT / "frontend/js/arr.js").read_text(encoding="utf-8")
lib_src = (ROOT / "src/routers/library.py").read_text(encoding="utf-8")
check("backlog cards and wishes show a 'deleted' badge",
      arr_js.count("DELETED_BADGE : ''") == 2
      and '"deleted":     is_deleted(r.series_title, r.mbid)' in lib_src
      and '"deleted": is_deleted(w.title, w.mbid)' in lib_src)

# ── recommendations say when THIS user's cache was built ────────────────────
rsrc = (ROOT / "src/routers/recommendations.py").read_text(encoding="utf-8")
check("cached_at comes from the user's own rows, not the household-wide stamp",
      'own = max((r["cached_at"] for r in recs if r.get("cached_at")), default=None)' in rsrc)

# ══ Follow-ups (owner decisions 2026-10-20) ═════════════════════════════════

# ── a re-added title's old doc is MERGED into its live twin, never rebuilt ──
import numpy as np  # noqa: E402

drop = cw.drop_merged
res = {"ids": [["radarr:7", "radarr:912", "radarr:3"]],
       "distances": [[0.1, 0.2, 0.3]],
       "documents": [["old", "live", "other"]],
       "metadatas": [[{cw.MERGED_INTO: "radarr:912"}, {}, {}]],
       "embeddings": [np.zeros((3, 4))], "included": ["metadatas"]}
out = drop(res, 2)
check("similarity search hides merged docs and keeps every column aligned",
      out["ids"] == [["radarr:912", "radarr:3"]] and out["distances"] == [[0.2, 0.3]]
      and out["documents"] == [["live", "other"]] and len(out["embeddings"][0]) == 2)
check("...and still returns the page size the caller asked for",
      len(drop(res, 1)["ids"][0]) == 1)


class _Coll:
    def __init__(self):
        self.asked = None

    def query(self, query_embeddings, n_results, where=None):
        self.asked = n_results
        return {"ids": [["a", "b", "c"]], "distances": [[1, 2, 3]],
                "metadatas": [[{}, {cw.MERGED_INTO: "x"}, {}]]}

    def get(self, where=None, include=None, limit=None):
        return {"embeddings": [[1.0], [2.0]], "metadatas": [{}, {cw.MERGED_INTO: "x"}]}


wrapper = cw.ChromaDBWrapper.__new__(cw.ChromaDBWrapper)
wrapper.collection = _Coll()
q = wrapper.query([[0.0]], n_results=2)
check("query() over-fetches by the headroom and trims after hiding merged docs",
      wrapper.collection.asked == 2 + cw._MERGED_HEADROOM and q["ids"] == [["a", "c"]])
check("taste calibration skips merged docs (no title counted twice)",
      [list(r) for r in wrapper.embeddings_for_domain("movie")] == [[1.0]])

import src.services.corpus_repair as crp  # noqa: E402

with session() as s:
    s.add(ArrEnrichmentStatus(service="radarr", arr_id=7, category="movie",
                              title="Dune", tmdb_id=438631))
    s.add(ArrEnrichmentStatus(service="radarr", arr_id=8, category="movie",
                              title="Gone For Good", tmdb_id=1))
    s.commit()


class _PrefetchCache(_NoCache):
    def get_cache(self, k):
        if k == "raw_prefetch:lidarr:5":
            return {"response": {"title": "Solstice", "mbid": "MB-SOL"}}
        return None

    def close(self):
        pass
live_index = {}
for prk, it in {"radarr:912": {"tmdb_id": 438631},
                "lidarr:99": {"mbid": "mb-sol"},
                "sonarr:438631": {"tvdb_id": 438631}}.items():
    for k in crp.identity_keys(prk.split(":")[0], it):
        live_index[k] = prk
mcache.MetadataCache = _PrefetchCache
try:
    tw = asyncio.run(crp.find_live_twins(["radarr:7", "radarr:8", "lidarr:5"], live_index))
finally:
    mcache.MetadataCache = _saved[0]
check("a gone doc is matched to its live twin by tmdb id (status row) and MBID (prefetch)",
      tw == {"radarr:7": "radarr:912", "lidarr:5": "lidarr:99"}, tw)
check("ids never cross services (a film's tmdb id is not a series' tvdb id)",
      "sonarr:438631" not in tw.values())


class _MergeChroma:
    def __init__(self):
        self.updated, self.facets_dropped = [], []

    def get_by_id(self, d):
        return {"id": d, "metadata": {"title": "Dune", "domain": "movie"}}

    def update_metadata(self, d, meta):
        self.updated.append((d, meta))

    def facets_delete_by_parent(self, d):
        self.facets_dropped.append(d)
fake_merge = _MergeChroma()
cw.get_chroma_db = lambda: fake_merge
try:
    ok = asyncio.run(crp.merge_into_live("radarr:7", "radarr:912"))
finally:
    cw.get_chroma_db = _saved[1]
check("merging keeps the doc (metadata marked, nothing deleted) and drops its facets",
      ok and fake_merge.updated[0][1][cw.MERGED_INTO] == "radarr:912"
      and fake_merge.updated[0][1]["title"] == "Dune"
      and fake_merge.facets_dropped == ["radarr:7"])
_walk = en_src = (ROOT / "src/routers/enrichment.py").read_text(encoding="utf-8")
_gone_loop = _walk[_walk.index('for d, dom in bucket["gone"]:'):]
check("the zombie walk merges a twin instead of rebuilding it, and only into a doc that exists",
      _gone_loop.index("if d in twins:") < _gone_loop.index("rebuild_doc_from_prefetch(")
      and "if _prk not in _doc_ids:" in _walk and "md.get(MERGED_INTO)" in _walk)

# ── member keeps hold on the legacy path (no pillar judge) too ──────────────
import src.services.recommendations_engine as reng  # noqa: E402

keeps_map = reng._member_keeps_for(1, [{"title": "Severance"}, {"title": "Owner Only"}], "show")
check("legacy path: a member's keep is found per item",
      keeps_map == {reng._idx_key({"title": "Severance"}): ["ana"],
                    reng._idx_key({"title": "Owner Only"}): []}, keeps_map)


@contextmanager
def _broken():
    class _B:
        def query(self, *a, **k):
            raise RuntimeError("database is locked")
    yield _B()
conn.get_db_session = _broken
try:
    unk = reng._member_keeps_for(1, [{"title": "Severance"}], "show")
finally:
    conn.get_db_session = session
check("legacy path: an unreadable keep is unknown (None), never 'nobody'",
      unk == {reng._idx_key({"title": "Severance"}): None})
eng = (ROOT / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
_legacy = eng[eng.index("# ── LEGACY taste-mismatch pitch path"):]
check("legacy proposals of member-kept titles are 'Your call' with the household guard",
      '"stagnant": bool(_keepers),' in _legacy
      and '{"_guard": "household_floor", "_guard_detail": _keepers[0]}' in _legacy
      and "is None:\n                continue" in _legacy)

conn.get_db_session = _real_session
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
