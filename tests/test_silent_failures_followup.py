"""Silent-failure sweep, follow-up (docs/SILENT_FAILURE_SWEEP_2026_10.md §3).

Each check fails on the code before this change:
  * a mid-session 401 ends the session once, everywhere (api.js), and the
    task stream / bell say so instead of failing forever in silence
  * the judge's evidence: unreadable feedback, keep-reasons, principles or
    verified data defer the title (owner_signal_unknown,
    significance_unchecked) instead of judging without them
  * enrichment: Last.fm / OpenSubtitles / Plex outages are transient, not
    "no data"; a vector that never landed is retried from the cache; walker
    crashes are logged as crashes
  * deletion: a failed outcome commit no longer poisons the rest of a bulk
    run; a Plex delete whose check cannot answer is limbo, not error/success
  * AppSec leftovers: names resolving to link-local refused; bounded Spotify
    uploads; no library/plex.tv text in auth errors
  * concurrency: re-attribution off the loop in chunked writes; the music
    runner's lease survives a server restart

    python tests/test_silent_failures_followup.py
"""
import asyncio
import inspect
import json
import logging
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from pydantic import SecretStr  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.database.connection as conn  # noqa: E402
import src.services.app_state as app_state  # noqa: E402
from src.config import settings  # noqa: E402
from src.database.models import Base, DeletionProposal, User, WatchHistoryEntry  # noqa: E402

PASS = FAIL = 0
_JS = ROOT / "frontend" / "js"


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


# ── 401 ends the session (api.js) ───────────────────────────────────────────

def test_401_ends_the_session():
    api = (_JS / "api.js").read_text(encoding="utf-8")
    node = shutil.which("node")
    if not node:
        print("  (node not installed - checked statically)")
        check("api() hands a mid-session 401 to sessionEnded",
              "r.status === 401 && _endsSession(path)" in api)
        return
    body = api[:api.index("// ── SWR VIEW CACHE")]   # api() + the session-end block
    body = "\n".join(l for l in body.splitlines() if not l.startswith("import "))
    body = body.replace("export ", "")
    js = """
const state = {token: 'tok'};
const API = '';
const confirmDialog = async () => ({ok: false});
const store = {}; let reloads = 0;
globalThis.localStorage = {setItem(){}, removeItem(k){ store['ls-' + k] = 'removed'; }};
globalThis.sessionStorage = {getItem(){ return null; }, setItem(k, v){ store[k] = v; }};
globalThis.location = {reload(){ reloads++; }};
let status = 401;
globalThis.fetch = async (path) => ({
  status, ok: status < 300,
  headers: {get(){ return null; }},
  clone(){ return this; },
  text: async () => JSON.stringify({detail: 'Session revoked - please sign in again'}),
  json: async () => ({}),
});
%s
(async () => {
  const out = {};
  try { await api('/api/setup/status'); } catch (e) {}
  out.setupReloads = reloads;
  try { await api('/api/auth/status'); } catch (e) {}
  out.authReloads = reloads;
  try { await api('/api/messages/unread'); } catch (e) { out.status = e.status; }
  try { await api('/api/tasks/'); } catch (e) {}
  out.reloads = reloads; out.token = state.token;
  out.reason = store['curatarr_session_ended'];
  status = 500;
  try { await api('/api/x'); } catch (e) {}
  out.after500 = reloads;
  console.log(JSON.stringify(out));
})();
""" % body
    r = subprocess.run([node, "-e", js], capture_output=True, text=True, encoding="utf-8")
    out = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {}
    if r.returncode != 0:
        print(r.stderr[:600])
    check("a 401 from the setup gate or the auth routes does not end the session",
          out.get("setupReloads") == 0 and out.get("authReloads") == 0)
    check("a mid-session 401 clears the token and reloads ONCE",
          out.get("reloads") == 1 and out.get("token") == "")
    check("...the caller still gets the error", out.get("status") == 401)
    check("...and the sign-in screen gets the reason",
          out.get("reason") == "Session revoked - please sign in again")
    check("a 500 does not end the session", out.get("after500") == 1)

    app = (_JS / "app.js").read_text(encoding="utf-8")
    act = (_JS / "activity.js").read_text(encoding="utf-8")
    bell = (_JS / "notifications.js").read_text(encoding="utf-8")
    check("the sign-in screen shows why the session ended",
          "SESSION_ENDED_KEY" in app and "sign in again" in app)
    check("task stream: a 401 ends the session instead of returning silently",
          "if (r.status === 401) { sessionEnded(" in act)
    check("task stream: ticket failures back off and say the stream is down",
          "3000 * 2 ** (n - 1)" in act and "_showStreamDown(" in act)
    check("bell: repeated failures are shown, not swallowed",
          "_bellFails" in bell and "catch {}\n}" not in bell)
    check("bell: a failed system read keeps the Settings count (no false 0)",
          "if (sysOk) setBadge('settings-badge'" in bell)


