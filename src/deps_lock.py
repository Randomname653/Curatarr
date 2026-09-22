"""The tested install, written down: lock/requirements.txt.

requirements.txt pins the 17 packages Curatarr imports; everything they
pull in (~90 packages) was unpinned and therefore invisible. OSV-Scanner
2.6.0 made that visible the wrong way round — it resolved the manifest
through deps.dev and reported floor versions no install ever gets — while
the real install ran anyio 4.13.0 inside two advisories nobody saw
(2026-09-21). The lock is the closure of the direct pins as installed on
the machine that runs the battery, so the scanners read what actually runs.

Rules, so the file can never lie or bite:

* The lock FOLLOWS the install. ``apply()`` rewrites it from what is
  installed, and only writes when something changed.
* The lock never lowers anything. A package installed below its lock line
  is raised (that is how a merged security bump reaches a machine); a
  package installed above it raises the lock line instead.
* Direct pins are requirements.txt's business: the lock repeats them, the
  launchers enforce them first (src.deps_check), and the lock may lag a
  fresh pin bump until the next start — never run ahead of it.
* Packages outside this machine's closure (uvloop on Windows, pywin32 on
  Linux) are neither installed nor removed; they are listed as "unused".

    python -m src.deps_lock              report; exit 1 when anything differs
    python -m src.deps_lock --apply      raise what is below the lock, then refresh it
    python -m src.deps_lock --sync-pins  repeat requirements.txt's pins in the lock

Marker evaluation and version ordering come from `packaging`, which is
part of the closure itself; when even that is missing (a venv before its
first pip run) the launchers skip the lock step and say so.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError
from importlib.metadata import distribution as _distribution
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
LOCK = ROOT / "lock" / "requirements.txt"
APPLY_CMD = "python -m src.deps_lock --apply"

_HEADER = """# The tested install: every package the pins in ../requirements.txt pull
# in, at the versions installed on the machine that runs the battery.
# Written by `python -m src.deps_lock --apply` (start.bat and the tray do
# it at every start); edit requirements.txt for a deliberate bump, never
# this file. Scanned by OSV-Scanner and the dependency graph, so transitive
# advisories are visible; reproduce the set with
#     pip install -r requirements.txt -c lock/requirements.txt
"""

_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;#]+)")
_PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[([^\]]*)\])?\s*==\s*([^\s;#]+)")

RequiresFn = Callable[[str], Optional[Tuple[str, str, List[str]]]]


def norm(name: str) -> str:
    """PEP 503: case-insensitive, runs of -_. are one dash."""
    return re.sub(r"[-_.]+", "-", name).lower()


# ── the files ────────────────────────────────────────────────────────────────

def parse_lock(text: str) -> Dict[str, str]:
    """{normalised name: version}; comments, blanks and anything unpinned are skipped."""
    out: Dict[str, str] = {}
    for line in text.splitlines():
        m = _LINE.match(line.split("#", 1)[0])
        if m:
            out[norm(m.group(1))] = m.group(2)
    return out


def lock_spelling(text: str) -> Dict[str, str]:
    """{normalised name: the spelling the file uses}."""
    out: Dict[str, str] = {}
    for line in text.splitlines():
        m = _LINE.match(line.split("#", 1)[0])
        if m:
            out[norm(m.group(1))] = m.group(1)
    return out


def format_lock(entries: Dict[str, str], spelling: Optional[Dict[str, str]] = None) -> str:
    """Sorted `name==version` lines under the header."""
    spelling = spelling or {}
    body = "\n".join(f"{spelling.get(k, k)}=={v}" for k, v in sorted(entries.items()))
    return _HEADER + body + "\n"


def direct_pins(text: str) -> Dict[str, Tuple[str, List[str]]]:
    """{normalised name: (version, [extras])} from requirements.txt."""
    out: Dict[str, Tuple[str, List[str]]] = {}
    for line in text.splitlines():
        m = _PIN.match(line.split("#", 1)[0])
        if m:
            extras = [e.strip() for e in (m.group(3) or "").split(",") if e.strip()]
            out[norm(m.group(1))] = (m.group(4), extras)
    return out


# ── the closure ──────────────────────────────────────────────────────────────

def _packaging():
    """packaging from the environment, else pip's vendored copy, else None."""
    try:
        from packaging.requirements import Requirement
        from packaging.version import Version
        return Requirement, Version
    except ImportError:
        try:
            from pip._vendor.packaging.requirements import Requirement  # type: ignore
            from pip._vendor.packaging.version import Version  # type: ignore
            return Requirement, Version
        except ImportError:
            return None


def _dist_requires(name: str) -> Optional[Tuple[str, str, List[str]]]:
    """(canonical spelling, installed version, requirement strings) or None."""
    try:
        d = _distribution(name)
    except PackageNotFoundError:
        return None
    return (d.metadata["Name"] or name), d.version, list(d.requires or [])


