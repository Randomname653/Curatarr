"""Silent-failure sweep, round 3 — API / OS lifecycle
(docs/SILENT_FAILURE_SWEEP_2026_10.md §2, model updates, process monitor,
setup wizard, Syncthing guard, tray, LLM lane).

Each behavioural check fails on the code before this change:
  * model updates: a failed rebake is not "current" (pin kept, 502); a pin
    copy that failed stops the test before the pull; a rollback that could
    not restore says so; the disk-room guard fails closed; a corrupt state
    is kept aside; a rollback that raises keeps the test's own error and
    releases the bulk hold; the router never sticks at "running"
  * GPU-pressure gate: an nvidia-smi timeout counts as pressure (no 600 s
    switch-off); [N/A] utilization still judges by free memory
  * process monitor: a failed DB read keeps the known games, skips the
    game prompts; unload / scan failures are WARNINGs
  * setup wizard: icacls / chmod failures are WARNINGs; a bug in
    model_exists is not "absent" in silence; a pull without "success" fails
  * sync guard: an unreadable config.xml is not "not synced"
  * tray: a failed autostart shortcut is not reported as done
  * LLM lane: a broken probe is a WARNING, once per distinct error

    python tests/test_silent_failures_r3_os.py
"""
import asyncio
import logging
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import _harness  # noqa: E402,F401  (first: temp stores and .env, never the live data/)
import src.services.app_state as app_state  # noqa: E402
from src.config import settings  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


@contextmanager
def captured(logger_name):
    """Records of ``logger_name`` (and below) at DEBUG and up."""
    recs = []

    class H(logging.Handler):
        def emit(self, record):
            recs.append(record)

    lg = logging.getLogger(logger_name)
    h, old = H(level=logging.DEBUG), lg.level
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    try:
        yield recs
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)


def warnings(recs, needle=""):
    return [r for r in recs if r.levelno >= logging.WARNING and needle in r.getMessage()]


def run(coro):
    return asyncio.run(coro)


# ── model updates ───────────────────────────────────────────────────────────
from src.services import model_updates as mu  # noqa: E402
import src.services.setup_wizard as sw  # noqa: E402
import src.services.llm_priority as lp  # noqa: E402

_REAL = {k: getattr(mu, k) for k in ("load_state", "save_state", "_ollama", "running_weights",
                                      "_compare", "rollback")}
_REAL_SW = {k: getattr(sw, k) for k in ("build_ollama_models", "pull_ollama_model")}


def _restore_mu():
    for k, v in _REAL.items():
        setattr(mu, k, v)
    for k, v in _REAL_SW.items():
        setattr(sw, k, v)


def _fake_store(initial):
    store = {k: dict(v) for k, v in initial.items()}
    mu.load_state = lambda: {k: dict(v) for k, v in store.items()}
    mu.save_state = lambda st: (store.clear(), store.update({k: dict(v) for k, v in st.items()}))
    return store


def _fake_weights(run_map):
    async def rw(endpoint, model):
        return run_map.get(model)
    mu.running_weights = rw


ROW = {"role": "curator", "base": "gemma4:31b", "bake": "curatarr-curator",
       "running": "sha256:old", "remote": "sha256:new", "status": "update", "comparable": True}
PIN = mu.pin_name("gemma4:31b", "sha256:old")


def test_failed_rebake_is_not_current():
    calls = []

    async def ollama(endpoint, method, path, **kw):
        calls.append((method, path, kw.get("json")))
        return httpx.Response(200)

    async def build(*a):
        return {"embedding": True, "curator": False, "summarizer": True}

    try:
        store = _fake_store({"curator": {**ROW, "comparison": {
            "pinned_as": PIN, "new_weights": "sha256:new", "summary": {}}}})
        mu._ollama, sw.build_ollama_models = ollama, build
        _fake_weights({PIN: "sha256:old"})
        raised = None
        try:
            run(mu.decide("curator", "update"))
        except RuntimeError as e:
            raised = e
        row = store["curator"]
    finally:
        _restore_mu()
    check("update: a failed rebake raises instead of reporting the update", raised is not None)
    check("...the pin is NOT deleted (it is the only copy of the old weights)",
          not any(c[0] == "DELETE" for c in calls))
    check("...the row stays 'update' on the old weights, the result kept, the error recorded",
          row["status"] == "update" and row["running"] == "sha256:old"
          and "comparison" in row and "curator" in (row.get("compare_error") or ""))