# ── judge evidence ──────────────────────────────────────────────────────────

def test_owner_signals_unknown_defer():
    import src.services.pillars as pillars
    import src.services.recommendations_engine as reng
    src = inspect.getsource(pillars.build_evidence)
    for what in ("owner-feedback failed", "owner-signals failed",
                 "principles retrieval failed"):
        i = src.index(what)
        check(f"build_evidence: '{what}' marks owner_signal_unknown",
              'flags["owner_signal_unknown"] = True' in src[i:i + 200])
    i = src.index("verified-data failed")
    check("build_evidence: a verified-data failure is significance_unchecked",
          'flags["significance_unchecked"] = True' in src[i:i + 250])
    check("an unreadable feedback blob is reported, not read as empty",
          'out["fb_unknown"] = True' in inspect.getsource(pillars._read_db_facts))
    gen = inspect.getsource(reng.generate_deletion_proposals)
    loop = gen[gen.index("signal_unknown_skipped = 0"):]
    check("the deletion loop defers owner_signal_unknown before judging",
          loop.index('_fl.get("owner_signal_unknown")') < loop.index("await adjudicate("))


# ── enrichment: transient is not "no data" ──────────────────────────────────

def test_lastfm_transient():
    from src.services import music_matcher as mm

    def resp(status, body):
        return httpx.Response(status, content=body if isinstance(body, bytes)
                              else json.dumps(body).encode())

    def outcome(r):
        try:
            mm._lastfm_json(r)
            return "answer"
        except mm._LastfmTransient:
            return "transient"

    check("Last.fm error 29 (rate limit) under HTTP 200 is transient",
          outcome(resp(200, {"error": 29, "message": "Rate limit"})) == "transient")
    check("Last.fm error 10 (bad key) is transient",
          outcome(resp(403, {"error": 10})) == "transient")
    check("Last.fm HTML error page is transient", outcome(resp(502, b"<html>")) == "transient")
    check("Last.fm error 6 (not found) is an answer",
          outcome(resp(200, {"error": 6, "message": "Track not found"})) == "answer")
    check("Last.fm tags are an answer", outcome(resp(200, {"track": {}})) == "answer")
    src = inspect.getsource(mm)
    i = src.index("except (_LastfmTransient, httpx.HTTPError) as exc:", src.index("artist.getTopTags"))
    check("a transient artist.getTopTags failure is not written as genres=''",
          "no_data_keys.add" not in src[i:i + 500])


