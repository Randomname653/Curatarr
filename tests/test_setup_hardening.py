"""Setup / auth hardening from the 2026-09 first-run and boundary audits.

Pins:
  - write_env unwraps SecretStr and refuses to persist a "**********" mask
    (library_configure handed it raw objects: JWT_SECRET=********** would have
    made every token forgeable)
  - write_env carries the live JWT secret forward instead of regenerating it
    (a wizard re-run used to log the whole household out) and keeps tuned
    SYNC/BINGE values; the write is atomic
  - pre-admin setup calls need the console setup code unless local
  - Plex login: first account must be the server owner, later ones must be
    known to the server (/accounts)
  - the chat anchor cache is scoped per user
  - the poster cache honours a total budget
  - the small gates: force-resync admin-only, pipeline stop by owner/admin,
    PIN compare constant-time

    python tests/test_setup_hardening.py
"""
import asyncio
import pathlib
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from pydantic import SecretStr

import src.services.setup_wizard as sw
import src.routers.auth as auth
import src.routers.chat as chat
import src.routers.image_proxy as ip


class _LiveSettings:
    effective_jwt_secret = "live-secret-that-must-survive-a-rerun-0123456789"
    PITCHER_MODEL = ""
    BASE_PITCHER_MODEL = "qwen3.8:27b"
    SOULSYNC_URL = None
    SOULSYNC_API_KEY = None
    SYNC_ON_STARTUP = False
    SYNC_INTERVAL_HOURS = 6
    BINGE_EPISODE_THRESHOLD = 4
    BINGE_SESSION_HOURS = 8
    BINGE_SERIES_PERCENT = 0.7


def _write(config):
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    env = tmpdir / ".env"
    real_path, real_live, real_acl = sw.ENV_PATH, sw._live_settings, sw._restrict_env_acl
    sw.ENV_PATH = env
    sw._live_settings = lambda: _LiveSettings()
    sw._restrict_env_acl = lambda p: None
    try:
        sw.write_env(config)
        return env.read_text(encoding="utf-8"), tmpdir
    finally:
        sw.ENV_PATH, sw._live_settings, sw._restrict_env_acl = real_path, real_live, real_acl


def test_write_env_unwraps_secretstr_and_never_writes_the_mask():
    text, _ = _write({"plex_url": "http://p", "plex_token": SecretStr("plex-tok-123"),
                      "jwt_secret": SecretStr("jwt-real-secret")})
    assert "PLEX_TOKEN=plex-tok-123" in text
    assert "JWT_SECRET=jwt-real-secret" in text
    assert "*****" not in text
    try:
        _write({"plex_url": "http://p", "plex_token": "**********"})
        assert False, "a masked placeholder must be refused"
    except ValueError:
        pass


def test_write_env_keeps_the_live_secret_and_tuned_values():
    text, tmpdir = _write({"plex_url": "http://p", "plex_token": "t"})
    assert f"JWT_SECRET={_LiveSettings.effective_jwt_secret}" in text
    assert "SYNC_ON_STARTUP=false" in text and "SYNC_INTERVAL_HOURS=6" in text
    assert "BINGE_EPISODE_THRESHOLD=4" in text and "BINGE_SERIES_PERCENT=0.7" in text
    assert not list(tmpdir.glob("*.tmp")), "atomic write must leave no temp file"


class _Req:
    def __init__(self, host, code=None):
        self.client = type("C", (), {"host": host})()
        self.headers = {"X-Setup-Code": code} if code else {}


def test_setup_code_gate_exempts_localhost_and_demands_it_elsewhere():
    auth._require_setup_code(_Req("127.0.0.1"))                 # no code needed
    auth._require_setup_code(_Req("192.168.1.50", auth.SETUP_CODE.lower()))
    for bad in (None, "0000-0000"):
        try:
            auth._require_setup_code(_Req("192.168.1.50", bad))
            assert False, "remote caller without the code must be refused"
        except auth.HTTPException as e:
            assert e.status_code == 401 and "Setup code" in e.detail