def test_router_maps_a_failed_decide_to_502():
    import src.routers.model_updates as rmu

    async def boom(role, action):
        raise RuntimeError("the update did not finish: rebuilding curator failed")

    real_decide = mu.decide
    mu.decide = boom
    try:
        code = None
        try:
            run(rmu.decide("curator", rmu.DecideRequest(action="update")))
        except HTTPException as e:
            code = e.status_code
    finally:
        mu.decide = real_decide
    check("router: a failed decide is HTTP 502, not a 500 or a success", code == 502)


def test_failed_pin_copy_stops_before_the_pull():
    pulled = []

    async def ollama(endpoint, method, path, **kw):
        return httpx.Response(500, json={"error": "disk full"})

    async def pull(endpoint, model):
        pulled.append(model)
        return True

    try:
        _fake_store({"curator": ROW})
        mu._ollama, sw.pull_ollama_model = ollama, pull
        _fake_weights({"gemma4:31b": "sha256:old", "curatarr-curator": "sha256:old"})
        err = None
        try:
            run(mu._compare("curator", dict(ROW), "http://localhost:11434", 1, PIN, lambda m: None))
        except RuntimeError as e:
            err = str(e)
    finally:
        _restore_mu()
    check("test: a pin copy that failed stops the test", err is not None and "pin" in err)
    check("...before anything is downloaded over the tag", pulled == [])


def test_rollback_that_could_not_restore_says_so():
    async def ollama(endpoint, method, path, **kw):   # the copy "succeeds" but moves nothing
        return httpx.Response(500)

    try:
        store = _fake_store({"curator": {**ROW, "test_started": "x"}})
        mu._ollama = ollama
        _fake_weights({"gemma4:31b": "sha256:new", PIN: "sha256:old"})
        done = run(mu.recover_interrupted())
        msg = store["curator"].get("compare_error") or ""
    finally:
        _restore_mu()
    check("recovery: a tag that was not put back is NOT reported as 'everything was put back'",
          done == ["curator"] and "everything was put back" not in msg and "NOT" in msg)


def test_disk_room_fails_closed():
    real_client, real_du = httpx.AsyncClient, shutil.disk_usage
    status = {"code": 503, "body": {}}
    httpx.AsyncClient = lambda **kw: real_client(transport=httpx.MockTransport(
        lambda req: httpx.Response(status["code"], json=status["body"])))
    shutil.disk_usage = lambda p: SimpleNamespace(free=100e9, total=1e12, used=0)
    try:
        ok_503, why_503 = run(mu.disk_room("http://localhost:11434", "gemma4:31b"))
        status.update(code=200, body={"layers": [{"size": 20e9}]})
        shutil.disk_usage = lambda p: SimpleNamespace(free=11e9, total=1e12, used=0)
        ok_tight, _ = run(mu.disk_room("http://localhost:11434", "gemma4:31b"))
        shutil.disk_usage = lambda p: SimpleNamespace(free=100e9, total=1e12, used=0)
        ok_room, _ = run(mu.disk_room("http://localhost:11434", "gemma4:31b"))
    finally:
        httpx.AsyncClient, shutil.disk_usage = real_client, real_du
    check("disk room: a registry 503 (size unknown) refuses the test", ok_503 is False
          and "size" in why_503)
    check("disk room: 20 GB of new weights with 11 GB free is refused", ok_tight is False)
    check("disk room: a known size with room passes", ok_room is True)


