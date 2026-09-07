"""The launchers pull the interpreter up to requirements.txt; the server only reports.

2026-09-07: start.bat's sentinel import proved presence, not version, so a
Dependabot bump (pydantic 2.13.4 installed, 2.13.5 pinned) passed unnoticed;
it also imported `Crypto`, which is neither pinned nor used, so a fresh
install ran pip on every start. src/deps_check.py replaces both.

    python tests/test_deps_check.py
"""
import pathlib
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src import deps_check as dc  # noqa: E402

SAMPLE = """# Curatarr — Requirements
fastapi==0.141.1
uvicorn[standard]==0.52.4   # extras are not part of the name
PyJWT==2.13.0
pillow==12.3.0
some-lib>=1.0               # a range is not a pin
"""


def _fake_installed(table):
    return lambda name: table.get(name.lower())


def test_pins_are_parsed_without_extras_comments_or_ranges():
    pins = dc.parse_pins(SAMPLE)
    assert [(p.name, p.version) for p in pins] == [
        ("fastapi", "0.141.1"), ("uvicorn", "0.52.4"), ("PyJWT", "2.13.0"), ("pillow", "12.3.0")]


def test_check_classifies_missing_drift_and_ok():
    with tempfile.TemporaryDirectory() as d:
        req = pathlib.Path(d) / "requirements.txt"
        req.write_text(SAMPLE, encoding="utf-8")
        rep = dc.check(req, installed=_fake_installed({
            "fastapi": "0.141.1", "uvicorn": "0.52.3", "pyjwt": "2.13.0"}))   # pillow absent
    assert [p.name for p in rep.missing] == ["pillow"]
    assert [p.name for p in rep.drift] == ["uvicorn"]
    assert not rep.clean
    s = rep.summary()
    assert "1 missing, 1 differ" in s and "uvicorn: installed 0.52.3, pinned 0.52.4" in s
    assert dc.INSTALL_CMD in s, "the summary must carry the fix, not just the complaint"
    d_ = rep.as_dict()
    assert d_["clean"] is False and d_["drift"][0]["name"] == "uvicorn" and d_["command"] == dc.INSTALL_CMD


def test_version_noise_is_not_drift():
    assert dc.Pin("x", "1.0", installed="1.0.0").ok
    assert dc.Pin("x", "2.13.5", installed="v2.13.5").ok
    assert not dc.Pin("x", "2.13.5", installed="2.13.5rc1").ok
    assert not dc.Pin("x", "2.13.5", installed=None).ok


def test_install_uses_this_interpreter_and_reports_failure():
    calls = []

    class _R:
        def __init__(self, code):
            self.returncode, self.stdout, self.stderr = code, "", "boom"

    def run_ok(argv, **kw):
        calls.append(argv)
        return _R(0)

    assert dc.install(pathlib.Path("req.txt"), run=run_ok) is True
    argv = calls[0]
    assert argv[:4] == [sys.executable, "-m", "pip", "install"] and argv[4:6] == ["-r", "req.txt"]
    assert dc.install(pathlib.Path("req.txt"), run=lambda argv, **kw: _R(1)) is False


def test_main_exit_codes_follow_the_report():
    with tempfile.TemporaryDirectory() as d:
        req = pathlib.Path(d) / "requirements.txt"
        req.write_text("curatarr-surely-not-installed-xyz==9.9.9\n", encoding="utf-8")
        assert dc.main(["--requirements", str(req)]) == 1
        real_install = dc.install
        dc.install = lambda r: True          # "pip ran" — but the pin is still not installed
        try:
            assert dc.main(["--requirements", str(req), "--install"]) == 1
        finally:
            dc.install = real_install
        req.write_text("", encoding="utf-8")   # nothing pinned = nothing to complain about
        assert dc.main(["--requirements", str(req)]) == 0


def test_the_real_requirements_file_parses_to_named_pins():
    pins = dc.parse_pins((_ROOT / "requirements.txt").read_text(encoding="utf-8"))
    names = {p.name.lower() for p in pins}
    assert {"fastapi", "uvicorn", "pydantic", "chromadb"} <= names
    assert all(p.version[0].isdigit() for p in pins), "every entry is a hard pin"


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
