"""A GPU held by something other than Ollama counts as a game.

2026-09-14: the owner's image-generation job held 23.9 of 24.5 GB on the
4090 at 100 %; game_active stayed 0 (the detector knew game process names
only), the enrichment logged summariser timeouts for an hour and the
custodian kept starting LLM work. This pins the gate: pressure must
persist, Ollama's own load never counts, a host without nvidia-smi is
left alone.

    python tests/test_gpu_pressure.py
"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.config import settings  # noqa: E402
from src.services import process_monitor as pm  # noqa: E402

SAT = (23900.0, 24564.0, 100.0)      # the live numbers
FREE = (900.0, 24564.0, 3.0)


def _reset():
    pm._gpu_cache.update(at=0.0, busy=False, since=0.0, reason="")
    pm._gpu_unavailable_until = 0.0
    settings.GPU_PRESSURE_GATE = True


def test_pressure_counts_only_after_it_persisted():
    _reset()
    assert pm.gpu_pressure(smi=lambda: SAT, ps=lambda: [], now=1000.0) is False, "first look: a model load spikes too"
    assert pm.gpu_pressure(smi=lambda: SAT, ps=lambda: [], now=1030.0) is False
    assert pm.gpu_pressure(smi=lambda: SAT, ps=lambda: [], now=1046.0) is True, "45 s of pressure: busy"
    assert pm._gpu_cache["reason"] == "23900/24564 MB, 100 %"
    assert pm.gpu_pressure(smi=lambda: FREE, ps=lambda: [], now=1100.0) is False, "free again"
    assert pm._gpu_cache["since"] == 0.0


def test_ollamas_own_load_never_counts_and_an_unreachable_ollama_does():
    _reset()
    loaded = lambda: ["curatarr-summarizer:latest"]  # noqa: E731
    for t in (1000.0, 1050.0, 1100.0):
        assert pm.gpu_pressure(smi=lambda: SAT, ps=loaded, now=t) is False
    _reset()
    assert pm.gpu_pressure(smi=lambda: SAT, ps=lambda: None, now=1000.0) is False
    assert pm.gpu_pressure(smi=lambda: SAT, ps=lambda: None, now=1050.0) is True, "Ollama not answering while the GPU is full: not ours"


def test_low_free_memory_counts_like_high_utilisation():
    _reset()
    tight = (22500.0, 24564.0, 20.0)                 # 2.0 GB free, idle: no room for the summariser
    assert pm.gpu_pressure(smi=lambda: tight, ps=lambda: [], now=1000.0) is False
    assert pm.gpu_pressure(smi=lambda: tight, ps=lambda: [], now=1050.0) is True


def test_a_host_without_nvidia_smi_is_left_alone_and_the_gate_can_be_switched_off():
    _reset()
    assert pm.gpu_pressure(smi=lambda: None, ps=lambda: [], now=1000.0) is False
    assert pm._gpu_unavailable_until == 1600.0, "asked again after ten minutes, not every call"
    _reset()
    settings.GPU_PRESSURE_GATE = False
    assert pm.gpu_pressure(smi=lambda: SAT, ps=lambda: [], now=1000.0) is False
    assert pm.gpu_pressure(smi=lambda: SAT, ps=lambda: [], now=1100.0) is False
    settings.GPU_PRESSURE_GATE = True


def test_is_game_running_reports_the_pressure():
    _reset()
    orig = pm.gpu_pressure
    pm.gpu_pressure = lambda **k: True
    try:
        assert pm.is_game_running() is True
    finally:
        pm.gpu_pressure = orig
    _reset()


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
