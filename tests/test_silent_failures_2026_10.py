"""Silent-failure sweep 2026-10-15 (docs/SILENT_FAILURE_SWEEP_2026_10.md).

Each check fails on the code before the sweep:
  * music_listening_stats answered None ("never played", the strongest CUT
    signal) when its read failed; the judge's evidence now says UNKNOWN and
    the deletion loop defers the title
  * the memory extractor read ORM rows after their session closed
    (DetachedInstanceError, swallowed at DEBUG): no discussion thread got its
    anchor line and recommendation follow-up verdicts never reached the
    feedback analyzer
  * the game watcher evicted VRAM only after the lane probe and the flag
    write succeeded; _gaming() answered "no game" when the flag was unreadable
  * service URLs: inet_aton spellings and IPv4-mapped IPv6 of the metadata
    address passed validate_service_url
  * a Discogs stream cut short (game, corrupt data) replaced the complete
    styles DB with a partial one
  * a MusicBrainz outage was recorded as "no match" (parked for 14-90 days)
  * deletion scoring: the keep-cooldown, curator-stance, recent-watch and
    artist-rating vetoes, the listening-depth map and the keep-considerations
    fail closed
  * frontend: four self-referential CSS tokens; the bulk-delete poll swallowed
    every error; a failed setup-status check opened the Setup wizard

    python tests/test_silent_failures_2026_10.py
"""
import asyncio
import inspect
import json
import re
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import _harness  # noqa: E402,F401  (first: temp stores and .env, never the live data/)
import src.database.connection as conn  # noqa: E402
import src.services.app_state as app_state  # noqa: E402
import src.services.bg_tasks as bg_tasks  # noqa: E402
import src.services.discogs_offline as discogs  # noqa: E402
import src.services.episodic_memory as em  # noqa: E402
import src.services.llm_lane as llm_lane  # noqa: E402
import src.services.pillars as pillars  # noqa: E402
import src.services.process_monitor as pm  # noqa: E402
import src.services.recommendations_engine as reng  # noqa: E402
import src.services.scheduler as sched  # noqa: E402
import src.services.watch_status as ws  # noqa: E402
from src.database.models import Base, ConversationMessage, DeletionProposal, ProactiveMessage  # noqa: E402
from src.services.endpoint_policy import validate_service_url  # noqa: E402

PASS = FAIL = 0


from _checks import real_state  # noqa: E402


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


from _checks import memory_engine, with_users  # noqa: E402
engine = memory_engine()
Base.metadata.create_all(engine)
with_users(engine, 1, 7)
# The real factory's default: commit expires every loaded object, close
# detaches them - exactly what made the post-session reads raise.
Session = sessionmaker(bind=engine)


@contextmanager
def _session():
    db = Session()
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def _locked_session():
    raise OperationalError("SELECT", {}, Exception("database is locked"))
    yield  # pragma: no cover


# ── watch_status / pillars: unreadable is not "never played" ────────────────

def test_listening_stats_raise_when_unreadable():
    real = conn.get_db_session
    conn.get_db_session = _locked_session
    try:
        try:
            ws.music_listening_stats(1, "Some Artist")
            raised = False
        except OperationalError:
            raised = True
    finally:
        conn.get_db_session = real
    check("music_listening_stats raises on a failed read (was: None = never played)", raised)

    src = inspect.getsource(pillars.build_evidence)
    i = src.index("music_listening_stats(")
    handler = src[src.index("except Exception", i):][:400]
    check("build_evidence marks an unreadable listening record UNKNOWN (deferred)",
          'flags["owner_watch_unknown"] = True' in handler and "UNKNOWN" in handler)


# ── episodic memory: anchor line + follow-up bridge ─────────────────────────

