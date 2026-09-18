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
    assert L.lane(L.SUMMARIZER, pressed=True, game=False, ram=True) == L.CPU
    assert L.lane(L.CURATOR, pressed=True, game=False, ram=True) == L.NONE, \
        "19.9 GB cannot load on a held card and is pointless on the CPU"
    p = L.placement(L.SUMMARIZER, pressed=True, game=False, ram=True)
    assert p == {"num_gpu": 0, "num_thread": settings.LLM_CPU_THREADS}, p
    # The shipped default, not the value this install happens to run: an
    # operator who set eight threads must not fail the suite.
    from src.config import Settings
    assert Settings.model_fields["LLM_CPU_THREADS"].default == 6, \
        "six threads measured as fast as twelve, half the CPU stays free"
    assert L.placement(L.CURATOR, pressed=True, game=False) == {"num_gpu": 99}, \
        "a caller that ignores available() fails fast instead of grinding on the CPU"
    ok, why = L.available(L.SUMMARIZER, pressed=True, game=False, ram=True)
    assert ok is True and why == ""
    assert L.available(L.CURATOR, pressed=True, game=False, ram=True)[0] is False


def test_a_real_game_parks_everything():
    assert L.lane(L.SUMMARIZER, pressed=True, game=True) == L.NONE, \
        "a game owns the whole box — its CPU threads are not ours to take"
    assert L.lane(L.CURATOR, pressed=True, game=True) == L.NONE


def test_the_lane_can_be_switched_off():
    old = settings.LLM_CPU_LANE
    settings.LLM_CPU_LANE = False
    try:
        assert L.lane(L.SUMMARIZER, pressed=True, game=False, ram=True) == L.NONE
        assert L.placement(L.SUMMARIZER, pressed=True, game=False, ram=True) == {"num_gpu": 99}
    finally:
        settings.LLM_CPU_LANE = old
    assert L.lane(L.SUMMARIZER, pressed=True, game=False, ram=True) == L.CPU


def test_the_lane_also_needs_memory():
    """Measured: one summariser run took 9.7 GB of system RAM while the
    program on the card held 29 of 64 GB. Cores are not the only cost."""
    assert L.lane(L.SUMMARIZER, pressed=True, game=False, ram=False) == L.NONE, \
        "no room in memory is no lane — swapping helps nobody"
    assert L.placement(L.SUMMARIZER, pressed=True, game=False, ram=False) == {"num_gpu": 99}

    from src.config import Settings
    assert Settings.model_fields["LLM_CPU_MIN_FREE_MB"].default == 12000, \
        "9.7 GB measured plus room for the program holding the card"
    floor = L.min_free_mb()
    assert L.ram_ok(reader=lambda: floor - 1) == (False, floor - 1)
    assert L.ram_ok(reader=lambda: floor) == (True, floor)
    assert L.ram_ok(reader=lambda: None) == (True, None), \
        "a host we cannot measure keeps its background work"
    assert L.ram_ok(reader=lambda: (_ for _ in ()).throw(RuntimeError("x"))) == (True, None)

    old = settings.LLM_CPU_MIN_FREE_MB
    try:
        settings.LLM_CPU_MIN_FREE_MB = -5
        assert L.min_free_mb() == 0
        settings.LLM_CPU_MIN_FREE_MB = "nonsense"
        assert L.min_free_mb() == 12000
    finally:
        settings.LLM_CPU_MIN_FREE_MB = old


def test_the_notice_only_promises_progress_that_is_happening():
    open_msg = L.busy_message("17544/24564 MB, 92 %", lane_open=True)
    shut_msg = L.busy_message("17544/24564 MB, 92 %", lane_open=False)
    assert "keep running on the processor" in open_msg
    assert "keep running on the processor" not in shut_msg, \
        "with the memory too tight, promising background progress would be a lie"
    assert "waiting too" in shut_msg and "nothing is lost" in shut_msg
    assert "17544/24564 MB, 92 %" in open_msg and "17544/24564 MB, 92 %" in shut_msg


def test_the_thread_budget_is_clamped():
    old = settings.LLM_CPU_THREADS
    try:
        for given, want in ((0, 1), (-4, 1), (999, 64), (12, 12)):
            settings.LLM_CPU_THREADS = given
            assert L._cpu_threads() == want, given
    finally:
        settings.LLM_CPU_THREADS = old


def test_the_busy_notice_names_the_cause_and_the_way_out():
    # lane_open is passed explicitly: left to itself the notice reads the
    # live machine, and this install's memory is not the subject here.
    msg = L.busy_message("17467/24564 MB, 90 %", lane_open=True)
    assert "17467/24564 MB, 90 %" in msg
    assert "no room to load my model" in msg
    assert "end the job" in msg and "processor in the background" in msg
    assert "\n\n" in msg, "two paragraphs — it renders as a curator reply"
    bare = L.busy_message("", lane_open=True)
    assert "It is holding" not in bare and bare.startswith("The GPU is busy")


def test_the_status_shape():
    s = L.status()
    assert set(s) == {"gpu_pressed", "game", "reason", "curator", "summarizer",
                      "cpu_threads", "cpu_lane", "ram_free_mb", "ram_min_mb"}
    assert s["ram_min_mb"] == L.min_free_mb()
    assert s["ram_free_mb"] is None or isinstance(s["ram_free_mb"], int)
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
    assert '"lane": lane,' in pm and '"lane_reason": reason,' in pm
    assert 'get_state("llm_lane")' in pm and "before the first tick" in pm
    assert 'reason = st["reason"] or ""' in pm, \
        "the pre-first-tick fallback carries its own occupancy, not an empty state row"

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
    knobs = {"gpu_pressure_gate", "llm_cpu_lane", "llm_cpu_threads", "llm_cpu_min_free_mb"}
    assert knobs <= ids, "the wizard knows them"
    cfg = sw.current_env_config()
    # Shape, not the operator's choice — this install may run any of them.
    assert isinstance(cfg["llm_cpu_lane"], bool) and isinstance(cfg["gpu_pressure_gate"], bool)
    assert isinstance(cfg["llm_cpu_threads"], int) and cfg["llm_cpu_threads"] >= 1

    from src.routers.setup import ReconfigureRequest, SetupCompleteRequest
    for model in (ReconfigureRequest, SetupCompleteRequest):
        assert knobs <= set(model.model_fields), model.__name__

    # write_env round-trip into a throwaway file — never the live .env
    tmp = pathlib.Path(tempfile.mkdtemp()) / ".env"
    tmp.write_text("KEEP_ME=yes\n", encoding="utf-8")
    old_path = sw.ENV_PATH
    sw.ENV_PATH = tmp
    try:
        changed = sw.merge_env_config(cfg, {"llm_cpu_lane": False, "llm_cpu_threads": 12,
                                            "gpu_pressure_gate": False,
                                            "llm_cpu_min_free_mb": 9000})
        sw.write_env(changed)
        written = tmp.read_text(encoding="utf-8")
    finally:
        sw.ENV_PATH = old_path
    assert "LLM_CPU_LANE=false" in written and "LLM_CPU_THREADS=12" in written
    assert "GPU_PRESSURE_GATE=false" in written and "LLM_CPU_MIN_FREE_MB=9000" in written
    assert "KEEP_ME=yes" in written, "hand-added keys survive a rewrite"

    js = (_ROOT / "frontend/js/settings.js").read_text(encoding="utf-8")
    card = js.split("{key: 'gpu'")[1].split("]},")[0]
    for fid in knobs:
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