def test_corrupt_state_is_kept_aside():
    from _checks import real_state
    kv = real_state()                   # the real app_state table, not a dict
    kv[mu.STATE_KEY] = '{"curator": {"test_started": "x"'
    saved = app_state.get_state, app_state.set_state
    try:
        with captured("src.services.model_updates") as recs:
            st = mu.load_state()
    finally:
        app_state.get_state, app_state.set_state = saved
    check("corrupt state: the raw value is copied aside before anything resets it",
          st == {} and kv.get(f"{mu.STATE_KEY}_corrupt") == '{"curator": {"test_started": "x"')
    check("...and logged at ERROR", any(r.levelno >= logging.ERROR for r in recs))


def test_rollback_that_raises_keeps_the_error_and_releases_the_hold():
    async def compare(*a, **k):
        lp.extend_bulk_hold(900)
        raise RuntimeError("could not pull gemma4:31b")

    async def rollback(endpoint, row):
        raise httpx.ConnectError("ollama gone")

    try:
        store = _fake_store({"curator": ROW})
        mu._compare, mu.rollback = compare, rollback
        err = None
        try:
            run(mu.run_comparison("curator", 1))
        except Exception as e:  # noqa: BLE001
            err = e
        held = lp._bulk_held()
        marker = store["curator"].get("test_started")
    finally:
        _restore_mu()
        lp.release_bulk_hold()
    check("failed test + failed rollback: the TEST's error reaches the caller",
          isinstance(err, RuntimeError) and "could not pull" in str(err))
    check("...the bulk hold is released", held == 0.0)
    check("...the marker stays so the start-up recovery retries the rollback", bool(marker))


def test_router_never_sticks_at_running():
    import src.routers.model_updates as rmu
    import src.services.bg_tasks as bg
    launched = []

    def fake_track(coro, *, name=None):
        launched.append(name)
        coro.close()

    def bad_save(st):
        raise RuntimeError("database is locked")

    saved = mu.load_state, mu.save_state, bg.track_task
    mu.load_state = lambda: {"curator": dict(ROW)}
    mu.save_state = bad_save
    bg.track_task = fake_track
    try:
        try:
            run(rmu.compare("curator", False, user=SimpleNamespace(id=1)))
        except RuntimeError:
            pass
        stuck = rmu._running_view()
        mu.save_state = lambda st: None
        res = run(rmu.compare("curator", False, user=SimpleNamespace(id=1)))
    finally:
        mu.load_state, mu.save_state, bg.track_task = saved
        rmu._stop()
    check("router: a save that failed does not leave the test 'running' (no 409 until restart)",
          stuck is None)
    check("router: the test task is tracked (shutdown drain cancels it, its rollback runs)",
          res == {"started": True} and launched and "model_update" in launched[0])


# ── GPU-pressure gate ───────────────────────────────────────────────────────
from src.services import process_monitor as pm  # noqa: E402


def _gpu_reset():
    pm._gpu_cache.update(at=0.0, busy=False, since=0.0, reason="")
    pm._gpu_unavailable_until = 0.0
    pm._smi_state = "ok"
    settings.GPU_PRESSURE_GATE = True


@contextmanager
def smi_says(kind, stdout=""):
    real = subprocess.run

    def fake(*a, **kw):
        if kind == "timeout":
            raise subprocess.TimeoutExpired("nvidia-smi", 4)
        return subprocess.CompletedProcess(a[0], 0, stdout=stdout, stderr="")
    subprocess.run = fake
    try:
        yield
    finally:
        subprocess.run = real


def test_smi_timeout_counts_as_pressure():
    _gpu_reset()
    with captured("src.services.process_monitor") as recs, smi_says("timeout"):
        first = pm.gpu_pressure(ps=lambda: [], now=1000.0)
        second = pm.gpu_pressure(ps=lambda: [], now=1050.0)
    check("nvidia-smi timeout: the gate is NOT switched off for ten minutes",
          pm._gpu_unavailable_until == 0.0)
    check("...a timeout that persists counts as pressure (fails closed)", first is False and second is True)
    check("...one WARNING for the state change, not one per tick",
          len(warnings(recs, "timed out")) == 1)
    _gpu_reset()