def test_thread_anchor_reads_inside_session():
    em.get_db_session = _session
    conn.get_db_session = _session
    with _session() as db:
        msg = ProactiveMessage(user_id=7, trigger_type="recommendation_followup",
                               trigger_data=json.dumps({"title": "Arrival", "category": "movie"}),
                               message="You watched Arrival - how was it?")
        dp = DeletionProposal(user_id=7, title="Old Show", category="show",
                              service="sonarr", media_id="12")
        db.add_all([msg, dp])
        db.flush()
        pm_id, dp_id = msg.id, dp.id
        db.add_all([
            ConversationMessage(user_id=7, role="user", content="Loved the ending",
                                thread_id=f"proactive_message:{pm_id}"),
            ConversationMessage(user_id=7, role="user", content="Never watch it anymore",
                                thread_id=f"deletion_proposal:{dp_id}"),
        ])

    prompts, feedback, state = [], [], real_state()   # the real app_state table

    async def _fake_run(user_id, prompt, media_category=None, quiet_s=0.0):
        prompts.append(prompt)
        return True

    async def _fake_feedback(user_id, title, category, user_msgs):
        feedback.append((title, category))

    def _fake_track(coro, name=None):
        feedback.append(("tracked", name))
        coro.close()

    saved = (em._run_memory_extraction, em.analyze_recommendation_feedback,
             app_state.get_state, app_state.set_state, bg_tasks.track_task)
    em._run_memory_extraction = _fake_run
    em.analyze_recommendation_feedback = _fake_feedback
    bg_tasks.track_task = _fake_track
    try:
        asyncio.run(em.extract_memories_from_thread(7, f"proactive_message:{pm_id}"))
        asyncio.run(em.extract_memories_from_thread(7, f"deletion_proposal:{dp_id}"))
    finally:
        (em._run_memory_extraction, em.analyze_recommendation_feedback,
         app_state.get_state, app_state.set_state, bg_tasks.track_task) = saved

    check("proactive thread: the anchor names the opening message",
          len(prompts) == 2 and "You watched Arrival" in prompts[0])
    check("deletion thread: the anchor names the title",
          len(prompts) == 2 and "the deletion of 'Old Show' (show)" in prompts[1])
    check("recommendation follow-up verdict is handed to the feedback analyzer",
          ("tracked", f"rec_feedback:proactive_message:{pm_id}") in feedback)


# ── game mode fails closed ──────────────────────────────────────────────────

def test_game_guards_fail_closed():
    real = app_state.get_state

    def _boom(_k):
        raise OperationalError("SELECT", {}, Exception("database is locked"))

    app_state.get_state = _boom
    try:
        check("_gaming() treats an unreadable flag as a game", sched._gaming() is True)
    finally:
        app_state.get_state = real

    evicted = []

    async def _unload():
        evicted.append(1)
        return ["curator"]

    def _lane_boom():
        raise RuntimeError("psutil probe broke")

    saved = (pm.is_game_running, pm.unload_llm_models, llm_lane.status)
    pm.is_game_running = lambda: True
    pm.unload_llm_models = _unload
    llm_lane.status = _lane_boom
    try:
        asyncio.run(sched.job_game_watcher())
    finally:
        pm.is_game_running, pm.unload_llm_models, llm_lane.status = saved
    check("game watcher evicts VRAM even when the lane probe fails", evicted == [1])


# ── service URL policy ──────────────────────────────────────────────────────

def test_url_policy_spellings():
    refused = ["http://[::ffff:169.254.169.254]", "http://[::ffff:a9fe:a9fe]",
               "http://2852039166", "http://0xa9fea9fe", "http://169.254.169.254.",
               "http://169.254.43518", "http://0", "http://10.1"]
    allowed = ["http://192.168.1.5:32400", "http://localhost:7878", "http://[::1]:8000",
               "http://plex.local:32400", "http://nas:8989", "https://1plex.example.com",
               "http://127.0.0.1:11434"]
    for u in refused:
        try:
            validate_service_url(u)
            ok = False
        except ValueError:
            ok = True
        check(f"refused: {u}", ok)
    for u in allowed:
        try:
            validate_service_url(u)
            ok = True
        except ValueError:
            ok = False
        check(f"allowed: {u}", ok)


# ── Discogs: a cut-short stream keeps the complete DB ───────────────────────

