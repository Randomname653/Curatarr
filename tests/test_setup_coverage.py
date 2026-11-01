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
                "listenbrainz_token", "opensubtitles_api_key", "mal_client_id", "jwt_secret"):
        assert key in cfg, key
    assert all(type(v).__name__ != "SecretStr" for v in cfg.values()), \
        "secrets must be unwrapped here, or write_env stores a mask"


def test_masking_hides_every_secret_and_drops_the_jwt():
    masked = sw.mask_secrets({"plex_url": "http://p", "plex_token": "tok",
                              "tmdb_api_key": "", "jwt_secret": "s3cret",
                              "mal_client_id": "mal-id",
                              "base_curator_model": "gemma4:31b"})
    assert masked["mal_client_id"] == {"set": True}
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
    MAL_CLIENT_ID = "mal-live"


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
    assert "MAL_CLIENT_ID=mal-live" in text, "an id added by hand survives a settings save"


def test_complete_setup_builds_the_pitcher_when_enabled():
    calls = {}

    class _BG:
        def add_task(self, fn, *args, **kwargs):
            calls["fn"], calls["args"], calls["kwargs"] = fn, args, kwargs

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
        # The first bake carries the window the wizard chose, not the one
        # this process started with (it holds the defaults until a restart).
        assert calls["kwargs"]["num_ctx"] == 16384, calls["kwargs"]
        for profile, ctx, want in (("small", None, 8192), ("small", 12000, 12000),
                                   ("large", None, 16384)):
            calls.clear()
            req = st.SetupCompleteRequest(plex_url="http://p", plex_token="t",
                                          llm_profile=profile, curator_num_ctx=ctx)
            asyncio.run(st.complete_setup(req, _BG(), _gate=None))
            assert calls["kwargs"]["num_ctx"] == want, (profile, ctx, calls["kwargs"])
            assert calls["cfg"]["llm_profile"] == profile
    finally:
        st.write_env = real_write


def test_the_curator_profile_is_written_and_kept():
    """LLM_PROFILE / CURATOR_NUM_CTX / CURATOR_NUM_GPU: the bench package's
    values reach the .env (they were only shown as lines to copy), and a
    write that does not name them keeps what the install has."""
    tmp = pathlib.Path(tempfile.mkdtemp()) / ".env"
    old_path = sw.ENV_PATH
    sw.ENV_PATH = tmp
    try:
        cfg = sw.merge_env_config(sw.current_env_config(), {
            "llm_profile": "small", "curator_num_ctx": 12288, "curator_num_gpu": -1})
        sw.write_env(cfg)
        keys = ("LLM_PROFILE", "CURATOR_NUM_CTX", "CURATOR_NUM_GPU")
        assert _env_values(tmp, keys) == {"LLM_PROFILE": ["small"], "CURATOR_NUM_CTX": ["12288"],
                                          "CURATOR_NUM_GPU": ["-1"]}, _env_values(tmp, keys)
        cfg.pop("llm_profile")
        cfg["curator_num_ctx"] = None
        saved = (sw._live_settings().LLM_PROFILE, sw._live_settings().CURATOR_NUM_CTX)
        sw._live_settings().LLM_PROFILE, sw._live_settings().CURATOR_NUM_CTX = "small", 9000
        try:
            sw.write_env(cfg)
        finally:
            sw._live_settings().LLM_PROFILE, sw._live_settings().CURATOR_NUM_CTX = saved
        assert _env_values(tmp, ("LLM_PROFILE", "CURATOR_NUM_CTX")) == {
            "LLM_PROFILE": ["small"], "CURATOR_NUM_CTX": ["9000"]}, "a field left out keeps the live value"
    finally:
        sw.ENV_PATH = old_path


def _env_values(path, keys):
    """{key: [every value the written .env gives it]} for ``keys``."""
    out = {k: [] for k in keys}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            k, _, v = line.strip().partition("=")
            if k in out:
                out[k].append(v)
    return out


def test_the_curator_profile_is_validated_and_needs_a_rebuild():
    ok = st.ReconfigureRequest(llm_profile=" Small ", curator_num_ctx=0, curator_num_gpu=-1)
    assert (ok.llm_profile, ok.curator_num_ctx, ok.curator_num_gpu) == ("small", 0, -1)
    for bad in ({"llm_profile": "huge"}, {"curator_num_ctx": 2048},
                {"curator_num_ctx": 500000}, {"curator_num_gpu": -2}):
        try:
            st.ReconfigureRequest(**bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")
    assert {"llm_profile", "curator_num_ctx", "curator_num_gpu"} <= set(st._MODEL_KEYS), \
        "a profile change must say: rebuild models, then restart"


def test_rebuild_bakes_the_window_the_settings_hold_now():
    """Settings -> Rebuild models right after a profile change: this process
    resolved CURATOR_NUM_CTX at import, the plan must not."""
    from src.services import model_check
    s = sw._live_settings()
    saved = (s.LLM_PROFILE, s.CURATOR_NUM_CTX, s.OLLAMA_PC_ENDPOINT)
    try:
        s.OLLAMA_PC_ENDPOINT = ""
        s.LLM_PROFILE, s.CURATOR_NUM_CTX = "small", 0
        assert model_check.host_plan("main")["num_ctx"] == 8192
        s.CURATOR_NUM_CTX = 12000
        assert model_check.host_plan("main")["num_ctx"] == 12000
    finally:
        s.LLM_PROFILE, s.CURATOR_NUM_CTX, s.OLLAMA_PC_ENDPOINT = saved


def test_reconfigure_model_exists_with_every_wizard_key_optional():
    fields = st.ReconfigureRequest.model_fields
    for key in ("plex_url", "plex_token", "ollama_endpoint", "base_curator_model",
                "enable_pitcher", "listenbrainz_token", "opensubtitles_api_key",
                "radarr_url", "soulsync_api_key", "mal_client_id"):
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