def test_subtitle_quota_is_transient():
    from src.services import subtitle_signals as ss
    state = {}
    saved = (ss._os_conf, app_state.get_state, app_state.set_state, httpx.AsyncClient)
    real_client = httpx.AsyncClient
    ss._os_conf = lambda: ("key", "", "", 0)
    app_state.get_state = lambda k: state.get(k)
    app_state.set_state = lambda k, v: state.__setitem__(k, v)
    status = {"code": 406}
    httpx.AsyncClient = lambda **kw: real_client(transport=httpx.MockTransport(
        lambda req: httpx.Response(status["code"], json={"message": "x"})))
    try:
        r406 = asyncio.run(ss.fetch_opensubtitles("tt0111161"))
        blocked = ss._os_blocked_today()
        r_after = asyncio.run(ss.fetch_opensubtitles("tt0111161"))
        state.clear()
        status["code"] = 404
        r404 = asyncio.run(ss.fetch_opensubtitles("tt0111161"))
        status["code"] = 401
        r401 = asyncio.run(ss.fetch_opensubtitles("tt0111161"))
    finally:
        ss._os_conf, app_state.get_state, app_state.set_state, httpx.AsyncClient = saved
    check("OpenSubtitles 406 (quota) is transient, not 'no subtitles'", r406 is None)
    check("...and pauses OpenSubtitles for the day", blocked and r_after is None)
    check("OpenSubtitles 404 is a real 'nothing on file'", r404 == "")
    check("OpenSubtitles 401 is transient", r401 is None)


def test_vector_retried_and_crashes_logged():
    from src.services import media_enricher as me
    import src.embeddings.embedding_generator as eg

    class _Gen:
        async def generate_embedding(self, text):
            return None

        async def close(self):
            pass

    real = eg.EmbeddingGenerator
    eg.EmbeddingGenerator = _Gen
    try:
        ok = asyncio.run(me.store_profile_vector(
            {"title": "Arrival", "embedding_text": "x", "plex_rating_key": "5"},
            media_type="movie"))
    finally:
        eg.EmbeddingGenerator = real
    check("store_profile_vector reports a vector that did not land", ok is False)
    pas = inspect.getsource(me.process_and_save)
    check("process_and_save marks vector_pending before caching",
          pas.index('profile["vector_pending"] = True') < pas.index("cache.set_cache("))
    enr = (ROOT / "src/routers/enrichment.py").read_text(encoding="utf-8")
    check("an already-enriched hit re-embeds a vector_pending profile",
          'cached_profile.get("vector_pending")' in enr and "store_profile_vector(" in enr)

    from src.services.enrichment_state import log_topup_failure

    class _Log:
        def __init__(self):
            self.calls = []

        def debug(self, *a, **k):
            self.calls.append("debug")

        def warning(self, *a, **k):
            self.calls.append("warning")

    lg = _Log()
    log_topup_failure(lg, "[t]", "x", httpx.ConnectError("down"))
    log_topup_failure(lg, "[t]", "x", ValueError("too many values to unpack"))
    check("walker: an outage stays at DEBUG, a bug is a WARNING",
          lg.calls == ["debug", "warning"])


# ── deletion outcome ────────────────────────────────────────────────────────

def test_outcome_commit_and_plex_check():
    import src.routers.recommendations as recs
    with _session() as db:
        p = DeletionProposal(user_id=1, title="Gone", service="radarr",
                             media_id="5", status="deleting")
        db.add(p)
        db.flush()
        pid = p.id
    db = Session()
    p = db.get(DeletionProposal, pid)
    p.status, p.resolved_at = "deleted", datetime(2026, 10, 15, 12, 0)
    real_commit = db.commit
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return real_commit()

    db.commit = flaky
    asyncio.run(recs._commit_outcome(db, p))
    db.close()
    with _session() as db2:
        st = db2.get(DeletionProposal, pid).status
        healthy = db2.query(DeletionProposal).count() >= 1
    check("a failed outcome commit is retried as a conditional status write",
          st == "deleted")
    check("...and the session stays usable for the next item", healthy)

    saved = (settings.PLEX_URL, settings.PLEX_TOKEN, httpx.AsyncClient)
    settings.PLEX_URL, settings.PLEX_TOKEN = "http://plex.test:32400", SecretStr("tok")
    real_client = httpx.AsyncClient

    def run(get_status):
        def handler(req):
            if req.method == "DELETE":
                return httpx.Response(200)
            if get_status == "raise":
                raise httpx.ReadTimeout("slow")
            return httpx.Response(get_status)
        return asyncio.run(recs._plex_delete_artist(
            "12", client=real_client(transport=httpx.MockTransport(handler))))

    try:
        r_500, r_timeout, r_200 = run(500), run("raise"), run(200)
        r_404 = run(404)
    finally:
        settings.PLEX_URL, settings.PLEX_TOKEN, httpx.AsyncClient = saved
    check("Plex delete: a 500 on the check is limbo, not success", r_500 is None)
    check("Plex delete: a check that raises is limbo, not error", r_timeout is None)
    check("Plex delete: still there is a failure", r_200 is False)
    check("Plex delete: only a 404 proves it gone", r_404 is True)


