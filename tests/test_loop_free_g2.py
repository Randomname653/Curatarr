"""Loop-free pins for the deletion, music, library, chat and returns paths.

These coroutines used to run their SQLAlchemy work on the event-loop
thread (a request session held across the arr DELETE, the Lidarr guard,
the chat stream; app_state flags written from the music pipeline). Each
check drives one of them against a temp file database with the loop guard
in raise mode, and checks both that no statement ran on the loop and that
the rows / flags / response came out as before.

  * approve_deletion: the row is deleted, the resolution log written
  * the bulk runner: a protected title is skipped, the other deleted
  * the deletion analysis: proposals saved, the run lock taken and freed;
    the cached read serves them
  * the music pipeline: progress, interrupted flag and lease written
  * chat: a verification claim (and its revert) and a history clear
  * library breakdown (quick) and the daily deleted-items-returned check

External services (arrs, Ollama, SoulSync, Chroma) are stubbed.

    python tests/test_loop_free_g2.py
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402

import _harness  # noqa: E402,F401  (first: temp stores and .env, never the live data/)
import src.database.connection as conn_mod  # noqa: E402
from src.database import loop_guard  # noqa: E402
from src.database.models import (  # noqa: E402
    Base, ConversationMessage, CuratorResolutionLog, DeletionProposal, LibraryConfig,
    ProactiveMessage, ProtectedMedia,
)
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
eng = create_engine(f"sqlite:///{(tmp / 'g2.db').as_posix()}",
                    connect_args={"check_same_thread": False})
Base.metadata.create_all(eng)
conn_mod.SessionLocal.configure(bind=eng)
conn_mod.engine = eng
loop_guard.install(eng)
loop_guard.serving(True)

MINE = ("src/routers/recommendations.py", "src/routers/music.py", "src/routers/library.py",
        "src/routers/chat.py", "src/services/deletion_returns.py")


def guarded(coro_fn):
    """Run coro_fn() under raise mode; (result, error, sites in my files)."""
    loop_guard.reset()
    os.environ["CURATARR_LOOP_GUARD"] = "raise"
    out = err = None
    try:
        out = asyncio.run(coro_fn())
    except Exception as e:  # noqa: BLE001 - reported by the checks
        err = e
    finally:
        os.environ.pop("CURATARR_LOOP_GUARD", None)
    sites = [s["site"] for s in loop_guard.snapshot()["sites"] if s["site"].startswith(MINE)]
    return out, err, sites


def seed(*rows):
    with conn_mod.get_db_session() as db:
        db.add_all(rows)
        db.commit()
        return [r.id for r in rows]


def read(fn):
    with conn_mod.get_db_session() as db:
        return fn(db)


def proposal(title, **kw):
    base = dict(user_id=1, title=title, service="radarr", media_id="7", reason="pitch",
                confidence=0.8, storage_mb=2048, status="pending", category="movie")
    base.update(kw)
    return DeletionProposal(**base)


import src.routers.recommendations as recs  # noqa: E402

deleted_at_arr = []


async def _reachable(service):
    return True


async def _arr_delete(p):
    deleted_at_arr.append(p.title)
    return True


recs._probe_arr = _reachable
recs._execute_arr_delete = _arr_delete
_U = types.SimpleNamespace(id=1, is_admin=True)

# ── 1. single approve (deletion path) ───────────────────────────────────────
print("approve_deletion")
(pid,) = seed(proposal("Approve Me"))
out, err, sites = guarded(lambda: recs.approve_deletion(pid, user=_U))
check(f"approve runs no statement on the loop ({err!r}, {sites})", err is None and not sites)
check("...the arr DELETE went out and the response says deleted",
      deleted_at_arr == ["Approve Me"] and out and out["ok"] and out["status"] == "deleted")
st, logs = read(lambda db: (db.get(DeletionProposal, pid).status,
                            db.query(CuratorResolutionLog).filter_by(title="Approve Me").count()))
check("...the row is deleted and one resolution-log row written", st == "deleted" and logs == 1)

# ── 2. bulk runner: protection re-checked per item ──────────────────────────
print("bulk delete")
import src.services.episodic_memory as em  # noqa: E402
from src.services.task_monitor import task_monitor  # noqa: E402

deleted_at_arr.clear()
keep_id, go_id = seed(proposal("Bulk Protected"), proposal("Bulk Go", media_id="8"))
seed(ProtectedMedia(user_id=1, identifier="Bulk Protected", title="Bulk Protected"))
task = task_monitor.create(name="Bulk delete (g2)", category="curation", total=2)
_, err, sites = guarded(lambda: recs._run_bulk_delete_bg(task, 1, [keep_id, go_id], ""))
check(f"the bulk runner runs no statement on the loop ({err!r}, {sites})",
      err is None and not sites)
statuses = read(lambda db: (db.get(DeletionProposal, keep_id).status,
                            db.get(DeletionProposal, go_id).status))
check("...protected title rejected and untouched at the arr, the other deleted",
      statuses == ("rejected", "deleted") and deleted_at_arr == ["Bulk Go"])
check("...and the run summary says so",
      "1 deleted" in task.logs[-1].message and "1 skipped" in task.logs[-1].message)

# ── 3. deletion analysis (lock + save) and the cached read ──────────────────
print("deletion analysis")
import src.services.recommendations_engine as engine_mod  # noqa: E402


async def _candidates(category=None):
    return [{"title": "Fresh Pick", "service": "radarr", "arr_id": 41, "category": "movie",
             "size_mb": 4096}]


async def _no_imports(svc):
    return {}


async def _gen(user_id, items, category, monitor_task=None):
    return [{"title": "Fresh Pick", "pitch": "never watched", "confidence": 0.7,
             "arr_id": 41, "service": "radarr", "category": "movie", "size_mb": 4096}]


async def _enrich(p, item_map, category):
    return dict(p)


recs._fetch_arr_candidates = _candidates
recs._fetch_arr_recent_imports = _no_imports
recs._enrich_proposal = _enrich
engine_mod.generate_deletion_proposals = _gen
out, err, sites = guarded(lambda: recs.get_deletion_proposals(
    category="movie", refresh=True, recent_only=False, recent_days=7, user=_U))
check(f"Analyze runs no statement on the loop ({err!r}, {sites})", err is None and not sites)
saved = read(lambda db: [p.title for p in db.query(DeletionProposal).filter_by(
    status="pending", title="Fresh Pick")])
check("...the proposal is saved and returned with its id, the deletion_run lock freed",
      saved == ["Fresh Pick"] and out and out["proposals"][0].get("id")
      and app_state.get_state("deletion_run") == "0")
out, err, sites = guarded(lambda: recs.get_deletion_proposals(
    category="movie", refresh=False, recent_only=False, recent_days=7, user=_U))
check(f"the cached read runs no statement on the loop ({err!r}, {sites})",
      err is None and not sites)
check("...and serves the saved proposal", out and [p["title"] for p in out["proposals"]]
      == ["Fresh Pick"] and out["total_gb"] == 4.0 and "enrichment_coverage" in out)

# ── 4. music pipeline flags ─────────────────────────────────────────────────
print("music pipeline")
import src.routers.music as music  # noqa: E402
import src.services.music_matcher as mm  # noqa: E402
from src.services import state_lease  # noqa: E402


async def _phase(user_id, batch=None):
    return {"matched": 3, "unmatched": 1, "resolved": 2, "remaining": 0,
            "enriched_plays": 5, "tracks_queried": 4}


_mm_real = (mm.match_spotify_to_plex, mm.resolve_artist_mbids,
            mm.enrich_music_genres_spotify, mm.enrich_music_genres_lastfm)
mm.match_spotify_to_plex = mm.resolve_artist_mbids = _phase
mm.enrich_music_genres_spotify = mm.enrich_music_genres_lastfm = _phase
try:
    assert state_lease.acquire("music_pipeline_running")   # the endpoint's job
    _, err, sites = guarded(lambda: music._run_music_pipeline(1, 10))
finally:
    (mm.match_spotify_to_plex, mm.resolve_artist_mbids,
     mm.enrich_music_genres_spotify, mm.enrich_music_genres_lastfm) = _mm_real
check(f"the pipeline runs no statement on the loop ({err!r}, {sites})", err is None and not sites)
prog = json.loads(app_state.get_state("music_pipeline_progress") or "{}")
check("...progress reaches 'done' with the phase counts",
      prog.get("phase") == "done" and prog.get("matched") == 3 and prog.get("enriched_plays") == 5)
check("...interrupted cleared, last run stamped, lease released",
      app_state.get_state("music_pipeline_interrupted") == "0"
      and app_state.get_state("music_pipeline_last_run")
      and app_state.get_state("music_pipeline_running") == "0")

# ── 5. chat: verification claim + revert, history clear ─────────────────────
print("chat")
import src.routers.chat as chat  # noqa: E402
import src.services.verification_session as vs  # noqa: E402

(qid,) = seed(ProactiveMessage(user_id=1, trigger_type="verification", read=False,
                               message="Did you finish it?",
                               trigger_data=json.dumps({"title": "Dune"})))
answered = []


async def _not_processed(user_id, text, question):
    answered.append(question.get("title"))
    return False


_vs_real = vs.process_verification_response
vs.process_verification_response = _not_processed
try:
    _, err, sites = guarded(lambda: chat._check_verification_response(
        1, "yes, all of it", f"proactive_message:{qid}"))
finally:
    vs.process_verification_response = _vs_real
check(f"the verification claim runs no statement on the loop ({err!r}, {sites})",
      err is None and not sites)
check("...the question was claimed and handed over, then reverted for a retry",
      answered == ["Dune"] and read(lambda db: db.get(ProactiveMessage, qid).read) is False)

seed(ConversationMessage(user_id=1, role="user", content="a", thread_id="general"),
     ConversationMessage(user_id=1, role="assistant", content="b", thread_id="general"),
     ConversationMessage(user_id=1, role="user", content="c", thread_id="deletion_proposal:9"))


async def _flushed(user_id, thread_id, media_category=None):
    return True


_flush_real = em.flush_thread_extraction
em.flush_thread_extraction = _flushed
try:
    out, err, sites = guarded(lambda: chat.clear_chat_history(
        thread_id="general", force=False, user=_U))
finally:
    em.flush_thread_extraction = _flush_real
check(f"clearing a thread runs no statement on the loop ({err!r}, {sites})",
      err is None and not sites)
left = read(lambda db: [m.thread_id for m in db.query(ConversationMessage).filter_by(user_id=1)])
check("...only that thread is gone", out and out["deleted"] == 2
      and left == ["deletion_proposal:9"])

# ── 6. library breakdown (quick) + the daily returns check ──────────────────
print("library breakdown / deletion returns")
import src.routers.library as lib  # noqa: E402
import src.vector_store.chromadb_wrapper as cw  # noqa: E402
from src.config import settings  # noqa: E402

seed(LibraryConfig(plex_section_key="1", plex_section_title="Films",
                   plex_section_type="movie", media_category="movie"))


class _Chroma:
    def count_by_id_prefix(self, prefix):
        return 0


_saved = (cw.get_chroma_db, settings.PLEX_TOKEN, settings.PLEX_AUTH_TOKEN)
cw.get_chroma_db = lambda: _Chroma()
settings.PLEX_TOKEN, settings.PLEX_AUTH_TOKEN = None, ""   # no Plex token: no section calls
try:
    out, err, sites = guarded(lambda: lib.library_breakdown(user=_U, quick=True))
finally:
    cw.get_chroma_db, settings.PLEX_TOKEN, settings.PLEX_AUTH_TOKEN = _saved
check(f"the breakdown runs no statement on the loop ({err!r}, {sites})", err is None and not sites)
libs = (out or {}).get("libraries") or []
check("...and lists the configured library",
      [(x["category"], x["inventory"]["plex_section"]["title"]) for x in libs]
      == [("movie", "Films")])

import src.services.deletion_returns as dr  # noqa: E402
import src.services.soulsync_client as ss  # noqa: E402
from datetime import datetime  # noqa: E402

(back_id,) = seed(proposal("Came Back", status="deleted", tmdb_id=438631,
                           resolved_at=datetime(2026, 5, 14)))


async def _radarr(service, **kw):
    if service != "radarr":
        raise RuntimeError("not set up")
    return ([{"id": 501, "title": "Came Back", "tmdbId": 438631,
              "added": "2026-05-20T08:00:00Z", "statistics": {"sizeOnDisk": 1048576}}], [], {})


_dr_saved = (lib._fetch_arr_library, ss.writes_allowed)
lib._fetch_arr_library, ss.writes_allowed = _radarr, (lambda: False)
try:
    out, err, sites = guarded(lambda: dr.run_daily())
finally:
    lib._fetch_arr_library, ss.writes_allowed = _dr_saved
check(f"the daily returns check runs no statement on the loop ({err!r}, {sites})",
      err is None and not sites)
info = json.loads(read(lambda db: db.get(DeletionProposal, back_id).returned_info) or "{}")
check("...and records the return on the deleted proposal",
      out and "Came Back" in out["new"] and info.get("kind") == "returned"
      and info.get("media_id") == "501")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