def test_smi_na_utilization_still_uses_memory():
    _gpu_reset()
    with smi_says("ok", "22500, 24564, [N/A]\n"):
        a = pm.gpu_pressure(ps=lambda: [], now=1000.0)
        b = pm.gpu_pressure(ps=lambda: [], now=1050.0)
    check("[N/A] utilization: free memory still decides (2 GB free = pressure)", a is False and b is True)
    check("...and the gate is not switched off", pm._gpu_unavailable_until == 0.0)
    _gpu_reset()
    with smi_says("ok", "900, 24564, [N/A]\n"):
        c = pm.gpu_pressure(ps=lambda: [], now=1000.0)
        d = pm.gpu_pressure(ps=lambda: [], now=1050.0)
        plain = pm._nvidia_smi()
    check("[N/A] utilization with free memory: no pressure", c is False and d is False)
    check("_nvidia_smi keeps its (used, total, util) contract for model_check",
          plain == (900.0, 24564.0, None))
    _gpu_reset()
    with smi_says("timeout"):
        check("_nvidia_smi: a timeout is still None for model_check", pm._nvidia_smi() is None)


def test_missing_smi_still_left_alone():
    _gpu_reset()
    check("no nvidia-smi on the host: still no pressure, asked again in ten minutes",
          pm.gpu_pressure(smi=lambda: None, ps=lambda: [], now=1000.0) is False
          and pm._gpu_unavailable_until == 1600.0)
    _gpu_reset()


# ── process monitor ─────────────────────────────────────────────────────────
import src.database.connection as dbc  # noqa: E402


class _Q:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *a):
        return self

    def all(self):
        return self.rows


@contextmanager
def _db(rows=None, fail=False):
    @contextmanager
    def sess():
        if fail:
            raise RuntimeError("database is locked")
        yield SimpleNamespace(query=lambda *a: _Q(rows or []))
    real = dbc.get_db_session
    dbc.get_db_session = sess
    try:
        yield
    finally:
        dbc.get_db_session = real


def _procs(*names, rss=2 * 2**30):
    def it(attrs=None):
        for i, n in enumerate(names):
            yield SimpleNamespace(info={"name": n, "pid": 100 + i}, pid=100 + i,
                                  memory_info=lambda: SimpleNamespace(rss=rss))
    return it


def test_failed_game_list_read_keeps_known_games():
    real_iter = pm.psutil.process_iter
    pm.invalidate_process_cache()
    pm._cached_game_pid = None
    pm.psutil.process_iter = _procs("eldenring.exe")
    try:
        with _db([("eldenring.exe",)]):
            first = pm.game_process_running()
        pm._cached_time = 0          # cache expired
        pm._cached_game_pid = None
        with captured("src.services.process_monitor") as recs, _db(fail=True):
            again = pm.game_process_running()
    finally:
        pm.psutil.process_iter = real_iter
        pm.invalidate_process_cache()
        pm._cached_game_pid = None
    check("game list: a failed DB read keeps the games read before (not 'no game')", first and again)
    check("...and is a WARNING", warnings(recs, "classified games"))


