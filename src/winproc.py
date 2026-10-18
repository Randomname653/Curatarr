"""Curatarr — keyword arguments for a child process that must not open a window.

The tray serves under pythonw.exe, which has no console. Windows gives every
CONSOLE program started from such a process a console window of its own,
so ``nvidia-smi`` — asked by the GPU-pressure gate every ~30 s — flashed a
window and could steal focus from the fullscreen game the gate exists to
protect. ``subprocess.run([...], **NO_WINDOW)`` keeps it hidden; elsewhere
the dict is empty and changes nothing.

Stdlib only: deps_check imports it before the dependencies are installed.
"""
import subprocess
import sys

NO_WINDOW: dict = ({"creationflags": subprocess.CREATE_NO_WINDOW}
                   if sys.platform == "win32" else {})
