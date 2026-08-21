"""Render assets/curatarr.ico (+ a 64px PNG preview) from the tray-app's
curation-eye drawing. Run once from the repo root:

    python scripts/make_icon.py

The .ico is used by the autostart shortcut now and the PyInstaller build
later; the running tray icon draws itself in memory and needs no file.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tray_app import _draw_eye  # noqa: E402

ASSETS = Path(__file__).resolve().parents[1] / "assets"
ASSETS.mkdir(exist_ok=True)

master = _draw_eye(256)
master.save(ASSETS / "curatarr.ico",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
_draw_eye(64).save(ASSETS / "curatarr_64.png")
master.save(ASSETS / "curatarr_256.png")   # README banner
print(f"wrote {ASSETS / 'curatarr.ico'} + curatarr_64.png + curatarr_256.png")
