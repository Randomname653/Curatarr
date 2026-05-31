"""
Curatarr - Syncthing guard.

While Curatarr is running, the live ``data/`` directory (the WAL-mode
SQLite database + its ``-wal`` / ``-shm`` sidecars, the ChromaDB store,
and the enrichment cache — all live SQLite files) must NOT be touched by
Syncthing. An external process hashing/copying an open SQLite database
causes ``sqlite3.OperationalError: database is locked`` and, far worse,
risks *corrupting* the database: Syncthing never copies ``.db`` / ``-wal``
/ ``-shm`` as one consistent snapshot, so a partially-synced WAL can
destroy the file.

This module adds an ignore entry for the data directory to the enclosing
Syncthing folder's ``.stignore`` at startup, and removes it again on clean
shutdown — so the database can still sync between machines while Curatarr
is *stopped*, but never while it's running.

Design notes:
  * The correct ``.stignore`` lives at the *root* of the Syncthing folder
    that contains our data dir, not necessarily the project root. We find
    it by parsing Syncthing's ``config.xml`` and matching the folder whose
    ``path`` is an ancestor of the data dir.
  * Fully best-effort: if Syncthing isn't installed, or the data dir isn't
    inside any synced folder, every function is a silent no-op.
  * Only the block between our BEGIN/END markers is ever touched, so any
    existing user patterns (e.g. ``/Rockstar Games``) are preserved.
  * Crash-safe direction: if the process dies without running the shutdown
    hook, the ignore entry simply stays — the DB stays *excluded* from
    sync (the safe failure mode). The next clean shutdown removes it.
"""

from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_BEGIN = "// BEGIN Curatarr auto-ignore (managed while running - do not edit by hand)"
_END = "// END Curatarr auto-ignore"

# Remembered between enable()/disable() so shutdown reverts the exact file.
_active_stignore: Optional[Path] = None


def _candidate_config_paths() -> list[Path]:
    """Likely locations of Syncthing's ``config.xml`` across platforms."""
    paths: list[Path] = []
    for env in ("LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(env)
        if base:
            paths.append(Path(base) / "Syncthing" / "config.xml")
    home = Path.home()
    paths += [
        home / ".config" / "syncthing" / "config.xml",                          # Linux (XDG)
        home / ".local" / "state" / "syncthing" / "config.xml",                 # Linux (newer)
        home / "Library" / "Application Support" / "Syncthing" / "config.xml",  # macOS
    ]
    return paths


def _data_dir() -> Path:
    """Absolute path of the directory holding the live SQLite DB."""
    from src.config import settings

    url = settings.DATABASE_URL
    prefix = "sqlite:///"
    raw = url[len(prefix):] if url.startswith(prefix) else url
    p = Path(raw)
    if not p.is_absolute():
        p = Path.cwd() / p
    return p.resolve().parent


def _find_synced_folder_root(target: Path) -> Optional[Path]:
    """Return the Syncthing folder root that contains ``target``, else None."""
    target = target.resolve()
    for cfg in _candidate_config_paths():
        if not cfg.is_file():
            continue
        try:
            root = ET.parse(cfg).getroot()
        except Exception as e:
            logger.debug("[sync-guard] could not parse %s: %s", cfg, e)
            continue
        for folder in root.iter("folder"):
            raw_path = folder.get("path")
            if not raw_path:
                continue
            try:
                froot = Path(raw_path).resolve()
                target.relative_to(froot)
            except (ValueError, OSError):
                continue
            return froot
    return None


def _locate() -> Optional[Tuple[Path, str]]:
    """Resolve (``.stignore`` path, Syncthing ignore pattern) for the data dir."""
    data_dir = _data_dir()
    froot = _find_synced_folder_root(data_dir)
    if froot is None:
        return None
    rel = data_dir.resolve().relative_to(froot).as_posix()
    return froot / ".stignore", "/" + rel


def _strip_block(lines: list[str]) -> list[str]:
    """Drop any existing managed BEGIN..END block (inclusive)."""
    out: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped == _BEGIN:
            skipping = True
            continue
        if skipping:
            if stripped == _END:
                skipping = False
            continue
        out.append(line)
    return out


def enable() -> None:
    """Exclude the data dir from Syncthing while the app is running."""
    global _active_stignore
    try:
        located = _locate()
        if located is None:
            logger.debug("[sync-guard] data dir is not inside a Syncthing folder — nothing to do")
            return
        stignore, pattern = located

        lines: list[str] = []
        if stignore.exists():
            lines = stignore.read_text(encoding="utf-8").splitlines()
        lines = _strip_block(lines)  # idempotent: remove any stale block first

        block = [_BEGIN, pattern, _END]
        if lines and lines[-1].strip():
            lines.append("")  # blank separator from user's own patterns
        lines.extend(block)

        stignore.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _active_stignore = stignore
        logger.info("[sync-guard] Excluded '%s' from Syncthing while running (%s)", pattern, stignore)
    except Exception as e:
        logger.warning("[sync-guard] could not update .stignore (continuing): %s", e)


def disable() -> None:
    """Restore Syncthing sync for the data dir (clean shutdown)."""
    global _active_stignore
    try:
        stignore = _active_stignore
        if stignore is None:
            located = _locate()
            stignore = located[0] if located else None
        if stignore is None or not stignore.exists():
            return

        lines = stignore.read_text(encoding="utf-8").splitlines()
        lines = _strip_block(lines)
        while lines and not lines[-1].strip():
            lines.pop()

        stignore.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
        logger.info("[sync-guard] Restored Syncthing sync for the data dir (%s)", stignore)
    except Exception as e:
        logger.warning("[sync-guard] could not restore .stignore (continuing): %s", e)
    finally:
        _active_stignore = None
