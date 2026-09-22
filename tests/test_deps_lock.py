"""lock/requirements.txt follows the install, never lowers anything, and
never runs ahead of requirements.txt.

2026-09-22: the first real OSV scan showed why the 17 direct pins were not
enough — the install ran anyio 4.13.0 inside two advisories while the
scanner, resolving the manifest itself, reported 4.9.0. src/deps_lock.py
writes the closure of the pins as installed on the tested machine, raises
packages that fall below it (a merged security bump reaches every
machine) and lets the lock follow anything installed above it.

    python tests/test_deps_lock.py
"""
import pathlib
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src import deps_lock as dl  # noqa: E402
from src import deps_check as dc  # noqa: E402

REQ = """fastapi==0.141.1
uvicorn[standard]==0.53.0   # extras pull their own deps
PyJWT==2.14.0
"""

# a tiny installed world: (spelling, version, requirement strings with extras + markers)
WORLD = {
    "fastapi": ("fastapi", "0.141.1", ["starlette>=0.40", "pydantic>=2", "anyio>=3"]),
    "starlette": ("starlette", "0.50.0", ["anyio>=3.6"]),
    "pydantic": ("pydantic", "2.13.5", ["typing-extensions", 'email-validator; extra == "email"']),
    "anyio": ("anyio", "4.13.0", ["idna>=2.8", "sniffio"]),
    "idna": ("idna", "3.18", []),
    "sniffio": ("sniffio", "1.3.1", []),
    "typing-extensions": ("typing_extensions", "4.15.0", []),
    "uvicorn": ("uvicorn", "0.53.0", ["h11", 'watchfiles; extra == "standard"',
                                      'uvloop; sys_platform != "win32" and extra == "standard"',
                                      'pyyaml; extra == "standard"']),
    "h11": ("h11", "0.16.0", []),
    "watchfiles": ("watchfiles", "1.2.0", ["anyio"]),
    "pyyaml": ("PyYAML", "6.0.3", []),
    "uvloop": ("uvloop", "0.21.0", []),
    "pyjwt": ("PyJWT", "2.14.0", ['cryptography; extra == "crypto"']),
    "email-validator": ("email-validator", "2.2.0", []),
    "cryptography": ("cryptography", "46.0.0", []),
}


def _requires(name):
    return WORLD.get(dl.norm(name))


def test_closure_follows_extras_and_markers_but_not_unasked_extras():
    versions, spelling, missing = dl.closure(dl.direct_pins(REQ), _requires)
    assert missing == []
    assert versions["watchfiles"] == "1.2.0" and versions["pyyaml"] == "6.0.3", "uvicorn[standard] pulls its extra deps"
    assert "email-validator" not in versions, "pydantic's email extra was never asked for"
    assert "cryptography" not in versions, "PyJWT's crypto extra was never asked for"
    on_windows = sys.platform == "win32"
    assert ("uvloop" in versions) is (not on_windows), "a platform marker is evaluated for THIS interpreter"
    assert spelling["typing-extensions"] == "typing_extensions", "the lock writes the distribution's own spelling"
    assert versions["anyio"] == "4.13.0" and versions["idna"] == "3.18"


def test_compare_names_below_above_unlocked_and_unused():
    installed = {"anyio": "4.13.0", "idna": "3.18", "h11": "0.16.0", "sniffio": "1.3.1"}
    lock = "anyio==4.14.2\nidna==3.15\nh11==0.16.0\npywin32==311\n"
    rep = dl.compare(lock, installed)
    assert rep.below == [("anyio", "4.13.0", "4.14.2")], "installed below the lock line: to be raised"
    assert rep.above == [("idna", "3.18", "3.15")], "installed above the lock line: the lock follows"
    assert rep.unlocked == ["sniffio"] and rep.unused == ["pywin32"]
    assert not rep.clean
    s = rep.summary()
    assert "anyio: installed 4.13.0, lock says 4.14.2 (will be raised)" in s and dl.APPLY_CMD in s
    assert dl.compare("anyio==4.13.0\nidna==3.18\nh11==0.16.0\nsniffio==1.3.1\n", installed).clean


def test_merged_keeps_the_higher_lock_line_and_repeats_direct_pins():
    installed = {"anyio": "4.13.0", "idna": "3.18", "fastapi": "0.141.0"}
    rep = dl.compare("anyio==4.14.2\nidna==3.15\nfastapi==0.141.0\n", installed)
    out = dl.merged(rep, dl.direct_pins(REQ))
    assert out["anyio"] == "4.14.2", "a lock line above the install is kept (the raise is retried)"
    assert out["idna"] == "3.18", "the lock follows an install above it"
    assert out["fastapi"] == "0.141.1", "direct pins repeat requirements.txt, whatever is installed"


