"""Curatarr tray launcher — double-click to run without a console window.

The .pyw extension makes Windows run this under pythonw.exe (no console).
start.bat remains the dev entry (console + hot reload). This file is also
the autostart-shortcut target and the future PyInstaller entry script.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.tray_app import main  # noqa: E402

if __name__ == "__main__":
    main()
