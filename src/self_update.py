"""A desktop install updates itself (2026-10-23).

A Windows install is a git checkout of main. "Update" means exactly one
thing: fast-forward it to origin/main, then restart the way the tray always
restarts (its preflight installs what the lock asks for). Nothing is merged,
stashed, reset or overwritten. The checkout is left alone and the reason is
said out loud when it:

- is not on main (a dev branch, a detached HEAD),
- has changes to tracked files,
- has commits of its own that main does not have.

The remote is whatever ``origin`` this checkout already names; no URL, ref
or command ever comes from outside (the NAS's update request carries none).
``check()`` fetches; the tray asks for it on demand, the PC agent every
CHECK_EVERY_S and reports the answer to the NAS.

    python -m src.self_update            check and print
    python -m src.self_update --apply    fast-forward when possible
"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from src.paths import ROOT

logger = logging.getLogger(__name__)

BRANCH = "main"
CHECK_EVERY_S = 3600      # a git fetch: no API quota involved
_TIMEOUT_S = 60


def _no_window() -> dict:
    try:
        from src.winproc import NO_WINDOW
        return dict(NO_WINDOW)
    except ImportError:
        return {}


def git(*args: str, root: Path = ROOT, run: Callable = subprocess.run) -> "tuple[int, str]":
    """(exit code, stdout+stderr) of one git command in the checkout. A
    missing git is (127, ...), a hang (124, ...); never raises."""
    try:
        p = run(["git", *args], cwd=str(root), capture_output=True, text=True,
                timeout=_TIMEOUT_S, **_no_window())
    except FileNotFoundError:
        return 127, "git is not installed or not on PATH"
    except subprocess.TimeoutExpired:
        return 124, f"git {args[0]} did not finish within {_TIMEOUT_S} s"
    except OSError as e:
        return 126, str(e)
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def check(*, fetch: bool = True, root: Path = ROOT, run: Callable = subprocess.run) -> dict:
    """Where this checkout stands against origin/main:
    {"head", "latest", "behind", "ahead", "branch", "dirty", "can_update",
    "reason", "checked_at"}. ``reason`` says why it cannot update (or what
    failed); empty when it can or there is nothing to do."""
    out = {"head": None, "latest": None, "behind": None, "ahead": None, "branch": None,
           "dirty": False, "can_update": False, "reason": "", "checked_at": time.time()}

    def g(*a):
        return git(*a, root=root, run=run)

    rc, head = g("rev-parse", "HEAD")
    if rc != 0:
        out["reason"] = "not a git checkout" if rc != 127 else head
        return out
    out["head"] = head.splitlines()[-1].strip()
    rc, branch = g("rev-parse", "--abbrev-ref", "HEAD")
    out["branch"] = branch.strip() if rc == 0 else None
    rc, porcelain = g("status", "--porcelain", "--untracked-files=no")
    out["dirty"] = rc != 0 or bool(porcelain.strip())
    if fetch:
        rc, msg = g("fetch", "--quiet", "origin", BRANCH)
        if rc != 0:
            out["reason"] = f"could not reach origin: {msg.splitlines()[-1] if msg else rc}"[:200]
            return out
    rc, latest = g("rev-parse", f"origin/{BRANCH}")
    if rc != 0:
        out["reason"] = f"no origin/{BRANCH} in this checkout"
        return out
    out["latest"] = latest.strip()
    rc, counts = g("rev-list", "--left-right", "--count", f"HEAD...origin/{BRANCH}")
    try:
        ahead, behind = (int(x) for x in counts.split())
    except ValueError:
        out["reason"] = "could not compare with origin"
        return out
    out.update(ahead=ahead, behind=behind)
    if out["branch"] != BRANCH:
        out["reason"] = f"on {out['branch'] or 'a detached HEAD'}, not {BRANCH}"
    elif out["dirty"]:
        out["reason"] = "tracked files have local changes"
    elif ahead:
        out["reason"] = f"{ahead} local commit{'s' if ahead != 1 else ''} not on {BRANCH}"
    else:
        out["can_update"] = behind > 0
    return out


def apply(*, root: Path = ROOT, run: Callable = subprocess.run) -> dict:
    """Fast-forward to origin/main when ``check`` allows it.
    {"ok", "updated", "from", "to", "behind", "reason"}: ``ok`` with
    ``updated`` False means it was already current. A restart is the
    caller's job."""
    st = check(root=root, run=run)
    res = {"ok": False, "updated": False, "from": st["head"], "to": st["head"],
           "behind": st["behind"], "reason": st["reason"]}
    if st["reason"]:
        return res
    if not st["behind"]:
        res["ok"] = True
        return res
    rc, msg = git("merge", "--ff-only", "--quiet", f"origin/{BRANCH}", root=root, run=run)
    if rc != 0:
        res["reason"] = f"fast-forward refused: {msg.splitlines()[-1] if msg else rc}"[:200]
        return res
    rc, head = git("rev-parse", "HEAD", root=root, run=run)
    res.update(ok=True, updated=True, to=head.strip() if rc == 0 else st["latest"])
    logger.info("[update] fast-forwarded %s -> %s (%s commits)",
                (res["from"] or "")[:7], (res["to"] or "")[:7], st["behind"])
    return res


class Checker:
    """The last ``check`` for the agent's report, refreshed in the background
    at most every CHECK_EVERY_S (a fetch is a network call; the NAS polls
    every 5 s)."""

    def __init__(self, check_fn: Callable[[], dict] = check, every: float = CHECK_EVERY_S):
        import threading
        self._check = check_fn
        self._every = every
        self._last: Optional[dict] = None
        self._lock = threading.Lock()
        self._running = False

    def _run(self) -> None:
        try:
            res = self._check()
        except Exception as e:  # noqa: BLE001 - a failed check is reported, never raised
            res = {"reason": f"check failed: {e}"[:200], "checked_at": time.time()}
        with self._lock:
            self._last, self._running = res, False

    def refresh(self) -> None:
        import threading
        with self._lock:
            if self._running:
                return
            self._running = True
        threading.Thread(target=self._run, name="curatarr-update-check", daemon=True).start()

    def report(self) -> Optional[dict]:
        """The last answer (None before the first); kicks a refresh when stale."""
        with self._lock:
            last = self._last
        if last is None or time.time() - float(last.get("checked_at") or 0) >= self._every:
            self.refresh()
        if last is None:
            return None
        return {k: last.get(k) for k in ("head", "latest", "behind", "can_update", "reason",
                                         "checked_at")}

    def forget(self) -> None:
        with self._lock:
            self._last = None


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if "--apply" in sys.argv:
        r = apply()
        print(r)
        sys.exit(0 if r["ok"] else 1)
    print(check())
