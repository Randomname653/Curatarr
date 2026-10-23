"""Which build of Curatarr this is (2026-10-23).

The container image carries the commit it was built from (the docker
workflow passes ``CURATARR_BUILD_SHA`` / ``CURATARR_BUILD_DATE``). A desktop
install is a git checkout: its build is the commit HEAD points at, read from
``.git`` directly (no git binary, no subprocess). The sidebar shows it, and
services/update_check compares it with what upstream has published.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from src.paths import ROOT

_cache: dict = {}


def _git_dir(root: Path) -> Optional[Path]:
    """``.git`` as a directory, or the one a worktree's ``.git`` file names."""
    dot = root / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():
        line = dot.read_text(encoding="utf-8", errors="replace").strip()
        if line.startswith("gitdir:"):
            p = Path(line[len("gitdir:"):].strip())
            return p if p.is_absolute() else (root / p).resolve()
    return None


def git_head(root: Path = ROOT) -> Optional[str]:
    """The commit HEAD points at (40 hex), or None."""
    gd = _git_dir(root)
    if gd is None:
        return None
    try:
        head = (gd / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head.startswith("ref:"):
        return head if len(head) == 40 else None
    ref = head[len("ref:"):].strip()
    # A worktree keeps HEAD in its own dir but the refs in the common one.
    common = gd
    try:
        cd = (gd / "commondir").read_text(encoding="utf-8").strip()
        common = (gd / cd).resolve()
    except OSError:
        pass
    for base in (gd, common):
        try:
            sha = (base / ref).read_text(encoding="utf-8").strip()
            if len(sha) == 40:
                return sha
        except OSError:
            continue
    try:
        for line in (common / "packed-refs").read_text(encoding="utf-8").splitlines():
            parts = line.strip().split(" ")
            if len(parts) == 2 and parts[1] == ref and len(parts[0]) == 40:
                return parts[0]
    except OSError:
        pass
    return None


def build(environ=os.environ, root: Path = ROOT) -> dict:
    """{version, sha, short, channel ("container" | "git" | "unknown"), built}."""
    from src.config import settings
    sha = (environ.get("CURATARR_BUILD_SHA") or "").strip()
    channel = "container" if sha else None
    if not sha:
        sha = git_head(root) or ""
        channel = "git" if sha else "unknown"
    return {"version": settings.VERSION, "sha": sha or None, "short": sha[:7] or None,
            "channel": channel, "built": (environ.get("CURATARR_BUILD_DATE") or "").strip() or None}


def current() -> dict:
    """build(), read once per process: the build does not change while running."""
    if "value" not in _cache:
        _cache["value"] = build()
    return _cache["value"]
