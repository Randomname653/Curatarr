"""The launchers pick ONE interpreter, whatever PATH says, and every start
names it in the log.

2026-09-25 / 09-27: start.bat ran `python` from PATH. The machine part of
PATH (where PlatformIO keeps its own Python) is missing in some consoles,
so one start ran on PlatformIO's Python and the next on Python 3.12 — two
installs, each raised only when it happened to run, both shared with other
tools whose pins the raises broke. Order now: a venv in the folder, else
`py -3.12`, else PATH.

    python tests/test_launcher_interpreter.py
"""
import pathlib
import re
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


def _code_lines(text: str) -> list:
    return [l for l in text.splitlines() if l.strip() and not l.strip().upper().startswith("REM")]


def test_start_bat_resolves_one_interpreter_in_a_fixed_order():
    bat = (_ROOT / "start.bat").read_text(encoding="utf-8")
    assert "setlocal EnableDelayedExpansion" in bat
    order = [bat.index('if exist "venv\\Scripts\\python.exe"'),
             bat.index('if exist ".venv\\Scripts\\python.exe"'),
             bat.index("py -3.12 -c"),
             bat.index('set "PY=python"')]
    assert order == sorted(order), "venv, .venv, py -3.12, PATH — in that order"
    assert "echo  Python: !PY!" in bat, "the window says which one runs"


def test_every_python_call_in_start_bat_goes_through_the_choice():
    bat = (_ROOT / "start.bat").read_text(encoding="utf-8")
    code = _code_lines(bat)
    bare = [l for l in code if re.match(r"\s*python(\.exe)?\s", l)]
    assert not bare, f"a bare python call follows PATH again: {bare}"
    calls = [l for l in code if '"!PY!"' in l]
    assert len(calls) == 6, calls
    assert any("-m uvicorn src.main:app" in l for l in calls)
    assert any("-m src.deps_lock --apply" in l for l in calls)
    # delayed expansion eats every other "!": none may exist
    assert "!" not in bat.replace("!PY!", ""), "a literal ! in start.bat breaks under delayed expansion"


def test_the_tray_launcher_uses_the_same_order():
    bat = (_ROOT / "start_tray.bat").read_text(encoding="utf-8")
    assert "setlocal EnableDelayedExpansion" in bat
    order = [bat.index('if exist "venv\\Scripts\\pythonw.exe"'),
             bat.index('if exist ".venv\\Scripts\\pythonw.exe"'),
             bat.index("py -3.12 -c"),
             bat.index('set "PYW=pythonw.exe"')]
    assert order == sorted(order)
    assert 'start "" "!PYW!" curatarr_tray.pyw' in bat
    assert not [l for l in _code_lines(bat) if re.match(r'\s*start "" pythonw', l)]
    assert "!" not in bat.replace("!PYW!", "")


def test_every_start_names_its_interpreter_in_the_log():
    main = (_ROOT / "src/main.py").read_text(encoding="utf-8")
    assert 'logger.info("[python] %s (%s)", _sys.executable, _platform.python_version())' in main
    lock = (_ROOT / "src/deps_lock.py").read_text(encoding="utf-8")
    assert 'file_log.info("[lock] interpreter: %s", sys.executable)' in lock


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