def closure(roots: Dict[str, Tuple[str, List[str]]],
            requires: RequiresFn = _dist_requires,
            packaging=None) -> Tuple[Dict[str, str], Dict[str, str], List[str]]:
    """Walk installed metadata from the roots, honouring extras and
    environment markers for THIS interpreter. Returns ({norm: version},
    {norm: spelling}, [roots that are not installed])."""
    pk = packaging or _packaging()
    if pk is None:
        raise RuntimeError("packaging is not importable; run pip for requirements.txt first")
    Requirement, _Version = pk
    versions: Dict[str, str] = {}
    spelling: Dict[str, str] = {}
    missing: List[str] = []
    seen_extras: Dict[str, set] = {}
    queue: List[Tuple[str, Tuple[str, ...]]] = [(n, tuple(sorted(ex))) for n, (_v, ex) in roots.items()]
    while queue:
        name, extras = queue.pop()
        key = norm(name)
        first = key not in versions
        new_extras = set(extras) - seen_extras.get(key, set())
        if not first and not new_extras:
            continue
        info = requires(name)
        if info is None:
            if first and name not in missing:
                missing.append(name)
            continue
        spelled, ver, reqs = info
        versions[key] = ver
        spelling[key] = spelled
        # base dependencies on the first visit; extras only the first time each is asked for
        wanted = set(new_extras) | ({""} if first else set())
        seen_extras.setdefault(key, set()).update(extras)
        for r in reqs:
            try:
                req = Requirement(r)
            except Exception:  # noqa: BLE001 — a malformed requirement string is not our bug
                continue
            if req.marker is None:
                if "" in wanted:
                    queue.append((req.name, tuple(sorted(req.extras))))
                continue
            if any(req.marker.evaluate({"extra": extra}) for extra in wanted):
                queue.append((req.name, tuple(sorted(req.extras))))
    return versions, spelling, missing


# ── comparison ───────────────────────────────────────────────────────────────

