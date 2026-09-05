"""Setup flow coverage: every integration the app uses can be configured.

The 2026-09 coverage audit found the two-bake pitcher split invisible
end-to-end (complete_setup never passed base_pitcher; no request field, no
catalog role), ListenBrainz and OpenSubtitles with no UI at all, and the
core connection settings settable exactly once. This suite pins the
backend half of the fix; the frontend half is checked by the schema test
in test_gui_import.py and the node syntax guard.

    python tests/test_setup_coverage.py
"""
import asyncio
import pathlib
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import src.services.setup_wizard as sw
import src.routers.setup as st


def test_current_config_is_plain_and_complete():
    cfg = sw.current_env_config()
    for key in ("plex_url", "plex_token", "ollama_endpoint", "base_curator_model",
                "embedding_model", "pitcher_model", "base_pitcher_model",
                "listenbrainz_token", "opensubtitles_api_key", "jwt_secret"):
        assert key in cfg, key
    assert all(type(v).__name__ != "SecretStr" for v in cfg.values()), \
        "secrets must be unwrapped here, or write_env stores a mask"


def test_masking_hides_every_secret_and_drops_the_jwt():
    masked = sw.mask_secrets({"plex_url": "http://p", "plex_token": "tok",
                              "tmdb_api_key": "", "jwt_secret": "s3cret",
                              "base_curator_model": "gemma4:31b"})
    assert masked["plex_url"] == "http://p"
    assert masked["plex_token"] == {"set": True}
    assert masked["tmdb_api_key"] == {"set": False}
    assert "jwt_secret" not in masked
    assert masked["base_curator_model"] == "gemma4:31b"


def test_merge_skips_none_clears_on_empty_and_maps_the_pitcher_switch():
    cur = {"plex_url": "http://old", "tmdb_api_key": "k", "pitcher_model": ""}
    merged = sw.merge_env_config(cur, {"plex_url": None, "tmdb_api_key": "",
                                       "enable_pitcher": True})
    assert merged["plex_url"] == "http://old"          # None = unchanged
    assert merged["tmdb_api_key"] == ""                # "" = cleared
    assert merged["pitcher_model"] == "curatarr-pitcher"
    assert "enable_pitcher" not in merged
    assert sw.merge_env_config(cur, {"enable_pitcher": False})["pitcher_model"] == ""


class _Live:
    effective_jwt_secret = "live-jwt-secret-0123456789-0123456789-01234567"
    effective_plex_url = "http://plex"; effective_plex_token = "ptok"
    effective_ollama = "http://localhost:11434"
    BASE_CURATOR_MODEL = "gemma4:31b"; BASE_SUMMARIZER_MODEL = "granite4.1:8b"
    EMBEDDING_MODEL = "nomic-embed-text-v2-moe"
    PITCHER_MODEL = ""; BASE_PITCHER_MODEL = "qwen3.8:27b"
    SOULSYNC_URL = None; SOULSYNC_API_KEY = None
    SYNC_ON_STARTUP = True; SYNC_INTERVAL_HOURS = 24
    BINGE_EPISODE_THRESHOLD = 3; BINGE_SESSION_HOURS = 6; BINGE_SERIES_PERCENT = 0.5
    LISTENBRAINZ_TOKEN = "lb-live"
    OPENSUBTITLES_API_KEY = None; OPENSUBTITLES_USERNAME = None
    OPENSUBTITLES_PASSWORD = None; OPENSUBTITLES_DAILY_BUDGET = 400


def test_write_env_manages_listenbrainz_and_opensubtitles():
    tmp = pathlib.Path(tempfile.mkdtemp()) / ".env"
    real = (sw.ENV_PATH, sw._live_settings, sw._restrict_env_acl)
    sw.ENV_PATH, sw._live_settings, sw._restrict_env_acl = tmp, (lambda: _Live()), (lambda p: None)
    try:
        sw.write_env({"plex_url": "http://p", "plex_token": "t",
                      "opensubtitles_api_key": "os-key", "opensubtitles_daily_budget": 250})
        text = tmp.read_text(encoding="utf-8")
    finally:
        sw.ENV_PATH, sw._live_settings, sw._restrict_env_acl = real
    assert "LISTENBRAINZ_TOKEN=lb-live" in text, "token not in the request -> carried from live"
    assert "OPENSUBTITLES_API_KEY=os-key" in text
    assert "OPENSUBTITLES_DAILY_BUDGET=250" in text
    assert "OPENSUBTITLES_USERNAME=" in text


def test_complete_setup_builds_the_pitcher_when_enabled():
    calls = {}

    class _BG:
        def add_task(self, fn, *args):
            calls["fn"], calls["args"] = fn, args

    real_write = st.write_env
    st.write_env = lambda cfg: calls.setdefault("cfg", cfg)
    try:
        req = st.SetupCompleteRequest(plex_url="http://p", plex_token="t",
                                      enable_pitcher=True, base_pitcher_model="qwen3.8:27b")
        asyncio.run(st.complete_setup(req, _BG(), _gate=None))
        assert calls["args"][-1] == "qwen3.8:27b", calls["args"]
        assert calls["cfg"]["pitcher_model"] == "curatarr-pitcher"
        calls.clear()
        req = st.SetupCompleteRequest(plex_url="http://p", plex_token="t")
        asyncio.run(st.complete_setup(req, _BG(), _gate=None))
        assert calls["args"][-1] is None, "pitcher off -> no pitcher bake"
        assert calls["cfg"]["pitcher_model"] == ""
    finally:
        st.write_env = real_write


def test_reconfigure_model_exists_with_every_wizard_key_optional():
    fields = st.ReconfigureRequest.model_fields
    for key in ("plex_url", "plex_token", "ollama_endpoint", "base_curator_model",
                "enable_pitcher", "listenbrainz_token", "opensubtitles_api_key",
                "radarr_url", "soulsync_api_key"):
        assert key in fields, key
    assert st.ReconfigureRequest().model_dump(exclude_none=True) == {}


def test_library_configure_uses_the_shared_config():
    lib = (_ROOT / "src/routers/library.py").read_text(encoding="utf-8")
    assert "current_env_config()" in lib
    for stale in ("qwen2.5:32b", "dolphin3", '"nomic-embed-text"'):
        assert stale not in lib, stale


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
