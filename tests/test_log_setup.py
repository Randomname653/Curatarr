"""Test processes log to data/logs/tests.log, never into the app's live log.

2026-09-27: every suite that imports src.main runs init_logging, and its
file handler opened data/logs/curatarr.log. Two "[slow] GET
/api/history/recent" lines from a battery run sat in the live log while
the app was down, reading like traffic.

    python tests/test_log_setup.py
"""
import logging
import pathlib
import sys
from logging.handlers import RotatingFileHandler

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src import log_setup  # noqa: E402
from src.paths import LOG_DIR  # noqa: E402

LIVE = LOG_DIR / "curatarr.log"
TESTS = LOG_DIR / "tests.log"


def test_a_script_under_tests_logs_to_tests_log():
    for script in (_ROOT / "tests" / "run_all.py", _ROOT / "tests" / "test_history_router.py"):
        assert log_setup.log_file(str(script)) == TESTS, script


def test_the_app_and_its_launch_steps_keep_the_live_log():
    uvicorn = pathlib.Path(sys.prefix) / "Lib" / "site-packages" / "uvicorn" / "__main__.py"
    for script in (uvicorn, _ROOT / "curatarr_tray.pyw", _ROOT / "src" / "deps_lock.py",
                   _ROOT / "tests", "-c", ""):
        assert log_setup.log_file(str(script)) == LIVE, script


def test_this_process_is_a_test_process():
    """run_all.py starts each suite as a script under tests/, or itself for
    bare suites, so no battery run reaches the live log."""
    assert log_setup.log_file() == TESTS, getattr(sys.modules["__main__"], "__file__", None)


def test_init_logging_opens_that_file():
    root = logging.getLogger()
    before = list(root.handlers)
    done = getattr(log_setup.init_logging, "_done", False)
    log_setup.init_logging._done = False
    try:
        log_setup.init_logging("INFO")
        opened = [pathlib.Path(h.baseFilename) for h in root.handlers
                  if h not in before and isinstance(h, RotatingFileHandler)]
        assert opened == [TESTS], opened
    finally:
        for h in [h for h in root.handlers if h not in before]:
            root.removeHandler(h)
            h.close()
        log_setup.init_logging._done = done


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