@dataclass
class LockReport:
    lock: Dict[str, str] = field(default_factory=dict)
    installed: Dict[str, str] = field(default_factory=dict)
    spelling: Dict[str, str] = field(default_factory=dict)
    below: List[Tuple[str, str, str]] = field(default_factory=list)   # (name, installed, locked): raise
    above: List[Tuple[str, str, str]] = field(default_factory=list)   # (name, installed, locked): lock follows
    unlocked: List[str] = field(default_factory=list)                 # in the closure, not in the lock
    unused: List[str] = field(default_factory=list)                   # in the lock, not in this closure
    missing_roots: List[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def clean(self) -> bool:
        return not (self.below or self.above or self.unlocked or self.error)

    def lines(self) -> List[str]:
        out = [f"{n}: installed {i}, lock says {l} (will be raised)" for n, i, l in self.below]
        out += [f"{n}: installed {i}, lock says {l} (lock follows)" for n, i, l in self.above]
        out += [f"{n}: installed, not in the lock" for n in self.unlocked]
        return out

    def summary(self) -> str:
        if self.error:
            return f"lock not checked: {self.error}"
        if self.clean:
            return f"lock matches this interpreter ({len(self.lock)} packages)"
        shown = self.lines()
        return (f"lock differs: {len(self.below)} below, {len(self.above)} above, "
                f"{len(self.unlocked)} unlocked: " + "; ".join(shown[:8])
                + (" …" if len(shown) > 8 else "") + f". Run: {APPLY_CMD}")

    def as_dict(self) -> dict:
        return {
            "clean": self.clean,
            "packages": len(self.lock),
            "below": [{"name": n, "installed": i, "locked": l} for n, i, l in self.below],
            "above": [{"name": n, "installed": i, "locked": l} for n, i, l in self.above],
            "unlocked": list(self.unlocked),
            "unused": list(self.unused),
            "error": self.error,
            "command": APPLY_CMD,
        }


def compare(lock_text: str, installed: Dict[str, str], packaging=None) -> LockReport:
    pk = packaging or _packaging()
    Version = pk[1] if pk else None
    rep = LockReport(lock=parse_lock(lock_text), installed=dict(installed))

    def lower(a: str, b: str) -> bool:
        if Version is None:
            return a != b
        try:
            return Version(a) < Version(b)
        except Exception:  # noqa: BLE001
            return a != b

    for name, ver in sorted(installed.items()):
        locked = rep.lock.get(name)
        if locked is None:
            rep.unlocked.append(name)
        elif lower(ver, locked):
            rep.below.append((name, ver, locked))
        elif lower(locked, ver):
            rep.above.append((name, ver, locked))
    rep.unused = sorted(set(rep.lock) - set(installed))
    return rep


def check(requirements: Path = REQUIREMENTS, lock: Path = LOCK,
          requires: RequiresFn = _dist_requires) -> LockReport:
    """The report the server logs and Settings → Maintenance shows."""
    try:
        roots = direct_pins(Path(requirements).read_text(encoding="utf-8"))
        versions, spelling, missing_roots = closure(roots, requires)
    except Exception as e:  # noqa: BLE001
        return LockReport(error=f"{type(e).__name__}: {e}")
    lock_path = Path(lock)
    lock_text = lock_path.read_text(encoding="utf-8") if lock_path.exists() else ""
    rep = compare(lock_text, versions)
    rep.spelling = spelling
    rep.missing_roots = missing_roots
    if not lock_text:
        rep.error = f"{lock_path.name} is missing"
    return rep


# ── writing and raising ──────────────────────────────────────────────────────

def merged(rep: LockReport, pins: Dict[str, Tuple[str, List[str]]]) -> Dict[str, str]:
    """What the lock should say: this closure, each package at the higher of
    installed and locked — except direct pins, which repeat requirements.txt."""
    keep_lock = {n: locked for n, _installed, locked in rep.below}
    out = {name: keep_lock.get(name, ver) for name, ver in rep.installed.items()}
    for name, (ver, _extras) in pins.items():
        if name in out:
            out[name] = ver
    return out


def raise_below(rep: LockReport, run=subprocess.run) -> List[str]:
    """pip install name==locked for every package below its lock line.
    One call per package so one failure does not take the others down;
    returns the names that failed."""
    failed: List[str] = []
    for name, _installed, locked in rep.below:
        r = run([sys.executable, "-m", "pip", "install", f"{rep.spelling.get(name, name)}=={locked}",
                 "--quiet", "--disable-pip-version-check"], capture_output=True, text=True)
        if r.returncode != 0:
            failed.append(name)
            sys.stderr.write((r.stderr or r.stdout or "")[-600:])
    if rep.below and len(failed) < len(rep.below):
        import importlib
        importlib.invalidate_caches()
    return failed


def write_lock(entries: Dict[str, str], spelling: Dict[str, str], lock: Path = LOCK) -> bool:
    """Write only when the content changes; returns True when it did."""
    text = format_lock(entries, spelling)
    lock = Path(lock)
    if lock.exists() and lock.read_text(encoding="utf-8") == text:
        return False
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(text, encoding="utf-8", newline="\n")
    return True


def apply(requirements: Path = REQUIREMENTS, lock: Path = LOCK,
          requires: RequiresFn = _dist_requires, run=subprocess.run, log=print) -> LockReport:
    """The launchers' step: raise what is below, then let the lock follow."""
    rep = check(requirements, lock, requires)
    if rep.error and not rep.installed:          # nothing walkable: no packaging, no closure
        log(f"[lock] {rep.summary()}")
        return rep
    if rep.below:
        log(f"[lock] raising {len(rep.below)} package(s) to the lock: "
            + ", ".join(f"{n} {i}->{l}" for n, i, l in rep.below))
        failed = raise_below(rep, run)
        if failed:
            log(f"[lock] pip failed for: {', '.join(failed)} (kept in the lock, retried next start)")
        rep = check(requirements, lock, requires)
    pins = direct_pins(Path(requirements).read_text(encoding="utf-8"))
    entries = merged(rep, pins)
    spelling = dict(lock_spelling(Path(lock).read_text(encoding="utf-8")) if Path(lock).exists() else {})
    spelling.update(rep.spelling)
    if write_lock(entries, spelling, lock):
        log(f"[lock] refreshed {Path(lock).name}: {len(entries)} packages")
    rep = check(requirements, lock, requires)
    log(f"[lock] {rep.summary()}")
    return rep


def sync_pins(requirements: Path = REQUIREMENTS, lock: Path = LOCK) -> int:
    """Repeat requirements.txt's pins in the lock without touching the rest
    (after a Dependabot merge, before anyone restarts). Returns the number
    of lines changed."""
    text = Path(lock).read_text(encoding="utf-8")
    entries, spelling = parse_lock(text), lock_spelling(text)
    pins = direct_pins(Path(requirements).read_text(encoding="utf-8"))
    changed = 0
    for name, (ver, _extras) in pins.items():
        if entries.get(name) not in (None, ver):
            entries[name] = ver
            changed += 1
    if changed:
        write_lock(entries, spelling, lock)
    return changed


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="the tested install vs. this interpreter")
    ap.add_argument("--apply", action="store_true", help="raise packages below the lock, then refresh it")
    ap.add_argument("--sync-pins", action="store_true", help="repeat requirements.txt's pins in the lock")
    ap.add_argument("--requirements", default=str(REQUIREMENTS))
    ap.add_argument("--lock", default=str(LOCK))
    args = ap.parse_args(argv)
    req, lock = Path(args.requirements), Path(args.lock)
    if args.sync_pins:
        n = sync_pins(req, lock)
        print(f"[lock] {n} pin line(s) updated from {req.name}")
        return 0
    if args.apply:
        return 0 if apply(req, lock).clean else 1
    rep = check(req, lock)
    print(f"[lock] {rep.summary()}")
    return 0 if rep.clean else 1


if __name__ == "__main__":
    sys.exit(main())
