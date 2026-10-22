"""
Curatarr — system-tray launcher (Windows, no console).

Entry: double-click ``curatarr_tray.pyw`` in the repo root (runs under
pythonw.exe via the .pyw file association) or the "Start with Windows"
shortcut the tray menu can create. start.bat remains the DEV entry
(console + hot reload); this launcher never uses --reload.

Thread layout (deliberate):
  - MAIN thread: pystray ``icon.run()`` — the Win32 notification-area icon
    wants a stable message-pump-owning thread.
  - WORKER thread: uvicorn ``Server.serve()`` on its own asyncio loop.
    uvicorn >= 0.48 skips signal-handler installation automatically when
    not on the main thread; stopping is done via ``server.should_exit``
    (thread-safe plain bool polled by uvicorn's tick), never via signals.

Shutdown paths all converge on ``_initiate_stop()``:
  tray menu Shutdown / Restart, the web UI's /api/system/shutdown via
  ``shutdown_bridge.register``, and a Windows logoff / shutdown via the
  hidden session window (``_install_endsession_hook``, which also holds the
  session open until the teardown is done) — each sets the SSE-release event
  (loop.call_soon_threadsafe) and flips ``server.should_exit``. uvicorn then
  drains connections and runs the FULL lifespan teardown (memory flush,
  scheduler stop, sync-guard release) before serve() returns; the worker's
  ``finally`` stops the tray icon and main exits (or re-execs on Restart).
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from src.paths import ROOT, LOG_DIR

logger = logging.getLogger("curatarr.tray")

_MUTEX_NAME = "Local\\CuratarrTray"
_STARTUP_LNK = (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
                / "Start Menu" / "Programs" / "Startup" / "Curatarr.lnk")
_CREATE_NO_WINDOW = 0x08000000


# ── brand icon, drawn from the exact curation-eye geometry ────────────────────

def _draw_eye(size: int = 64):
    """The favicon's aperture-C eye as a PIL image (transparent background).
    Supersampled then LANCZOS-downscaled so the thin arcs stay crisp at 16px.
    Flat amber #efb02c stands in for the gradient — indistinguishable at tray
    sizes. PIL arc angles: 0 deg = 3 o'clock, clockwise, y-down (same frame
    as the SVG, so the segment angles convert directly)."""
    from PIL import Image, ImageDraw

    S = 64 * 16
    k = S / 64.0
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    amber = (239, 176, 44, 255)     # #efb02c
    inner = (240, 185, 58, 255)     # #f0b93a
    bright = (255, 216, 115, 255)   # #ffd873

    def bbox(r):
        return [(32 - r) * k, (32 - r) * k, (32 + r) * k, (32 + r) * k]

    # outer aperture: three blade segments (gaps at the C-opening + 2 cuts)
    for start, end in ((231, 320), (141, 219), (40, 129)):
        d.arc(bbox(22), start=start, end=end, fill=amber, width=round(9 * k))
    # inner focus arc (270deg C, opening at 3 o'clock) + round end caps
    d.arc(bbox(12), start=45, end=315, fill=inner, width=round(3.6 * k))
    for cx, cy in ((40.5, 23.5), (40.5, 40.5)):
        r = 1.8 * k
        d.ellipse([cx * k - r, cy * k - r, cx * k + r, cy * k + r], fill=inner)
    # pupil + highlight
    for r, col in ((5, amber), (2, bright)):
        rr = r * k
        d.ellipse([32 * k - rr, 32 * k - rr, 32 * k + rr, 32 * k + rr], fill=col)

    from PIL import Image as _I
    return img.resize((size, size), _I.LANCZOS)


# ── single-instance guards ────────────────────────────────────────────────────

def _acquire_mutex():
    """Named mutex; returns handle or None when another tray instance owns it."""
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
        return None
    return handle


def _port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.75):
            return True
    except OSError:
        return False


# ── preflight ─────────────────────────────────────────────────────────────────

def _preflight_deps() -> bool:
    """Bring the interpreter up to requirements.txt BEFORE anything is
    imported: the tray serves in-process, so a half-installed package is a
    crash a minute later, not a warning. A missing or drifted pin runs pip
    for this interpreter (owner's call, 2026-09-07: the tray pulls, like
    start.bat does). The import probe below stays as the last word."""
    try:
        from src.deps_check import check, install
        rep = check()
        if not rep.clean:
            logger.warning("Dependencies off: %s — running pip.", "; ".join(rep.lines()))
            if install():
                logger.info("pip done: %s", check().summary())
            else:
                logger.error("pip failed; continuing with what is installed.")
    except Exception as e:  # noqa: BLE001
        logger.warning("Dependency check skipped: %s", e)
    # The tested install (lock/requirements.txt): raise what fell below it,
    # let the lock follow this interpreter. Never lowers anything.
    try:
        from src.deps_lock import apply as _lock_apply
        _lock_apply(log=logger.info)
    except Exception as e:  # noqa: BLE001
        logger.warning("Lock step skipped: %s", e)
    try:
        import importlib
        for mod in (
            "fastapi", "uvicorn", "sqlalchemy", "chromadb",
            "pydantic_settings", "apscheduler", "psutil",
            "PIL", "pystray", "jwt",
        ):
            importlib.import_module(mod)
        return True
    except Exception as e:
        logger.error("Dependency preflight failed on %s: %s", sys.executable, e)
        ctypes.windll.user32.MessageBoxW(
            None,
            f"Curatarr cannot start — a dependency is missing:\n\n{e}\n\n"
            f"Interpreter: {sys.executable}\n"
            f"Python {sys.version.split()[0]}\n\n"
            "This is usually a multi-Python mixup (the .pyw double-click "
            "uses the py-launcher's NEWEST install). Start the tray via "
            "start_tray.bat instead — it pins the same interpreter as "
            "start.bat — or install the missing package for the "
            "interpreter shown above.",
            "Curatarr", 0x10)
        return False


def _preflight_ollama() -> str | None:
    """Returns a warning string when models are missing (server still starts).
    The list and the probe live in src/services/model_check.py, shared with
    build_models.py --check and, through it, start.bat."""
    from src.services.model_check import missing_models
    missing = missing_models(creationflags=_CREATE_NO_WINDOW)
    if missing is None:
        return ("Ollama is not answering — the models could not be checked; "
                "start Ollama, the app checks again.")
    if missing:
        return ("Ollama models missing: " + ", ".join(missing)
                + " — run start.bat once to build them.")
    return None


# ── Windows logoff / shutdown ─────────────────────────────────────────────────
#
# pystray's window never answers WM_QUERYENDSESSION / WM_ENDSESSION, so a
# Windows logoff, shutdown or update restart simply terminated pythonw: no
# lifespan teardown, no WAL checkpoint, no Chroma pool shutdown — on the
# install that starts with Windows, i.e. every evening. A hidden TOP-LEVEL
# window (a message-only window never receives the session broadcasts) on
# its own thread takes the same graceful path as the tray's Shutdown item and
# holds the session open with a visible reason until the teardown is done.

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
_ENDSESSION_BUDGET_S = 20.0
_ENDSESSION_REASON = "Curatarr is saving its database…"


def _on_session_message(msg: int, wparam: int, *, stop, wait, block, unblock):
    """What the hidden window does with a message; None = not ours (the
    caller hands it to DefWindowProc). Pure, so it is testable off Windows.

    QUERYENDSESSION: register the block reason, start the graceful stop,
    and say yes — refusing would only make Windows nag the user.
    ENDSESSION(wparam=TRUE): the session really ends once we return, so
    wait (bounded) for the teardown first. ENDSESSION(FALSE): it was
    cancelled; the stop already began, the tray winds down as from its menu.
    """
    if msg == WM_QUERYENDSESSION:
        try:
            block()
        finally:
            stop()
        return 1
    if msg == WM_ENDSESSION:
        try:
            if wparam:
                wait()
        finally:
            unblock()
        return 0
    return None


def _install_endsession_hook(initiate_stop, server_thread,
                             budget_s: float = _ENDSESSION_BUDGET_S):
    """Start the hidden session window's thread. Never raises: without it
    the tray still works, it only loses the graceful logoff."""
    try:
        from ctypes import wintypes as W

        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", W.HINSTANCE), ("hIcon", W.HICON),
                        ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH),
                        ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]

        u32.DefWindowProcW.restype = LRESULT
        u32.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        u32.RegisterClassW.restype = W.ATOM
        u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        u32.CreateWindowExW.restype = W.HWND
        u32.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID]
        u32.ShutdownBlockReasonCreate.restype = W.BOOL
        u32.ShutdownBlockReasonCreate.argtypes = [W.HWND, W.LPCWSTR]
        u32.ShutdownBlockReasonDestroy.restype = W.BOOL
        u32.ShutdownBlockReasonDestroy.argtypes = [W.HWND]
        u32.GetMessageW.restype = W.BOOL
        u32.GetMessageW.argtypes = [ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT]
        u32.TranslateMessage.argtypes = [ctypes.POINTER(W.MSG)]
        u32.DispatchMessageW.restype = LRESULT
        u32.DispatchMessageW.argtypes = [ctypes.POINTER(W.MSG)]
        k32.GetModuleHandleW.restype = W.HMODULE
        k32.GetModuleHandleW.argtypes = [W.LPCWSTR]
        k32.SetProcessShutdownParameters.restype = W.BOOL
        k32.SetProcessShutdownParameters.argtypes = [W.DWORD, W.DWORD]
    except Exception as e:  # noqa: BLE001 - not Windows, or an odd user32
        logger.debug("Session hook unavailable: %s", e)
        return None

    def pump():
        def wndproc(hwnd, msg, wparam, lparam):
            try:
                r = _on_session_message(
                    msg, wparam,
                    stop=initiate_stop,
                    wait=lambda: server_thread.join(timeout=budget_s),
                    block=lambda: u32.ShutdownBlockReasonCreate(hwnd, _ENDSESSION_REASON),
                    unblock=lambda: u32.ShutdownBlockReasonDestroy(hwnd))
                if r is not None:
                    if msg == WM_QUERYENDSESSION:
                        logger.info("Windows is ending the session — stopping gracefully.")
                    return r
            except Exception as e:  # noqa: BLE001 - never let an exception cross into Win32
                logger.warning("Session message handling failed: %s", e)
            return u32.DefWindowProcW(hwnd, msg, wparam, lparam)

        callback = WNDPROC(wndproc)          # referenced for the thread's lifetime
        try:
            # Asked early (0x3FF = first of the application range), so the
            # teardown starts before the processes it talks to are gone.
            k32.SetProcessShutdownParameters(0x3FF, 0)
            hinst = k32.GetModuleHandleW(None)
            wc = WNDCLASSW(lpfnWndProc=callback, hInstance=hinst,
                           lpszClassName="CuratarrSessionWatch")
            if not u32.RegisterClassW(ctypes.byref(wc)):
                raise OSError(ctypes.get_last_error(), "RegisterClassW failed")
            hwnd = u32.CreateWindowExW(0, wc.lpszClassName, "Curatarr", 0,
                                       0, 0, 0, 0, None, None, hinst, None)
            if not hwnd:
                raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")
            msg = W.MSG()
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u32.TranslateMessage(ctypes.byref(msg))
                u32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:  # noqa: BLE001
            logger.warning("Session hook failed — a Windows logoff will not stop "
                           "Curatarr gracefully: %s", e)
        finally:
            del callback

    t = threading.Thread(target=pump, name="curatarr-session-watch", daemon=True)
    t.start()
    return t


# ── autostart shortcut ────────────────────────────────────────────────────────

def _pythonw() -> str:
    exe = Path(sys.executable)
    return str(exe if exe.name.lower() == "pythonw.exe"
               else exe.with_name("pythonw.exe"))


def _autostart_enabled(_item=None) -> bool:
    return _STARTUP_LNK.exists()


def _toggle_autostart(icon, _item):
    try:
        if _STARTUP_LNK.exists():
            _STARTUP_LNK.unlink()
            icon.notify("Autostart disabled.", "Curatarr")
            return
        ico = ROOT / "assets" / "curatarr.ico"
        ps = (
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
            "$s.TargetPath='{target}';$s.Arguments='\"{args}\"';"
            "$s.WorkingDirectory='{wd}';{icon_line}$s.Save()"
        ).format(
            lnk=str(_STARTUP_LNK).replace("'", "''"),
            target=_pythonw().replace("'", "''"),
            args=str(ROOT / "curatarr_tray.pyw").replace("'", "''"),
            wd=str(ROOT).replace("'", "''"),
            icon_line=(f"$s.IconLocation='{str(ico).replace(chr(39), chr(39)*2)}';"
                       if ico.exists() else ""),
        )
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                              capture_output=True, timeout=30,
                              creationflags=_CREATE_NO_WINDOW)
        # The exit code was never read: a blocked COM call or policy denial
        # still said "will start with Windows". The shortcut must exist.
        if proc.returncode != 0 or not _STARTUP_LNK.exists():
            err = (proc.stderr or proc.stdout or b"").decode(errors="replace").strip()
            logger.warning("Autostart shortcut not created (PowerShell exit %s): %s",
                           proc.returncode, err[:300])
            icon.notify("Could not enable autostart — see the log.", "Curatarr")
            return
        icon.notify("Curatarr will start with Windows.", "Curatarr")
    except Exception as e:
        logger.warning("Autostart toggle failed: %s", e)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    os.chdir(ROOT)   # belt-and-braces on top of src/paths anchoring

    from src.log_setup import init_logging
    init_logging("INFO")
    logger.info("Tray launcher starting (pid %d).", os.getpid())

    if not _preflight_deps():
        sys.exit(1)

    from src.config import settings

    mutex = _acquire_mutex()
    if mutex is None or _port_in_use(settings.PORT):
        # Another tray instance or a dev console already serves — focus that.
        logger.info("Curatarr already running — opening browser instead.")
        webbrowser.open(f"http://localhost:{settings.PORT}")
        if mutex:
            ctypes.windll.kernel32.CloseHandle(mutex)
        sys.exit(0)

    ollama_warning = _preflight_ollama()

    import pystray
    import uvicorn
    import src.main as app_main
    from src.services import shutdown_bridge
    from src.services.task_monitor import shutdown_event

    config = uvicorn.Config(
        app_main.app, host=settings.HOST, port=settings.PORT,
        log_config=None, access_log=False, timeout_graceful_shutdown=8,
        server_header=False,   # no "server: uvicorn" banner on a LAN port
    )
    server = uvicorn.Server(config)
    server_loop: asyncio.AbstractEventLoop | None = None
    restart_requested = threading.Event()

    def _initiate_stop():
        try:
            if server_loop is not None:
                server_loop.call_soon_threadsafe(shutdown_event.set)
        except Exception:  # noqa: BLE001 - loop may be closed; should_exit still stops it
            pass
        server.should_exit = True

    shutdown_bridge.register(_initiate_stop)

    # The PC agent (src/pc_agent.py): reports this PC's processes and GPU to
    # a Curatarr on the NAS. Only with PC_AGENT_TOKEN set; None otherwise.
    from src import pc_agent
    agent = pc_agent.start_from_settings(settings)

    def _stop_for_good():
        """The PC (or the tray) is going away, not restarting: tell the NAS
        first, then stop."""
        if agent is not None:
            agent.announce_shutdown()
        _initiate_stop()

    icon = pystray.Icon(
        "curatarr", _draw_eye(64), "Curatarr",
        menu=pystray.Menu(
            pystray.MenuItem(
                "Open Curatarr",
                lambda: webbrowser.open(f"http://localhost:{settings.PORT}"),
                default=True),
            pystray.MenuItem("Open logs", lambda: os.startfile(LOG_DIR)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start with Windows", _toggle_autostart,
                             checked=_autostart_enabled),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Restart",
                             lambda: (restart_requested.set(), _initiate_stop())),
            pystray.MenuItem("Shutdown", lambda: _stop_for_good()),
        ),
    )

    def _serve():
        nonlocal server_loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        server_loop = loop
        try:
            loop.run_until_complete(server.serve())
            logger.info("Server stopped cleanly.")
        except Exception as e:
            logger.error("Server crashed: %s", e, exc_info=True)
            try:
                icon.notify("Curatarr failed to start — see data\\logs.", "Curatarr")
            except Exception:  # noqa: BLE001 - toast only; the crash is logged above
                pass
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001 - teardown after the server stopped
                pass
            icon.stop()

    server_thread = threading.Thread(target=_serve, name="curatarr-uvicorn")
    server_thread.start()
    _install_endsession_hook(_stop_for_good, server_thread)

    if ollama_warning:
        # notify shortly after the icon is up (setup runs on icon.run)
        def _late_notify(i):
            i.visible = True
            try:
                i.notify(ollama_warning, "Curatarr")
            except Exception as e:
                # The toast was the only place this warning surfaced.
                logger.warning("%s (tray notification failed: %s)", ollama_warning, e)
        icon.run(setup=_late_notify)
    else:
        icon.run()

    # icon stopped (worker finally / user quit) → wind down
    server.should_exit = True
    server_thread.join(timeout=25)
    if agent is not None:
        agent.stop()
    if mutex:
        ctypes.windll.kernel32.CloseHandle(mutex)   # BEFORE any re-exec

    if restart_requested.is_set():
        logger.info("Restarting tray launcher (re-exec).")
        subprocess.Popen([_pythonw(), str(ROOT / "curatarr_tray.pyw")],
                         cwd=str(ROOT), creationflags=_CREATE_NO_WINDOW)
    logger.info("Tray launcher exiting.")
