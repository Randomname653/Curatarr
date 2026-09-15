"""The CPU lane: what runs where while another program holds the GPU.

No Ollama, no GPU — the pressure and game signals are injected. Covers the
routing rules, the request options the call sites receive, the honest chat
notice and the wiring into llm_utils, the custodian, chat and enrichment.

    python tests/test_llm_lane.py
"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.config import settings                      # noqa: E402
from src.services import llm_lane as L               # noqa: E402


def test_a_free_gpu_routes_everything_to_the_card():
    for role in (L.SUMMARIZER, L.CURATOR):
        assert L.lane(role, pressed=False) == L.GPU
        assert L.placement(role, pressed=False) == {"num_gpu": 99}
        assert L.available(role, pressed=False) == (True, "")


def test_a_held_gpu_moves_the_summarizer_to_the_cpu_and_parks_the_curator():
    assert L.lane(L.SUMMARIZER, pressed=True, game=False) == L.CPU
    assert L.lane(L.CURATOR, pressed=True, game=False) == L.NONE, \
        "19.9 GB cannot load on a held card and is pointless on the CPU"
    p = L.placement(L.SUMMARIZER, pressed=True, game=False)
    assert p == {"num_gpu": 0, "num_thread": settings.LLM_CPU_THREADS}, p
    assert p["num_thread"] == 6, "six threads measured as fast as twelve, half the CPU stays free"
    assert L.placement(L.CURATOR, pressed=True, game=False) == {"num_gpu": 99}, \
        "a caller that ignores available() fails fast instead of grinding on the CPU"
    ok, why = L.available(L.SUMMARIZER, pressed=True, game=False)
    assert ok is True and why == ""
    assert L.available(L.CURATOR, pressed=True, game=False)[0] is False


def test_a_real_game_parks_everything():
    assert L.lane(L.SUMMARIZER, pressed=True, game=True) == L.NONE, \
        "a game owns the whole box — its CPU threads are not ours to take"
    assert L.lane(L.CURATOR, pressed=True, game=True) == L.NONE


def test_the_lane_can_be_switched_off():
    old = settings.LLM_CPU_LANE
    settings.LLM_CPU_LANE = False
    try:
        assert L.lane(L.SUMMARIZER, pressed=True, game=False) == L.NONE
        assert L.placement(L.SUMMARIZER, pressed=True, game=False) == {"num_gpu": 99}
    finally:
        settings.LLM_CPU_LANE = old
    assert L.lane(L.SUMMARIZER, pressed=True, game=False) == L.CPU


def test_the_thread_budget_is_clamped():
    old = settings.LLM_CPU_THREADS
    try:
        for given, want in ((0, 1), (-4, 1), (999, 64), (12, 12)):
            settings.LLM_CPU_THREADS = given
            assert L._cpu_threads() == want, given
    finally:
        settings.LLM_CPU_THREADS = old


def test_the_busy_notice_names_the_cause_and_the_way_out():
    msg = L.busy_message("17467/24564 MB, 90 %")
    assert "17467/24564 MB, 90 %" in msg
    assert "no room to load my model" in msg
    assert "end the job" in msg and "processor in the background" in msg
    assert "\n\n" in msg, "two paragraphs — it renders as a curator reply"
    bare = L.busy_message("")
    assert "It is holding" not in bare and bare.startswith("The GPU is busy")


def test_the_status_shape():
    s = L.status()
    assert set(s) == {"gpu_pressed", "game", "reason", "curator", "summarizer",
                      "cpu_threads", "cpu_lane"}
    assert s["curator"] in (L.GPU, L.CPU, L.NONE) and s["summarizer"] in (L.GPU, L.CPU, L.NONE)


def test_the_options_helpers_carry_the_placement():
    from src.services.llm_utils import CURATOR_NUM_CTX, curator_options, ollama_options
    o = ollama_options(temperature=0.1, num_predict=700)["options"]
    assert o["temperature"] == 0.1 and o["num_predict"] == 700
    assert "num_gpu" in o, "placement always lands in the options"
    c = curator_options(temperature=0.7)["options"]
    assert c["num_gpu"] == 99 and c["num_ctx"] == CURATOR_NUM_CTX, \
        "the curator is pinned to the card whatever the lane says"
    assert curator_options(0.7, 100, num_gpu=0)["options"]["num_gpu"] == 0, \
        "an explicit caller still wins"
    assert ollama_options(0.1, 10, num_thread=3)["options"]["num_thread"] == 3


def test_the_watcher_records_the_lane_and_the_badge_names_it():
    sched = (_ROOT / "src/services/scheduler.py").read_text(encoding="utf-8")
    watcher = sched.split("async def job_game_watcher()")[1].split("\nasync def ")[0]
    assert 'set_state("llm_lane", mode)' in watcher and 'set_state("llm_lane_reason"' in watcher
    assert 'logger.info("[lane] %s -> %s%s"' in watcher, "every change lands in the log"
    assert "if mode != previous:" in watcher, "logged on transition, not every 30 s"
    for mode in ('"game"', '"cpu"', '"paused"', '"free"'):
        assert mode in watcher, mode

    pm = (_ROOT / "src/routers/process_monitor.py").read_text(encoding="utf-8")
    assert '"lane": lane,' in pm and '"lane_reason"' in pm
    assert 'get_state("llm_lane")' in pm and "before the first tick" in pm

    js = (_ROOT / "frontend/js/game.js").read_text(encoding="utf-8")
    assert "export function _renderLaneBadge" in js
    for key in ("game:", "cpu:", "paused:"):
        assert key in js.split("const LANE_BADGE")[1][:900], key
    assert "indicator.classList.toggle('busy'" in js, "amber for a held card, green stays for a game"
    assert "statusR.lane_reason" in js, "the occupancy shows in the tooltip"
    html = (_ROOT / "frontend/index.html").read_text(encoding="utf-8")
    assert 'id="game-indicator-label"' in html
    css = (_ROOT / "frontend/css/app.css").read_text(encoding="utf-8")
    assert "#game-indicator.busy" in css and "var(--amber)" in css.split("#game-indicator.busy")[1][:200]


def test_the_setting_is_asked_at_setup_and_changeable_later():
    import tempfile
    import src.services.setup_wizard as sw

    ids = {f["id"] for f in sw.SETUP_FIELDS}
    assert {"gpu_pressure_gate", "llm_cpu_lane", "llm_cpu_threads"} <= ids, "the wizard knows them"
    cfg = sw.current_env_config()
    assert cfg["llm_cpu_lane"] is True and cfg["llm_cpu_threads"] == 6 and cfg["gpu_pressure_gate"] is True

    from src.routers.setup import ReconfigureRequest, SetupCompleteRequest
    for model in (ReconfigureRequest, SetupCompleteRequest):
        assert {"gpu_pressure_gate", "llm_cpu_lane", "llm_cpu_threads"} <= set(model.model_fields), model.__name__

    # write_env round-trip into a throwaway file — never the live .env
    tmp = pathlib.Path(tempfile.mkdtemp()) / ".env"
    tmp.write_text("KEEP_ME=yes\n", encoding="utf-8")
    old_path = sw.ENV_PATH
    sw.ENV_PATH = tmp
    try:
        changed = sw.merge_env_config(cfg, {"llm_cpu_lane": False, "llm_cpu_threads": 12,
                                            "gpu_pressure_gate": False})
        sw.write_env(changed)
        written = tmp.read_text(encoding="utf-8")
    finally:
        sw.ENV_PATH = old_path
    assert "LLM_CPU_LANE=false" in written and "LLM_CPU_THREADS=12" in written
    assert "GPU_PRESSURE_GATE=false" in written
    assert "KEEP_ME=yes" in written, "hand-added keys survive a rewrite"

    js = (_ROOT / "frontend/js/settings.js").read_text(encoding="utf-8")
    card = js.split("{key: 'gpu'")[1].split("]},")[0]
    for fid in ("gpu_pressure_gate", "llm_cpu_lane", "llm_cpu_threads"):
        assert fid in card, fid
    assert "toggle: true" in card and "number: true" in card
    assert "card.hint" in js, "the card explains itself"
    wiz = (_ROOT / "frontend/js/setup.js").read_text(encoding="utf-8")
    assert 'id="s-cpu-lane"' in wiz and 'id="s-cpu-threads"' in wiz
    assert "state.setupData.llm_cpu_lane = !!document.getElementById('s-cpu-lane')?.checked;" in wiz
    assert "state.setupData.llm_cpu_threads = parseInt(" in wiz


def test_the_wiring():
    utils = (_ROOT / "src/services/llm_utils.py").read_text(encoding="utf-8")
    assert "**placement(SUMMARIZER), **extra" in utils
    assert '"num_ctx": CURATOR_NUM_CTX, "num_gpu": 99' in utils

    pm = (_ROOT / "src/services/process_monitor.py").read_text(encoding="utf-8")
    assert "def game_process_running() -> bool:" in pm and "def gpu_pressure_reason() -> str:" in pm
    assert "return gpu_pressure() or game_process_running()" in pm

    cust = (_ROOT / "src/services/data_custodian.py").read_text(encoding="utf-8")
    assert 'llm_role: str = "curator"' in cust, "curator is the safe default"
    assert cust.count('llm_role="summarizer"') == 6, "the six summariser-class tasks"
    for job in ("memory_catchup", "custodian_enrich", "custodian_signif",
                "custodian_recept", "custodian_taste", "lyrics_profile"):
        entry = cust.split(f'Task("{job}"')[1].split("),")[0]
        assert 'llm_role="summarizer"' in entry, job
    for job in ("arr_sync", "chat_starters", "custodian_recs", "plex_collections"):
        entry = cust.split(f'Task("{job}"')[1].split("),")[0]
        assert "llm_role" not in entry, f"{job} drives the curator — it must keep waiting"
    assert 'result": "skipped (GPU busy)"' in cust

    chat = (_ROOT / "src/routers/chat.py").read_text(encoding="utf-8")
    assert "from src.services.llm_lane import busy_message, curator_available" in chat
    assert "async def _gpu_busy_reply()" in chat
    head, _, tail = chat.partition("async def _gpu_busy_reply()")
    assert "_rl.CHAT_IN_FLIGHT.enter_or_409" not in head, \
        "the notice returns before the in-flight guard is taken"
    assert 'await curator_start("chat")' not in head, "and before the priority gate"
    assert "# 1. CONTEXT PRE-LOADING" not in head, \
        "and before the context assembly — nothing is built for an answer that cannot come"
    branch = chat.split("_curator_ok, _curator_why = curator_available()")[1].split("# 1. CONTEXT")[0]
    assert '_save_message(user.id, "user"' in branch and '_save_message(user.id, "assistant"' in branch, \
        "the thread still reads as a normal exchange"

    enr = (_ROOT / "src/routers/enrichment.py").read_text(encoding="utf-8")
    assert "def _llm_yields() -> bool:" in enr and "if _llm_yields():" in enr
    assert "if is_game_running():" not in enr, "the bare gate is gone from the worker"

    cfg = (_ROOT / "src/config.py").read_text(encoding="utf-8")
    assert "LLM_CPU_LANE: bool = True" in cfg and "LLM_CPU_THREADS: int = 6" in cfg


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
