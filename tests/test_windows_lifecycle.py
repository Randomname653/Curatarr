"""Windows process hygiene: no console flashes, graceful logoff, clean stops.

* Under the tray (pythonw, no console) every console child got a window of
  its own: nvidia-smi flashed one every ~30 s, during fullscreen games.
* A Windows logoff / shutdown terminated the tray's pythonw outright — no
  lifespan teardown, no WAL checkpoint (pystray never answers the session
  messages).
* Under ``uvicorn --reload`` (start.bat) the web UI's shutdown stopped only
  the worker; the reloader kept the port and the console.
* sync_guard rewrote .stignore in place at every boot — a power loss there
  truncated the user's patterns and the guard block with them.

Runs on any OS: the Win32 parts are exercised through their pure logic.

    python tests/test_windows_lifecycle.py
"""
import importlib
import re
import subprocess
import sys
import tempfile
import threading
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _checks import check  # noqa: E402


# ── no console windows ───────────────────────────────────────────────────────
print("\n-- child processes never open a console window --")
import src.winproc as winproc
check("off Windows: NO_WINDOW adds nothing", sys.platform == "win32" or winproc.NO_WINDOW == {})
real_platform = sys.platform
had_flag = hasattr(subprocess, "CREATE_NO_WINDOW")
try:
    sys.platform = "win32"
    if not had_flag:
        subprocess.CREATE_NO_WINDOW = 0x08000000
    importlib.reload(winproc)
    check("on Windows: CREATE_NO_WINDOW", winproc.NO_WINDOW == {"creationflags": 0x08000000})
finally:
    sys.platform = real_platform
    if not had_flag:
        del subprocess.CREATE_NO_WINDOW
    importlib.reload(winproc)

import src.services.process_monitor as pm
seen = {}


def fake_run(argv, **kw):
    seen.update(kw)
    return types.SimpleNamespace(returncode=0, stdout="1000, 24000, 5\n")
real_run, real_flags = subprocess.run, winproc.NO_WINDOW
subprocess.run = fake_run
winproc.NO_WINDOW = {"creationflags": 0x08000000}
try:
    stats = pm._nvidia_smi()
finally:
    subprocess.run, winproc.NO_WINDOW = real_run, real_flags
check("nvidia-smi (every ~30 s) runs with the no-window flag",
      seen.get("creationflags") == 0x08000000 and stats == (1000.0, 24000.0, 5.0))

# Every direct launch in the app passes the flag (an injected ``run`` is the
# caller's business — model_check gets it from the tray).
launch = re.compile(r"(subprocess\.run|subprocess\.Popen|create_subprocess_exec)\(")
missing = []
for path in list((ROOT / "src").rglob("*.py")):
    text = path.read_text(encoding="utf-8")
    for m in launch.finditer(text):
        depth, i = 0, m.end() - 1
        while i < len(text):
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            if depth == 0:
                break
            i += 1
        call = text[m.start():i + 1]
        if "NO_WINDOW" not in call and "creationflags" not in call:
            missing.append(f"{path.relative_to(ROOT)}:{text.count(chr(10), 0, m.start()) + 1}")
check("every subprocess launch under src/ passes NO_WINDOW / creationflags",
      not missing)
if missing:
    print("        missing: " + ", ".join(missing))
dl = (ROOT / "src/deps_lock.py").read_text(encoding="utf-8")
dc = (ROOT / "src/deps_check.py").read_text(encoding="utf-8")
check("pip / uv from the tray's preflight: no window",
      dl.count("**NO_WINDOW") >= 3 and "**NO_WINDOW" in dc)

# ── Windows logoff / shutdown ────────────────────────────────────────────────
print("\n-- a Windows logoff stops the tray gracefully --")
import src.tray_app as tray
log = []


def handle(msg, wparam):
    return tray._on_session_message(
        msg, wparam, stop=lambda: log.append("stop"), wait=lambda: log.append("wait"),
        block=lambda: log.append("block"), unblock=lambda: log.append("unblock"))

r = handle(tray.WM_QUERYENDSESSION, 0)
check("QUERYENDSESSION: block reason, graceful stop, answer yes",
      r == 1 and log == ["block", "stop"])
log.clear()
r = handle(tray.WM_ENDSESSION, 1)
check("ENDSESSION(TRUE): wait for the teardown, then drop the reason",
      r == 0 and log == ["wait", "unblock"])