# ── AppSec leftovers ────────────────────────────────────────────────────────

def test_appsec_leftovers():
    from src.services.endpoint_policy import resolved_refusal

    def _run_with(addrs, url):
        loop = asyncio.new_event_loop()

        async def fake(host, port, *a, **k):
            return [(2, 1, 6, "", (ip, 0)) for ip in addrs]
        loop.getaddrinfo = fake
        try:
            return loop.run_until_complete(resolved_refusal(url))
        finally:
            loop.close()

    check("a name resolving to the metadata address is refused",
          bool(_run_with(["169.254.169.254"], "http://meta.example:80")))
    check("a name resolving to an IPv4-mapped link-local is refused",
          bool(_run_with(["::ffff:169.254.1.1"], "http://x.example")))
    check("a name resolving to the LAN is allowed",
          _run_with(["192.168.1.5"], "http://plex.local:32400") is None)
    wiz = (ROOT / "src/services/setup_wizard.py").read_text(encoding="utf-8")
    lib = (ROOT / "src/routers/library.py").read_text(encoding="utf-8")
    check("the connection tests resolve before connecting",
          wiz.count("await _refused_target(") == 3 and "await resolved_refusal(" in lib)
    hosted = wiz[wiz.index("async def test_tmdb"):wiz.index("# ── ENV WRITER")]
    check("no fixed-host test answers with exception text",
          '"error": str(e)' not in hosted and hosted.count("_test_failed(_HOSTED, e)") == 3)

    from src.routers import auth
    real_secret = settings.JWT_SECRET
    settings.JWT_SECRET = SecretStr("s" * 64)
    try:
        auth._decode_jwt("not.a.jwt")
        detail = ""
    except HTTPException as e:
        detail = e.detail
    finally:
        settings.JWT_SECRET = real_secret
    check("a bad JWT gets a generic 401 (no library text)",
          detail == "Session expired or invalid")
    check("plex.tv's body is not returned to the client",
          "{resp.text}" not in inspect.getsource(auth))

    from src.routers import imports

    class _Up:
        def __init__(self):
            self.filename, self.sizes = "x.json", []

        async def read(self, size=-1):
            self.sizes.append(size)
            return b"x" * 10

    try:
        asyncio.run(imports.spotify_upload(files=[_Up() for _ in range(imports._MAX_FILES + 1)]))
        refused = False
    except HTTPException as e:
        refused = e.status_code == 413
    check("Spotify upload: too many files is refused before reading", refused)
    up = _Up()
    real_save = imports.spotify_import.save_upload
    imports.spotify_import.save_upload = lambda n, c: {"saved": [], "rejected": []}
    real_pending = imports.spotify_import.pending_files
    imports.spotify_import.pending_files = lambda: []
    try:
        asyncio.run(imports.spotify_upload(files=[up]))
    finally:
        imports.spotify_import.save_upload = real_save
        imports.spotify_import.pending_files = real_pending
    check("Spotify upload: each file is read with a bound",
          up.sizes == [imports._MAX_FILE_MB * 1024 * 1024 + 1])