def test_write_lock_is_idempotent_and_sync_pins_touches_only_pins():
    with tempfile.TemporaryDirectory() as d:
        lock = pathlib.Path(d) / "lock" / "requirements.txt"
        req = pathlib.Path(d) / "requirements.txt"
        req.write_text(REQ, encoding="utf-8")
        assert dl.write_lock({"anyio": "4.13.0", "fastapi": "0.141.0", "pyyaml": "6.0.3"},
                             {"pyyaml": "PyYAML"}, lock) is True
        text = lock.read_text(encoding="utf-8")
        assert text.startswith("# The tested install") and "PyYAML==6.0.3" in text
        assert dl.write_lock({"anyio": "4.13.0", "fastapi": "0.141.0", "pyyaml": "6.0.3"}, {"pyyaml": "PyYAML"}, lock) is False
        assert dl.sync_pins(req, lock) == 1, "fastapi 0.141.0 -> 0.141.1 from requirements.txt"
        after = dl.parse_lock(lock.read_text(encoding="utf-8"))
        assert after["fastapi"] == "0.141.1" and after["anyio"] == "4.13.0" and after["pyyaml"] == "6.0.3"
        assert dl.sync_pins(req, lock) == 0


def test_apply_raises_below_with_this_interpreter_and_then_refreshes():
    calls = []

    class R:
        returncode = 0
        stdout = stderr = ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return R()

    with tempfile.TemporaryDirectory() as d:
        lock = pathlib.Path(d) / "lock" / "requirements.txt"
        req = pathlib.Path(d) / "requirements.txt"
        req.write_text(REQ, encoding="utf-8")
        lock.parent.mkdir()
        lock.write_text("anyio==4.14.2\nidna==3.15\n", encoding="utf-8")
        lines = []
        rep = dl.apply(req, lock, requires=_requires, run=fake_run, log=lines.append)
        assert calls and calls[0][:4] == [sys.executable, "-m", "pip", "install"] and calls[0][4] == "anyio==4.14.2"
        assert len(calls) == 1, "one pip call per package below the lock, nothing else"
        written = dl.parse_lock(lock.read_text(encoding="utf-8"))
        assert written["anyio"] == "4.14.2", "the fake pip did not really upgrade, so the lock keeps the higher line"
        assert written["idna"] == "3.18", "the lock followed the install"
        assert written["fastapi"] == "0.141.1" and "watchfiles" in written, "the whole closure is written"
        assert any("raising 1 package" in l for l in lines)
        assert rep.below == [("anyio", "4.13.0", "4.14.2")], "still below until pip really ran"


def test_apply_without_a_lock_writes_one_and_runs_no_pip():
    calls = []
    with tempfile.TemporaryDirectory() as d:
        lock = pathlib.Path(d) / "lock" / "requirements.txt"
        req = pathlib.Path(d) / "requirements.txt"
        req.write_text(REQ, encoding="utf-8")
        rep = dl.apply(req, lock, requires=_requires, run=lambda *a, **k: calls.append(a), log=lambda s: None)
        assert not calls and lock.exists() and rep.clean
        assert len(dl.parse_lock(lock.read_text(encoding="utf-8"))) >= 10


def test_the_real_lock_parses_and_never_runs_ahead_of_requirements():
    """CI cannot know the tested machine's closure; it can check that the
    lock exists, is a lock, and that no direct pin in it is newer than
    requirements.txt (older = it lags a Dependabot merge until the next
    start, allowed)."""
    lock_text = dl.LOCK.read_text(encoding="utf-8")
    entries = dl.parse_lock(lock_text)
    assert len(entries) > 40, f"the lock lists {len(entries)} packages; the closure of 17 pins is far bigger"
    for line in lock_text.splitlines():
        if line.strip() and not line.startswith("#"):
            assert dl._LINE.match(line), f"not a pin: {line!r}"
    pins = dl.direct_pins(dl.REQUIREMENTS.read_text(encoding="utf-8"))
    pk = dl._packaging()
    assert pk is not None, "packaging must be importable wherever the battery runs"
    Version = pk[1]
    ahead = [(n, entries[n], v) for n, (v, _e) in pins.items() if n in entries and Version(entries[n]) > Version(v)]
    assert not ahead, f"lock lines ahead of requirements.txt (bump the pin, not the lock): {ahead}"
    assert dc.parse_pins(dl.REQUIREMENTS.read_text(encoding="utf-8")), "requirements.txt still parses for deps_check"


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