def test_failed_classified_read_skips_the_prompts():
    real_iter = pm.psutil.process_iter
    pm.invalidate_process_cache()
    pm.psutil.process_iter = _procs("somebigtool.exe")
    try:
        with captured("src.services.process_monitor") as recs, _db(fail=True):
            asked = pm.get_unknown_processes()
        with _db([]):
            asked_ok = pm.get_unknown_processes()
    finally:
        pm.psutil.process_iter = real_iter
        pm.invalidate_process_cache()
    check("prompts: an unreadable classification asks nothing this round (no flood)", asked == [])
    check("...WARNING, and the next good read asks again",
          warnings(recs, "classified processes") and [p["name"] for p in asked_ok] == ["somebigtool.exe"])

    def broken(attrs=None):
        raise RuntimeError("access denied")
        yield  # noqa: unreachable — a generator

    pm.psutil.process_iter = broken
    try:
        with captured("src.services.process_monitor") as recs, _db([]):
            pm.get_unknown_processes()
    finally:
        pm.psutil.process_iter = real_iter
        pm.invalidate_process_cache()
    check("a failed process scan is a WARNING", warnings(recs, "scan failed"))


def test_unload_http_error_is_logged():
    real_client = httpx.AsyncClient

    def handler(req):
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [{"name": f"{settings.CURATOR_MODEL}:latest"}]})
        return httpx.Response(500, json={"error": "busy"})

    httpx.AsyncClient = lambda **kw: real_client(transport=httpx.MockTransport(handler))
    try:
        with captured("src.services.process_monitor") as recs:
            out = run(pm.unload_llm_models())
    finally:
        httpx.AsyncClient = real_client
    check("unload: an HTTP 500 is a WARNING with the status, not silence",
          out == [] and warnings(recs, "500"))


# ── setup wizard ────────────────────────────────────────────────────────────

def test_icacls_failure_is_a_warning():
    real_os, real_run = sw.os, subprocess.run
    sw.os = SimpleNamespace(name="nt", environ={"USERNAME": "owner"}, getlogin=lambda: "owner")
    subprocess.run = lambda *a, **kw: subprocess.CompletedProcess(
        a[0], 5, stdout=b"", stderr=b"Access is denied.")
    try:
        with captured("src.services.setup_wizard") as recs:
            sw._restrict_env_acl(Path("C:/curatarr/.env"))
    finally:
        sw.os, subprocess.run = real_os, real_run
    check("icacls exit != 0: WARNING with its exit code and message",
          warnings(recs, "exit 5") and warnings(recs, "Access is denied"))

    def bad_chmod(*a):
        raise PermissionError("not the owner")

    sw.os = SimpleNamespace(name="posix", chmod=bad_chmod)
    try:
        with captured("src.services.setup_wizard") as recs:
            sw._restrict_env_acl(Path("/tmp/x/.env"))
    finally:
        sw.os = real_os
    check("chmod failure: WARNING (tokens may be readable), not DEBUG", warnings(recs, "chmod"))


def test_model_exists_and_pull_report_failures():
    real_client = httpx.AsyncClient
    mode = {"m": "bug"}

    def handler(req):
        if mode["m"] == "bug":
            return httpx.Response(200, json={"models": [{"model": "gemma4:31b"}]})   # no "name"
        if mode["m"] == "net":
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, content=b'{"status":"pulling manifest"}\n'
                                           b'{"status":"downloading","total":10,"completed":5}\n')

    httpx.AsyncClient = lambda **kw: real_client(transport=httpx.MockTransport(handler))
    try:
        with captured("src.services.setup_wizard") as recs:
            bug = run(sw.model_exists("http://localhost:11434", "gemma4:31b"))
        bug_warned = bool(warnings(recs, "model_exists"))
        mode["m"] = "net"
        with captured("src.services.setup_wizard") as recs:
            net = run(sw.model_exists("http://localhost:11434", "gemma4:31b"))
        net_warned = bool(warnings(recs))
        mode["m"] = "pull"
        pulled = run(sw.pull_ollama_model("http://localhost:11434", "gemma4:31b"))
    finally:
        httpx.AsyncClient = real_client
    check("model_exists: a bug is a WARNING (it used to mean a silent re-pull)", bug is False and bug_warned)
    check("model_exists: Ollama unreachable stays quiet (DEBUG), still False", net is False and not net_warned)
    check("pull: a stream that ends without 'success' is a failed pull", pulled is False)