def _membership(plex_id, first_ever, owner, ids):
    real_o, real_i = auth._plex_owner_id, auth._plex_server_account_ids

    async def _o():
        return owner

    async def _i():
        return ids
    auth._plex_owner_id, auth._plex_server_account_ids = _o, _i
    try:
        asyncio.run(auth._assert_plex_membership(plex_id, first_ever))
        return None
    except auth.HTTPException as e:
        return e.status_code
    finally:
        auth._plex_owner_id, auth._plex_server_account_ids = real_o, real_i


def test_plex_login_membership_rules():
    assert _membership("100", True, "100", set()) is None       # owner founds the install
    assert _membership("200", True, "100", set()) == 403        # stranger cannot be first admin
    assert _membership("200", True, None, set()) == 503         # no Plex yet -> fail closed
    assert _membership("300", False, "100", {"300", "301"}) is None   # known household member
    assert _membership("100", False, "100", set()) is None      # owner re-appearing after a DB rebuild
    assert _membership("999", False, "100", {"300"}) == 403     # random plex.tv account
    assert _membership("999", False, "100", None) == 503        # server unreachable -> fail closed for NEW users


def test_anchor_cache_is_scoped_per_user():
    chat._thread_active_title.clear()
    chat._set_thread_active_title(1, "general", ("Psycho-Pass", None, "anime"))
    chat._set_thread_active_title(2, "general", ("Sabrina Carpenter", None, "music"))
    assert chat._get_thread_active_title(1, "general")[0] == "Psycho-Pass"
    assert chat._get_thread_active_title(2, "general")[0] == "Sabrina Carpenter"
    assert chat._own_thread_active_titles(1) == [("general", ("Psycho-Pass", None, "anime"))]
    chat._clear_thread_active_titles(1)
    assert chat._get_thread_active_title(1, "general") is None
    assert chat._get_thread_active_title(2, "general") is not None, "clearing user 1 must not touch user 2"
    chat._thread_active_title.clear()


def test_poster_cache_budget_evicts_oldest_first():
    import time
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    real_dir, real_every, real_count = ip._CACHE_DIR, ip._SWEEP_EVERY, ip._writes_since_sweep
    from src.config import settings
    real_mb = settings.IMAGE_CACHE_MAX_MB
    ip._CACHE_DIR, ip._SWEEP_EVERY, ip._writes_since_sweep = tmpdir, 1, 0
    settings.IMAGE_CACHE_MAX_MB = 1                     # 1 MiB budget
    try:
        for n in range(4):                              # 4 x 400 KiB = 1.6 MiB > budget
            (tmpdir / f"f{n}.jpg").write_bytes(b"x" * 400 * 1024)
            time.sleep(0.02)
        ip._enforce_cache_budget()
        left = sorted(p.name for p in tmpdir.iterdir())
        total = sum(p.stat().st_size for p in tmpdir.iterdir())
        assert total <= 0.75 * 1024 * 1024 + 1, total
        assert "f0.jpg" not in left and "f3.jpg" in left, left   # oldest gone, newest kept
    finally:
        ip._CACHE_DIR, ip._SWEEP_EVERY, ip._writes_since_sweep = real_dir, real_every, real_count
        settings.IMAGE_CACHE_MAX_MB = real_mb


def test_the_small_gates_are_in_place():
    hist = (_ROOT / "src/routers/history.py").read_text(encoding="utf-8")
    music = (_ROOT / "src/routers/music.py").read_text(encoding="utf-8")
    users = (_ROOT / "src/routers/users.py").read_text(encoding="utf-8")
    lib = (_ROOT / "src/routers/library.py").read_text(encoding="utf-8")
    assert "if force and not user.is_admin:" in hist
    assert 'get_state("music_pipeline_owner") != str(user.id)' in music
    assert "hmac.compare_digest(_hash_pin(" in users
    assert "settings.effective_jwt_secret," in lib and "settings.JWT_SECRET," not in lib


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    sys.exit(1 if fails else 0)
