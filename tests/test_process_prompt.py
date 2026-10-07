"""The "is this a game?" toast asks only about programs that could be one,
and Steam's 64-bit overlay counts as a game signal.

2026-10-07: every running exe nobody had classified got the question. The
"not a game" list held 530 names, 95 of them versions of seven installers
and updaters (68 InstallShield extractors _is####.exe alone), and
gameoverlayui64.exe, Steam's overlay since it went 64-bit, sat among them
while the detection only knew gameoverlayui.exe.

    python tests/test_process_prompt.py
"""
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.services import process_monitor as pm  # noqa: E402


class _Proc:
    def __init__(self, name, pid, rss_mb):
        mem = None if rss_mb is None else types.SimpleNamespace(rss=rss_mb * 1024 * 1024)
        self.info = {"name": name, "pid": pid, "memory_info": mem}
        self.pid = pid


def _run(procs, games=(), ignored=()):
    real = (pm.psutil.process_iter, pm._classified)
    exact = frozenset(n.lower() for n in (*games, *ignored))
    families = frozenset(pm.process_family(n) for n in ignored)
    pm.psutil.process_iter = lambda attrs=None: iter(procs)
    pm._classified = lambda: (exact, families)
    try:
        return [p["name"] for p in pm.get_unknown_processes()]
    finally:
        pm.psutil.process_iter, pm._classified = real


def test_versions_and_hashes_fold_into_one_family():
    f = pm.process_family
    assert f("_is4993.exe") == f("_is3b83.exe") == f("_IS96A9.EXE") == "_is#.exe"
    assert f("am_delta_patch_1.449.533.0.exe") == f("am_delta_patch_1.453.245.0.exe")
    assert f("vortex-setup-2.0.1.exe") == f("vortex-setup-2.5.0.exe") == "vortex-setup-#.exe"
    assert (f("codesetup-stable-8b640eef5a6c6089c029249d48efa5c99adf7d51.exe")
            == f("codesetup-stable-08d4889f9ec4a1685d257b9b95de036c8e1ce1e5.exe"))
    assert (f("steelseriesgg117.0.0patchfrom116.0.0setup.exe")
            == f("steelseriesgg120.0.0patchfrom119.0.0setup.exe"))
    assert (f("microsoftedge_x64_148.0.3967.54.exe")
            == f("microsoftedge_x64_151.0.4129.59_150.0.4078.105.exe"))
    assert f("windowsdesktop-runtime-10.0.8-win-x64.exe") == f("windowsdesktop-runtime-8.0.8-win-x64.exe")
    assert f("fallout4.exe") == f("fallout76.exe"), "why games must match exactly"
    assert f("discord.exe") != f("discord_clips.exe")


def test_only_big_unknown_non_installers_are_asked_about():
    procs = [
        _Proc("_is9999.exe", 1, 900),                 # an ignored family
        _Proc("vortex-setup-3.0.0.exe", 2, 600),      # ignored family and an installer
        _Proc("fallout4.exe", 3, 3000),               # shares a family with a GAME: asked
        _Proc("awk.exe", 4, 5),                       # too small to need the card
        _Proc("NewGame.exe", 5, 2000),
        _Proc("steam.exe", 6, 800),                   # a launcher, never asked
        _Proc("bigsetup.exe", 7, 800),                # an installer
        _Proc("crashpad_handler.exe", 8, 600),        # a crash handler
        _Proc("denied.exe", 9, None),                 # memory not readable
        _Proc("multi.exe", 10, 10),
        _Proc("multi.exe", 11, 900),                  # the big instance counts
        _Proc("multi.exe", 12, 900),
        _Proc("helper", 13, 900),                     # not an .exe
        _Proc("fallout76.exe", 14, 4000),             # classified
    ]
    asked = _run(procs, games=["fallout76.exe"],
                 ignored=["_is4993.exe", "vortex-setup-2.0.1.exe"])
    assert asked == ["fallout4.exe", "NewGame.exe", "multi.exe"], asked


def test_a_not_a_game_answer_covers_the_next_version_only():
    procs = [_Proc("am_delta_patch_1.460.1.0.exe", 1, 700),
             _Proc("tool-2.0.exe", 2, 700), _Proc("tool-x.exe", 3, 700)]
    asked = _run(procs, ignored=["tool-1.9.exe"])
    assert asked == ["tool-x.exe"], asked


def test_steams_64_bit_overlay_is_a_game_signal():
    assert "gameoverlayui64.exe" in pm.GAME_LAUNCHER_SIGNALS
    assert "gameoverlayui.exe" in pm.GAME_LAUNCHER_SIGNALS
    real = pm.psutil.process_iter
    pm.invalidate_process_cache()
    pm._cached_game_pid = None
    pm.psutil.process_iter = lambda attrs=None: iter([_Proc("GameOverlayUI64.exe", 42, 80)])
    try:
        assert pm.game_process_running() is True
    finally:
        pm.psutil.process_iter = real
        pm.invalidate_process_cache()
        pm._cached_game_pid = None


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