log.clear()
r = handle(tray.WM_ENDSESSION, 0)
check("ENDSESSION(FALSE) (cancelled): no wait, reason dropped", r == 0 and log == ["unblock"])
log.clear()
check("anything else goes to DefWindowProc", handle(0x0001, 0) is None and log == [])


def boom():
    raise RuntimeError("no block reason")
stopped = []
try:
    tray._on_session_message(tray.WM_QUERYENDSESSION, 0, stop=lambda: stopped.append(1),
                             wait=None, block=boom, unblock=None)
except RuntimeError:
    pass
check("the stop starts even when the block reason fails", stopped == [1])
if sys.platform != "win32":
    check("off Windows the hook is a quiet no-op",
          tray._install_endsession_hook(lambda: None, threading.Thread()) is None)
ts = (ROOT / "src/tray_app.py").read_text(encoding="utf-8")
check("the tray installs it next to the server thread (stopping for good: the PC "
      "agent's shutdown notice first, then the graceful stop)",
      "_install_endsession_hook(_stop_for_good, server_thread)" in ts
      and ts.index("agent.announce_shutdown()") < ts.index("_initiate_stop()\n", ts.index("def _stop_for_good")))
check("top-level window, not message-only (HWND_MESSAGE misses session broadcasts)",
      "HWND_MESSAGE" not in ts and "CreateWindowExW(0, wc.lpszClassName" in ts)

# ── web-UI shutdown under uvicorn --reload ───────────────────────────────────
print("\n-- web-UI shutdown under --reload stops the reloader too --")
import signal
from src.main import request_dev_server_stop
calls = []
kw = dict(kill=lambda pid, sig: calls.append(("kill", pid, sig)),
          raise_signal=lambda sig: calls.append(("raise", sig)))
check("no reloader: SIGINT to itself", request_dev_server_stop(parent=False, **kw) == "self"
      and calls == [("raise", signal.SIGINT)])
calls.clear()
check("POSIX --reload: SIGINT to the reloader, never to itself",
      request_dev_server_stop(parent=True, platform="linux", ppid=4242, **kw) == "reloader"
      and calls == [("kill", 4242, signal.SIGINT)])
calls.clear()
check("Windows --reload: ONE Ctrl+C to the shared console (a second SIGINT force-exits "
      "and skips the lifespan teardown)",
      request_dev_server_stop(parent=True, platform="win32", **kw) == "console"
      and calls == [("kill", 0, getattr(signal, "CTRL_C_EVENT", 0))])
mn = (ROOT / "src/main.py").read_text(encoding="utf-8")
check("the endpoint uses it when no tray host is registered",
      "path = request_dev_server_stop()" in mn and "signal.raise_signal(signal.SIGINT)" not in mn)

# ── sync_guard: atomic, and only when needed ─────────────────────────────────
print("\n-- .stignore is replaced atomically, never rewritten for nothing --")
import src.services.sync_guard as sg
tmp = Path(tempfile.mkdtemp())
folder = tmp / "Sync"
data = folder / "Curatarr" / "data"
data.mkdir(parents=True)
stignore = folder / ".stignore"
stignore.write_text("/Rockstar Games\n// user note\n", encoding="utf-8")
sg._locate = lambda: (stignore, "/Curatarr/data")
writes = []
real_atomic, real_inplace = sg._write_atomic, sg._write_preserving
sg._write_atomic = lambda p, c: (writes.append("atomic"), real_atomic(p, c))
sg._write_preserving = lambda p, c: (writes.append("inplace"), real_inplace(p, c))

sg.enable()
text = stignore.read_text(encoding="utf-8")
check("block added, user patterns kept",
      "/Rockstar Games" in text and "// user note" in text and "/Curatarr/data/**" in text)
check("written through the atomic path", writes == ["atomic"])
check("no temp file left behind", not list(folder.glob(".syncthing.*")))
check("the temp is named so Syncthing never syncs it (.syncthing.*.tmp)",
      'with_name(".syncthing.stignore-curatarr.tmp")' in (ROOT / "src/services/sync_guard.py").read_text(encoding="utf-8"))
writes.clear()
sg.enable()
check("second boot with the block in place: no write at all", writes == []
      and stignore.read_text(encoding="utf-8") == text)
check("idempotent: one block, never two", text.count("BEGIN Curatarr auto-ignore") == 1)

check.done()
