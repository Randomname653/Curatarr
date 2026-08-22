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
  tray menu Shutdown / Restart, and the web UI's /api/system/shutdown via
  ``shutdown_bridge.register`` — each sets the SSE-release event
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
    try:
        import importlib
        for mod in (
            "fastapi", "uvicorn", "sqlalchemy", "chromadb",
            "pydantic_settings", "apscheduler", "psutil",
            "PIL", "pystray",
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
    """Returns a warning string when models are missing (server still starts)."""
    missing = []
    from src.config import settings
    # Check the model the runtime ACTUALLY uses (stored profile, v2-moe) —
    # settings.EMBEDDING_MODEL is the legacy v1 default and green-lit the
    # wrong model after the migration (external eval catch).
    try:
        from src.services.embed_service import effective_embedding_model
        _emb = effective_embedding_model()
    except Exception:
        _emb = settings.EMBEDDING_MODEL
    checked = [settings.CURATOR_MODEL, _emb]
    # Two-bake split: warn-only nudge when enabled but not built — the app
    # runs fine without it (deletion runs fall back to the curator bake).
    if (settings.PITCHER_MODEL or "").strip():
        checked.append(settings.PITCHER_MODEL.strip())
    for model in checked:
        try:
            r = subprocess.run(["ollama", "show", model], capture_output=True,
                               timeout=20, creationflags=_CREATE_NO_WINDOW)
            if r.returncode != 0:
                missing.append(model)
        except Exception:
            missing.append(model)
    if missing:
        return ("Ollama models missing: " + ", ".join(missing)
                + " — run start.bat once to build them.")
    return None


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
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=30,
                       creationflags=_CREATE_NO_WINDOW)
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
    )
    server = uvicorn.Server(config)
    server_loop: asyncio.AbstractEventLoop | None = None
    restart_requested = threading.Event()

    def _initiate_stop():
        try:
            if server_loop is not None:
                server_loop.call_soon_threadsafe(shutdown_event.set)
        except Exception:
            pass
        server.should_exit = True

    shutdown_bridge.register(_initiate_stop)

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
            pystray.MenuItem("Shutdown", lambda: _initiate_stop()),
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
            except Exception:
                pass
        finally:
            try:
                loop.close()
            except Exception:
                pass
            icon.stop()

    server_thread = threading.Thread(target=_serve, name="curatarr-uvicorn")
    server_thread.start()

    if ollama_warning:
        # notify shortly after the icon is up (setup runs on icon.run)
        def _late_notify(i):
            i.visible = True
            try:
                i.notify(ollama_warning, "Curatarr")
            except Exception:
                pass
        icon.run(setup=_late_notify)
    else:
        icon.run()

    # icon stopped (worker finally / user quit) → wind down
    server.should_exit = True
    server_thread.join(timeout=25)
    if mutex:
        ctypes.windll.kernel32.CloseHandle(mutex)   # BEFORE any re-exec

    if restart_requested.is_set():
        logger.info("Restarting tray launcher (re-exec).")
        subprocess.Popen([_pythonw(), str(ROOT / "curatarr_tray.pyw")],
                         cwd=str(ROOT), creationflags=_CREATE_NO_WINDOW)
    logger.info("Tray launcher exiting.")
