"""
Curatarr — single source of truth for filesystem anchoring.

The app used to be strongly CWD-dependent (Path("data/..."), env_file=".env",
Path("frontend")) — it only worked when launched from the repo root. The tray
launcher, autostart shortcuts and any future frozen (PyInstaller) build need
paths that hold regardless of the process working directory:

- Source checkout: ROOT = the repo root (parent of src/).
- Frozen build (stage 2): ROOT = the directory next to the executable, so
  data/ and .env stay EXTERNAL user data; bundled read-only assets
  (frontend/) resolve into the bundle via ``frontend_root()``.

Lives directly in src/ (not src/services/) so config.py can import it
without creating a cycle — services import config, never the other way.
"""
from __future__ import annotations

import sys
from pathlib import Path


def app_root() -> Path:
    if getattr(sys, "frozen", False):                  # PyInstaller build
        return Path(sys.executable).resolve().parent   # data/.env live next to the exe
    return Path(__file__).resolve().parents[1]         # repo root (parent of src/)


ROOT: Path = app_root()
DATA_DIR: Path = ROOT / "data"
LOG_DIR: Path = DATA_DIR / "logs"
ENV_FILE: Path = ROOT / ".env"


def frontend_root() -> Path:
    """frontend/ is source in the repo, a bundled read-only asset when frozen."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", ROOT)) / "frontend"
    return ROOT / "frontend"