def test_discogs_partial_stream_keeps_db():
    tmp = Path(tempfile.mkdtemp()) / "discogs_styles.db"
    con = sqlite3.connect(tmp)
    con.execute("CREATE TABLE artist_styles (artist_norm TEXT, styles TEXT)")
    con.execute("INSERT INTO artist_styles VALUES ('crypton', '[\"Frenchcore\"]')")
    con.commit()
    con.close()

    saved = (discogs.STYLES_DB_PATH, discogs._latest_masters_key,
             discogs._library_artists, httpx.AsyncClient, discogs._gaming)
    real_client = httpx.AsyncClient
    discogs._gaming = lambda: False
    discogs.STYLES_DB_PATH = tmp
    discogs._latest_masters_key = lambda: "discogs_masters.xml.gz"
    discogs._library_artists = lambda: {"crypton": (set(), set())}
    httpx.AsyncClient = lambda **kw: real_client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=b"definitely not gzip " * 64)))
    try:
        r = asyncio.run(discogs.refresh_discogs_styles(force=True))
    finally:
        (discogs.STYLES_DB_PATH, discogs._latest_masters_key,
         discogs._library_artists, httpx.AsyncClient, discogs._gaming) = saved
    con = sqlite3.connect(tmp)
    n = con.execute("SELECT COUNT(*) FROM artist_styles").fetchone()[0]
    con.close()
    check("a corrupt stream reports an error (the task stays due)", "error" in r)
    check("a corrupt stream leaves the existing styles DB intact", n == 1)


# ── source-level pins for paths too wide to drive here ──────────────────────

def test_source_pins():
    from src.services import music_matcher
    mm = inspect.getsource(music_matcher)
    check("MusicBrainz TRANSIENT is not recorded as a miss",
          "if profile is TRANSIENT:" in mm
          and mm.index("if profile is TRANSIENT:") < mm.index('reason="musicbrainz: no match"'))

    gen = inspect.getsource(reng.generate_deletion_proposals)
    for label in ("keep-cooldown", "curator stances", "recent-watch veto",
                  "Plex artist ratings"):
        check(f"deletion veto '{label}' fails closed",
              f'guard_failures.append("{label}")' in gen)
    check("deletion run stops when a veto is unreadable",
          'if _ctx["guard_failures"]:' in gen)
    check("listening-depth map failure stops the music run",
          "could not read listening depth" in gen)
    check("keep-considerations failure defers instead of scoring unprotected",
          'cand["considerations_failed"] = True' in gen and "cons = []" not in gen)

    score = inspect.getsource(reng.score_arr_items)
    vs = score[score.index("def _vector_score"):score.index("vectored: list = []")]
    check("_vector_score has no blanket except", "except Exception" not in vs)

    recs = inspect.getsource(reng.generate_recommendations)
    check("cluster-hint block no longer ends in `except Exception: pass`",
          not re.search(r"except Exception:\s*\n\s*pass", recs))
    check("discovery parse no longer wraps partition() in a DEBUG catch",
          "Recommendation parse error" not in recs)


# ── frontend ────────────────────────────────────────────────────────────────

def test_frontend():
    css = (ROOT / "frontend" / "css" / "app.css").read_text(encoding="utf-8")
    selfref = re.findall(r"--([\w-]+)\s*:\s*var\(--\1\)", css)
    check(f"no self-referential CSS custom property ({selfref})", not selfref)

    dl = (ROOT / "frontend" / "js" / "deletions.js").read_text(encoding="utf-8")
    body = dl[dl.index("export async function bulkDelete"):]
    body = body[:body.index("\nexport ")]
    poll = body[body.index("const poll"):]
    check("bulk-delete poll does not swallow errors", "catch {}" not in poll)
    check("bulk-delete poll gives up and says so", "Lost track of the bulk delete" in body)

    app = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    check("a failed setup-status check is not a first run",
          ".catch(()=>({first_run:true}))" not in app.replace(" ", ""))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            fn()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
