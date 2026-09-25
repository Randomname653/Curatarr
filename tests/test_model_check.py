"""One model list for every launcher, and "Ollama is down" is not "every
model is missing".

2026-09-25: start.bat probed a hardcoded nomic-embed-text and printed
"Ollama models missing. Building / pulling now..." on an install that runs
nomic-embed-text-v2-moe from the stored profile and never had v1. The tray
already asked the profile; now both ask src/services/model_check.py, and
start.bat goes through `python build_models.py --check`.

    python tests/test_model_check.py
"""
import pathlib
import sys
from types import SimpleNamespace

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.services import model_check as mc  # noqa: E402


def _runner(list_rc=0, present=()):
    calls = []

    def run(cmd, **kw):
        calls.append(list(cmd))
        if cmd[:2] == ["ollama", "list"]:
            return SimpleNamespace(returncode=list_rc)
        if cmd[:2] == ["ollama", "show"]:
            return SimpleNamespace(returncode=0 if cmd[2] in present else 1)
        raise AssertionError(cmd)
    return run, calls


def test_expected_models_come_from_the_runtime_not_the_env_default():
    real = mc.effective_embedding_model if hasattr(mc, "effective_embedding_model") else None
    import src.services.embed_service as es
    from src.config import settings
    orig_profile, orig_pitcher = es.effective_embedding_model, settings.PITCHER_MODEL
    es.effective_embedding_model = lambda: "nomic-embed-text-v2-moe"
    settings.PITCHER_MODEL = ""
    try:
        models = mc.expected_models()
        assert models[:2] == [settings.CURATOR_MODEL, settings.SUMMARIZER_MODEL]
        assert "nomic-embed-text-v2-moe" in models, "the stored profile's model, not settings.EMBEDDING_MODEL"
        assert len(models) == len(set(models))
        settings.PITCHER_MODEL = "curatarr-pitcher"
        assert mc.expected_models()[-1] == "curatarr-pitcher"
    finally:
        es.effective_embedding_model, settings.PITCHER_MODEL = orig_profile, orig_pitcher
    del real


def test_missing_models_asks_ollama_show_for_each_expected_model():
    run, calls = _runner(present={"curatarr-curator", "nomic-embed-text-v2-moe"})
    missing = mc.missing_models(run=run, models=["curatarr-curator", "curatarr-summarizer",
                                                 "nomic-embed-text-v2-moe"])
    assert missing == ["curatarr-summarizer"]
    assert calls[0][:2] == ["ollama", "list"] and [c[2] for c in calls[1:]] == [
        "curatarr-curator", "curatarr-summarizer", "nomic-embed-text-v2-moe"]


def test_ollama_not_answering_is_none_not_everything_missing():
    run, calls = _runner(list_rc=1)
    assert mc.missing_models(run=run, models=["curatarr-curator"]) is None
    assert len(calls) == 1, "no `ollama show` when the daemon does not even list"

    def boom(cmd, **kw):
        raise FileNotFoundError("ollama")
    assert mc.missing_models(run=boom, models=["x"]) is None


def test_the_launchers_share_the_list():
    bat = (_ROOT / "start.bat").read_text(encoding="utf-8")
    assert "build_models.py --check" in bat, "start.bat asks the shared check"
    assert "ollama show nomic-embed-text" not in bat, "no hardcoded model probe in the launcher"
    assert "_MODELS_RC" in bat and "not answering" in bat, "Ollama-down is its own branch, not a build"
    tray = (_ROOT / "src/tray_app.py").read_text(encoding="utf-8")
    assert "from src.services.model_check import missing_models" in tray
    assert '"ollama", "show"' not in tray, "the tray no longer keeps its own probe"
    builder = (_ROOT / "build_models.py").read_text(encoding="utf-8")
    assert '"--check" in sys.argv' in builder and "def check(" in builder


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