# ── sync guard ──────────────────────────────────────────────────────────────
from src.services import sync_guard as sg  # noqa: E402


def test_unreadable_syncthing_config_is_not_not_synced():
    tmp = Path(tempfile.mkdtemp(prefix="curatarr-sg-"))
    try:
        cfg = tmp / "config.xml"
        cfg.write_text("<configuration><folder path=", encoding="utf-8")   # truncated
        folder = tmp / "Sync"
        data = folder / "Curatarr" / "data"
        data.mkdir(parents=True)
        (folder / ".stfolder").mkdir()
        saved = sg._candidate_config_paths, sg._data_dir
        sg._candidate_config_paths = lambda: [cfg]
        sg._data_dir = lambda: data
        try:
            with captured("src.services.sync_guard") as recs:
                sg.enable()
            st = folder / ".stignore"
            written = st.exists() and "/Curatarr/data" in st.read_text(encoding="utf-8")
            parse_warned = bool(warnings(recs, "could not parse"))
            shutil.rmtree(folder / ".stfolder")
            st.unlink()
            with captured("src.services.sync_guard") as recs:
                sg.enable()
            may_warned = bool(warnings(recs, "MAY be synced"))
        finally:
            sg._candidate_config_paths, sg._data_dir = saved
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    check("sync guard: an unparseable config.xml is a WARNING", parse_warned)
    check("...the .stfolder marker still finds the folder and the DB is excluded", written)
    check("...without a marker it says the DB MAY be synced (not 'nothing to do')", may_warned)


# ── tray ────────────────────────────────────────────────────────────────────

def test_tray_autostart_failure_is_reported():
    import src.tray_app as tray
    tmp = Path(tempfile.mkdtemp(prefix="curatarr-tray-"))
    saved = tray._STARTUP_LNK, tray.subprocess.run
    notes = []
    icon = SimpleNamespace(notify=lambda msg, title=None: notes.append(msg))
    tray._STARTUP_LNK = tmp / "Curatarr.lnk"
    try:
        tray.subprocess.run = lambda *a, **kw: subprocess.CompletedProcess(
            a[0], 1, stdout=b"", stderr=b"Exception calling CreateShortcut")
        with captured("curatarr.tray") as recs:
            tray._toggle_autostart(icon, None)
        failed_note = notes[-1]

        def ok_run(*a, **kw):
            tray._STARTUP_LNK.write_bytes(b"lnk")
            return subprocess.CompletedProcess(a[0], 0, stdout=b"", stderr=b"")
        tray.subprocess.run = ok_run
        tray._toggle_autostart(icon, None)
        ok_note = notes[-1]
    finally:
        tray._STARTUP_LNK, tray.subprocess.run = saved
        shutil.rmtree(tmp, ignore_errors=True)
    check("tray: a PowerShell failure is not reported as 'will start with Windows'",
          "will start" not in failed_note and warnings(recs, "exit 1"))
    check("tray: a created shortcut still says so", "will start with Windows" in ok_note)


# ── LLM lane ────────────────────────────────────────────────────────────────

def test_lane_probe_failure_is_a_warning_once():
    from src.services import llm_lane
    real = pm.gpu_pressure
    err = {"e": RuntimeError("bad state")}

    def broken():
        raise err["e"]

    pm.gpu_pressure = broken
    llm_lane._probe_errors.clear()
    try:
        with captured("src.services.llm_lane") as recs:
            a, b = llm_lane._pressed(), llm_lane._pressed()
            err["e"] = KeyError("other")
            c = llm_lane._pressed()
    finally:
        pm.gpu_pressure = real
        llm_lane._probe_errors.clear()
    check("lane: a broken GPU probe still fails open", (a, b, c) == (False, False, False))
    check("...but WARNs once per distinct error, not every call", len(warnings(recs, "gpu_pressure")) == 2)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                check(f"{name} raised {type(e).__name__}: {e}", False)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