# ── concurrency ─────────────────────────────────────────────────────────────

def test_reattribution_off_the_loop():
    import src.services.plex_sync as ps
    with _session() as db:
        db.add_all([User(id=1, plex_user_id="p1", plex_username="owner", is_admin=True),
                    User(id=2, plex_user_id="p2", plex_username="partner")])
        t = datetime(2026, 9, 1, 20, 0, 0)
        db.add_all([
            WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id="100", title="A",
                              viewed_at=t, media_type="movie"),
            WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id="101", title="B",
                              viewed_at=t + timedelta(hours=1), media_type="movie"),
        ])
    import calendar
    ts = calendar.timegm(t.utctimetuple())

    async def fake_lookup(url, headers):
        return {("100", ts): "7"}, {"100": [(ts, "7")]}

    async def fake_accounts(url, headers, users):
        partner = next(u for u in users if u.id == 2)
        return {"1": next(u for u in users if u.id == 1), "7": partner}

    saved = (ps.get_db_session, conn.write_session, ps._fetch_plex_history_lookup,
             ps._fetch_account_to_user_map, settings.PLEX_URL, settings.PLEX_TOKEN,
             ps.set_datetime)
    ps.get_db_session = _session
    conn.write_session = _session
    ps._fetch_plex_history_lookup = fake_lookup
    ps._fetch_account_to_user_map = fake_accounts
    ps.set_datetime = lambda *a, **k: None
    settings.PLEX_URL, settings.PLEX_TOKEN = "http://plex.test:32400", SecretStr("tok")
    try:
        r = asyncio.run(ps.reattribute_watch_history())
    finally:
        (ps.get_db_session, conn.write_session, ps._fetch_plex_history_lookup,
         ps._fetch_account_to_user_map, settings.PLEX_URL, settings.PLEX_TOKEN,
         ps.set_datetime) = saved
    with _session() as db:
        a = db.query(WatchHistoryEntry).filter_by(plex_item_id="100").one()
        moved = (a.user_id, a.plex_user_id)
    check("re-attribution moves the play to the account that watched it",
          moved == (2, "7") and r.get("updated") == 1 and r.get("unattributable") == 1)
    src = inspect.getsource(ps.reattribute_watch_history)
    check("re-attribution: no session open across the /accounts fetch",
          src.index("await _fetch_account_to_user_map(") < src.index("def _scan()")
          and "await run_read(_snapshot_users)" in src)
    check("plex sync commits in batches",
          "if synced % _WRITE_BATCH == 0:" in inspect.getsource(ps))


def test_music_runner_lease():
    real = app_state.get_state
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                             "music_enricher"])
    try:
        app_state.get_state = lambda k: str(proc.pid) if k == app_state.EXTERNAL_HOLDER_KEY else None
        alive = app_state.external_music_holder_alive()
        app_state.get_state = lambda k: str(os.getpid()) if k == app_state.EXTERNAL_HOLDER_KEY else None
        not_runner = app_state.external_music_holder_alive()
        app_state.get_state = lambda k: "" if k == app_state.EXTERNAL_HOLDER_KEY else None
        empty = app_state.external_music_holder_alive()
    finally:
        app_state.get_state = real
        proc.kill()
        proc.wait()
    check("a live music_enricher holds the lease", alive is True)
    check("a PID that is not the runner (recycled) is no lease", not_runner is False)
    check("no PID, no lease", empty is False)
    main = (ROOT / "src/main.py").read_text(encoding="utf-8")
    script = (ROOT / "scripts/music_enricher.py").read_text(encoding="utf-8")
    check("boot and shutdown keep the runner's flag while it lives",
          main.count("external_music_holder_alive()") == 2)
    check("the runner records and clears its PID",
          "set_state(EXTERNAL_HOLDER_KEY, str(os.getpid()))" in script
          and 'force_set_state(EXTERNAL_HOLDER_KEY, "")' in script)


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            fn()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
