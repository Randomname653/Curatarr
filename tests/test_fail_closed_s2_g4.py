"""Chat, library, taste and bell: failures that read as "nothing there".

  * a crash while recording a verification answer logged at DEBUG; WARNING
  * arr profile reads that failed came back as [] plus an ``*_error`` no JS
    read, so Settings said "(none configured)"; logged and rendered
  * pick_starters writes but ran via run_read; a Python-side impression +1
    lost a concurrent increment
  * _recategorize_local (read-update-commit) ran on a free worker, not the
    single writer
  * the startup L2->L1 restore could overwrite a fresher live fetch
  * an unreadable rec_watch_hits queue was rewritten from [] (lost up to 50
    queued follow-ups); an unreadable taste blob after a CAS miss was
    force-written over; a crashed Plex lookup said "not in library"
  * frontend: a failed history load showed an empty thread, render bugs hid
    in ``catch {}``, a failed principles read emptied the bell, a failed SWR
    refresh passed for current, and a refused shutdown said "close this tab"

Each behavioural check fails on the code before this change.

    python tests/test_fail_closed_s2_g4.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import (Base, CachedRecommendation, ChatStarter,  # noqa: E402
                                 EncryptedTasteVector, ProactiveMessage, User)
from src.services import app_state  # noqa: E402

Base.metadata.create_all(conn_mod.engine)
with conn_mod.get_db_session() as _db:
    _db.add(User(id=1, plex_user_id="p1", plex_username="owner", is_admin=True))
    _db.commit()

from _checks import check  # noqa: E402


class _Cap(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def at(self, level):
        return [r for r in self.records if r.levelno >= level]


def captured(logger_name):
    cap = _Cap()
    lg = logging.getLogger(logger_name)
    lg.addHandler(cap)
    old = lg.level
    lg.setLevel(logging.DEBUG)
    return cap, lambda: (lg.removeHandler(cap), lg.setLevel(old))


def _src(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _js_fn(src, name):
    i = src.index(f"function {name}(")
    j = src.find("\n}\n", i)
    return src[i:j + 2]


# ── 1. verification answer crash ────────────────────────────────────────────
print("verification answer crash")
import src.routers.chat as chat  # noqa: E402
import src.services.verification_session as vs  # noqa: E402

with conn_mod.get_db_session() as db:
    pm_row = ProactiveMessage(user_id=1, trigger_type="verification", message="Q?",
                              trigger_data=json.dumps({"title": "Dark"}), read=False)
    db.add(pm_row)
    db.commit()
    pm_id = pm_row.id


async def _boom(*a, **k):
    raise RuntimeError("summariser exploded")

_real_pvr = vs.process_verification_response
vs.process_verification_response = _boom
cap, undo = captured("src.routers.chat")
try:
    asyncio.run(chat._check_verification_response(1, "yes, loved it", f"proactive_message:{pm_id}"))
finally:
    vs.process_verification_response = _real_pvr
    undo()
with conn_mod.get_db_session() as db:
    still_unread = db.get(ProactiveMessage, pm_id).read is False
check("a crash recording the answer is a WARNING with a traceback",
      any("Verification response check failed" in r.getMessage() and r.exc_info
          for r in cap.at(logging.WARNING)))
check("...and the question is re-armed", still_unread)

# ── 2. arr profiles: errors logged and rendered ────────────────────────────
print("arr profiles")
import src.routers.library as lib  # noqa: E402


class _FakeArr:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def list_root_folders(self):
        return [{"path": "/movies"}]

    async def list_quality_profiles(self):
        raise RuntimeError("API error 500: boom")

    async def list_tags(self):
        return []

_saved = (lib._make_client, lib._get_arr_url_key)
lib._make_client = lambda svc, url, key: _FakeArr()
lib._get_arr_url_key = lambda svc: ("http://radarr.lan", "k")
cap, undo = captured("src.routers.library")
try:
    out = asyncio.run(lib.library_profiles("radarr", _user=None))
finally:
    lib._make_client, lib._get_arr_url_key = _saved
    undo()
check("a failed profile read keeps its error field",
      out["quality_profiles"] == [] and "boom" in out.get("quality_profiles_error", "")
      and out["root_folders"] == [{"path": "/movies"}])
check("...and is a WARNING on the server",
      any("quality_profiles" in r.getMessage() for r in cap.at(logging.WARNING)))
ls_js = _js_fn(_src("frontend/js/library_settings.js"), "loadArrProfiles")
check("loadArrProfiles renders the *_error fields and disables the dropdown",
      "_error`]" in ls_js and "couldn't read from" in ls_js
      and "dis('quality_profiles')" in ls_js and "dis('root_folders')" in ls_js)

# ── 3. starters: written on the writer, increment in SQL ───────────────────
print("chat starters")
import src.services.chat_starters as cs  # noqa: E402

with conn_mod.get_db_session() as db:
    st = ChatStarter(user_id=1, text="Pick a lane tonight.", form="challenge",
                     daypart="any", impressions=0)
    db.add(st)
    db.commit()
    st_id = st.id

_real_named = cs._named_day
_bumped = []


def _named_day_and_a_second_tab(t):
    # another request counts its impression between our read and our commit
    if not _bumped:
        _bumped.append(1)
        with conn_mod.engine.begin() as c:
            c.execute(text("UPDATE chat_starters SET impressions = impressions + 1 WHERE id = :i"),
                      {"i": st_id})
    return _real_named(t)

cs._named_day = _named_day_and_a_second_tab
try:
    picked = cs.pick_starters(1, limit=3)
finally:
    cs._named_day = _real_named
with conn_mod.get_db_session() as db:
    n_imp = db.get(ChatStarter, st_id).impressions
check(f"two concurrent impressions both count (impressions={n_imp})",
      [p["id"] for p in picked] == [st_id] and n_imp == 2)

_calls = []


async def _rec_read(fn, *a, **k):
    _calls.append(("read", getattr(fn, "__name__", "")))
    return fn(*a, **k)


async def _rec_write(fn, *a, **k):
    _calls.append(("write", getattr(fn, "__name__", "")))
    return fn(*a, **k)

import src.services.process_monitor as pmon  # noqa: E402
_saved = (chat.run_read, chat.run_write, cs.pick_starters, cs.pool_fresh_count, pmon.is_game_running)


def pick_starters(uid, limit=3):
    return [{"id": 1, "text": "hi"}]

chat.run_read, chat.run_write = _rec_read, _rec_write
cs.pick_starters, cs.pool_fresh_count = pick_starters, (lambda uid: 99)
pmon.is_game_running = lambda: False
try:
    asyncio.run(chat.get_starters(user=SimpleNamespace(id=1)))
finally:
    chat.run_read, chat.run_write, cs.pick_starters, cs.pool_fresh_count, pmon.is_game_running = _saved
check(f"get_starters runs pick_starters on the writer ({_calls})",
      ("write", "pick_starters") in _calls and ("read", "pick_starters") not in _calls)

# ── 4. library_sorting: re-file on the single writer ───────────────────────
print("library_sorting")
import src.services.library_sorting as lsort  # noqa: E402
from src.config import settings  # noqa: E402

_threads = []


def _local(sid, new_cat, errors=None):
    _threads.append(threading.current_thread().name)
    return "show"


def _sonarr(req):
    if req.method == "GET":
        return httpx.Response(200, json={"id": 5, "title": "Frieren",
                                         "seriesType": "standard", "genres": []})
    return httpx.Response(202, json=[])

_RealClient = httpx.AsyncClient
_fake_cw = types.ModuleType("src.vector_store.chromadb_wrapper")


def _no_chroma():
    raise RuntimeError("no chroma in tests")
_fake_cw.get_chroma_db = _no_chroma


async def _run_chroma(fn, *a, **k):
    return fn(*a, **k)
_fake_cw.run_chroma = _run_chroma
_cw = sys.modules.get("src.vector_store.chromadb_wrapper")
sys.modules["src.vector_store.chromadb_wrapper"] = _fake_cw
_saved = (settings.SONARR_URL, settings.SONARR_API_KEY, lsort._recategorize_local)
settings.SONARR_URL, settings.SONARR_API_KEY = "http://sonarr.lan", "k"
lsort._recategorize_local = _local
httpx.AsyncClient = lambda *a, **k: _RealClient(
    *a, transport=httpx.MockTransport(_sonarr), **{x: y for x, y in k.items() if x != "transport"})
try:
    asyncio.run(lsort.apply_reclassify([{"sonarr_id": 5, "fix": {"seriesType": "anime"}}]))
finally:
    httpx.AsyncClient = _RealClient
    settings.SONARR_URL, settings.SONARR_API_KEY, lsort._recategorize_local = _saved
    if _cw is not None:
        sys.modules["src.vector_store.chromadb_wrapper"] = _cw
    else:
        sys.modules.pop("src.vector_store.chromadb_wrapper", None)
check(f"_recategorize_local runs on the db-writer thread ({_threads})",
      len(_threads) == 1 and _threads[0].startswith("db-writer"))

# ── 5. prewarm: a fresher live fetch survives the L2 restore ───────────────
print("arr cache prewarm")


class _MC:
    def get_cache(self, key):
        return {"response": {"items_raw": [{"id": "old"}], "tags": [], "at": time.time() - 3600}}

    def close(self):
        pass

import src.cache.metadata_cache as mcmod  # noqa: E402
_saved = (mcmod.MetadataCache, lib._get_arr_url_key)
mcmod.MetadataCache = _MC
lib._get_arr_url_key = lambda svc: ("http://x", "k") if svc == "sonarr" else (None, None)
lib._LIB_CACHE.clear()
lib._LIB_CACHE["sonarr"] = {"items_raw": [{"id": "live"}], "tags": [], "at": time.time()}
try:
    restored = lib._load_lib_cache_from_db()
    kept = lib._LIB_CACHE["sonarr"]["items_raw"] == [{"id": "live"}]
    lib._LIB_CACHE.clear()
    restored_empty = lib._load_lib_cache_from_db()
    filled = lib._LIB_CACHE.get("sonarr", {}).get("items_raw") == [{"id": "old"}]
finally:
    mcmod.MetadataCache, lib._get_arr_url_key = _saved
    lib._LIB_CACHE.clear()
check("the L2 restore does not overwrite a fresher live fetch", kept and restored == 0)
check("...and still fills an empty L1", filled and restored_empty == 1)

# ── 6. rec_watch_hits: an unreadable queue is not rewritten ────────────────
print("rec_watch_hits queue")
import src.services.plex_sync as ps  # noqa: E402

with conn_mod.get_db_session() as db:
    db.add(CachedRecommendation(user_id=1, category="show", lane="library", title="Dark"))
    db.commit()
_hit = {"user_id": 1, "title": "Dark", "series_title": "Dark", "identity_key": None,
        "tmdb_id": None, "viewed_at": "2099-01-01T00:00:00"}
_key = "rec_watch_hits:user_id=1"
app_state.set_state(_key, "[{broken")
cap, undo = captured("src.services.plex_sync")
try:
    try:
        ps._process_rec_watch_hits([_hit])
    except Exception as e:   # the old code may crash past the overwrite
        print("    (after the write:", type(e).__name__, e, ")")
finally:
    undo()
check("an unreadable queue is left as it is, not rewritten from []",
      app_state.get_state(_key) == "[{broken")
check("...with a WARNING", any(_key in r.getMessage() for r in cap.at(logging.WARNING)))

# ── 7. taste blob: no force-write over an unreadable blob ──────────────────
print("taste blob CAS retry")
import src.services.taste_engine as te  # noqa: E402
import src.services.taste_vectors as tv  # noqa: E402

with conn_mod.get_db_session() as db:
    etv = EncryptedTasteVector(user_id=1, media_category="movie", salt="unencrypted",
                               encrypted_blob=json.dumps({"version": 0, "rev": 1,
                                                          "explicit_feedback": ["x"]}))
    db.add(etv)
    db.commit()
    etv_id = etv.id

_cas_calls = []


def _cas_miss(db, etv_id_, old_rev, blob):
    # a concurrent merger rewrites the blob (here: into something unreadable)
    if not _cas_calls:
        with conn_mod.engine.begin() as c:
            c.execute(text("UPDATE encrypted_taste_vectors SET encrypted_blob = '{corrupt' "
                           "WHERE id = :i"), {"i": etv_id_})
    _cas_calls.append(old_rev)
    return False

_real_cas = tv.cas_update_blob
tv.cas_update_blob = _cas_miss
cap, undo = captured("src.services.taste_engine")
try:
    te._store_taste_blobs(1, {"movie": {"embedding": [0.1, 0.2], "watch_count": 3}},
                          ["[MOVIE] x"], "[MOVIE] x", calibrations={"movie": {}})
finally:
    tv.cas_update_blob = _real_cas
    undo()
with conn_mod.get_db_session() as db:
    blob_now = db.get(EncryptedTasteVector, etv_id).encrypted_blob
check(f"an unreadable blob after a CAS miss is not force-written over ({blob_now[:20]!r})",
      blob_now == "{corrupt" and _cas_calls == [1])
check("...with a WARNING", any("unreadable after a CAS" in r.getMessage()
                               for r in cap.at(logging.WARNING)))

# ── 8. wishes: a crashed Plex lookup is unknown, not "not in library" ─────
print("wishes in_library")
import src.services.lyrics as lyr  # noqa: E402


def _lookup_down(**k):
    raise RuntimeError("database disk image is malformed")

_real_lookup = lyr.plex_artist_lookup
lyr.plex_artist_lookup = _lookup_down
lib._wish_lookup_warned_at = 0.0
cap, undo = captured("src.routers.library")
try:
    got = lib._wish_in_library("Low", None)
finally:
    lyr.plex_artist_lookup = _real_lookup
    undo()
check(f"a failed lookup answers None, not False ({got!r})", got is None)
check("...with a WARNING", any("in_library unknown" in r.getMessage()
                               for r in cap.at(logging.WARNING)))
lyr.plex_artist_lookup = lambda **k: None
try:
    check("a lookup that works still answers False", lib._wish_in_library("Low", None) is False)
finally:
    lyr.plex_artist_lookup = _real_lookup
for f in ("arr.js", "recs.js"):
    js = _src(f"frontend/js/{f}")
    check(f"{f} never renders a falsy in_library as 'not in library'",
          "not in library" not in js.lower())

# ── 9. frontend: failures say so ────────────────────────────────────────────
print("frontend")
chat_js = _src("frontend/js/chat.js")
load = _js_fn(chat_js, "_loadThread")
show = _js_fn(chat_js, "_showThread")
restore = _js_fn(chat_js, "restoreChatHistory")
row = _js_fn(chat_js, "_historyErrorRow")
check("a failed history load is null (unknown), not []",
      "return null" in load and "return []" not in load and "console.warn" in load)
check("...and _showThread / the boot restore render an error row with Retry",
      "history === null" in show and "_historyErrorRow(" in show
      and "history === null" in restore and "_historyErrorRow(" in restore
      and "Retry" in row and "addEventListener('click'" in row)
check("chat.js background calls no longer swallow (flush toasts, others warn)",
      ".catch(() => {})" not in chat_js
      and "toast(" in _js_fn(chat_js, "_flushThreadMemories")
      and chat_js.count("_flushThreadMemories(tid);") == 2)

act_js = _src("frontend/js/activity.js")
onmsg = act_js[act_js.index("es.onmessage"):act_js.index("es.onerror")]
check("activity: only JSON.parse is guarded, render bugs reach console.error",
      "catch {}" not in onmsg and "console.error" in onmsg
      and onmsg.index("JSON.parse") < onmsg.index("return;") < onmsg.index("render(tasks)"))

bell = _src("frontend/js/notifications.js")
check("bell: a failed principles read keeps the last known list",
      "principles = _lastPrinciples;" in bell and "principles = _lastPrinciples = " in bell)
check("bell: mark-read failures are logged",
      ".catch(()=>{})" not in bell and ".catch(() => {})" not in bell)

api_js = _src("frontend/js/api.js")
swr = _js_fn(api_js, "_swrRun")
check("SWR: a failed refresh warns, flags the key stale and fires swr-stale",
      "console.warn" in swr and "_swrStale.add(key)" in swr and "'swr-stale'" in swr
      and "_swrStale.delete(key)" in swr and "export function _swrIsStale" in api_js)

cur = _src("frontend/js/curation.js")
sd = _js_fn(cur, "shutdownServer")
check("shutdown: an HTTP error toasts and returns before the overlay",
      "e.status" in sd and "toast(" in sd
      and sd.index("e.status") < sd.index("return;", sd.index("e.status")) < sd.index("sd-overlay"))

game = _src("frontend/js/game.js")
poll = _js_fn(game, "_pollGameProcesses")
check("game poll: failures warn instead of vanishing",
      "catch(_) {}" not in poll and ".catch(() => null)" not in poll
      and poll.count("console.warn") == 2)

check.done()
