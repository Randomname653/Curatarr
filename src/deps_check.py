"""Pinned requirements vs. the running interpreter: checked, reported, and
(for the launchers) pulled up to date.

Why a module and not the batch file's sentinel import: an import proves
presence, not version. Since Dependabot bumps the pins weekly, the common
case is "installed 2.13.4, pinned 2.13.5", which every import passes. The
launchers (start.bat, the tray) call this before anything else is imported
and run pip when something is off; the server only reports. pip inside the
process being served is the wrong layer: half-loaded modules, a restart is
needed anyway, and a server that runs pip with network access at boot is a
supply-chain surface a reviewer rightly flags.

    python -m src.deps_check            report; exit 1 when anything is off
    python -m src.deps_check --install  run pip for the pinned file first

Stdlib only: this runs before the dependencies exist.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from pathlib import Path
from typing import Callable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
INSTALL_CMD = "pip install -r requirements.txt"

# name, optional [extras], == version; anything else in the file (comments,
# blank lines, ranges) is not a pin and is ignored.
_PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*==\s*([^\s;#]+)")


@dataclass
class Pin:
    name: str                       # distribution name as written (PyJWT, uvicorn)
    version: str                    # pinned
    installed: Optional[str] = None  # None = not installed

    @property
    def ok(self) -> bool:
        return self.installed is not None and _norm(self.installed) == _norm(self.version)


@dataclass
class Report:
    pins: List[Pin] = field(default_factory=list)
    missing: List[Pin] = field(default_factory=list)
    drift: List[Pin] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.missing and not self.drift

    def lines(self) -> List[str]:
        out = [f"{p.name}: not installed (pinned {p.version})" for p in self.missing]
        out += [f"{p.name}: installed {p.installed}, pinned {p.version}" for p in self.drift]
        return out

    def summary(self) -> str:
        if self.clean:
            return f"dependencies match requirements.txt ({len(self.pins)} pins)"
        return (f"{len(self.missing)} missing, {len(self.drift)} differ from requirements.txt: "
                + "; ".join(self.lines()) + f". Run: {INSTALL_CMD}")

    def as_dict(self) -> dict:
        return {
            "clean": self.clean,
            "pins": len(self.pins),
            "missing": [{"name": p.name, "pinned": p.version} for p in self.missing],
            "drift": [{"name": p.name, "pinned": p.version, "installed": p.installed} for p in self.drift],
            "command": INSTALL_CMD,
            "interpreter": sys.executable,
        }


def _norm(v: str) -> tuple:
    """1.0 == 1.0.0; case and a leading v are noise. Pre-release tags stay."""
    parts = v.strip().lower().lstrip("v").split(".")
    while len(parts) > 1 and parts[-1] == "0":
        parts.pop()
    return tuple(parts)


def parse_pins(text: str) -> List[Pin]:
    pins = []
    for line in text.splitlines():
        m = _PIN.match(line.split("#", 1)[0])
        if m:
            pins.append(Pin(name=m.group(1), version=m.group(3)))
    return pins


def _installed(name: str) -> Optional[str]:
    try:
        return _dist_version(name)
    except PackageNotFoundError:
        return None


def check(requirements: Path = REQUIREMENTS,
          installed: Callable[[str], Optional[str]] = _installed) -> Report:
    rep = Report(pins=parse_pins(Path(requirements).read_text(encoding="utf-8")))
    for p in rep.pins:
        p.installed = installed(p.name)
        if p.installed is None:
            rep.missing.append(p)
        elif not p.ok:
            rep.drift.append(p)
    return rep


def install(requirements: Path = REQUIREMENTS, run=subprocess.run) -> bool:
    """pip for THIS interpreter, the one that will import the packages. Output
    is captured (the tray has no console) and the tail is returned via the
    log on failure; the caller re-checks afterwards."""
    r = run([sys.executable, "-m", "pip", "install", "-r", str(requirements),
             "--quiet", "--disable-pip-version-check"],
            capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write((r.stderr or r.stdout or "")[-1200:])
        return False
    import importlib
    importlib.invalidate_caches()   # a dist installed a moment ago must be importable now
    return True


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="pinned requirements vs. this interpreter")
    ap.add_argument("--install", action="store_true", help="run pip for the pinned file when anything is off")
    ap.add_argument("--requirements", default=str(REQUIREMENTS))
    args = ap.parse_args(argv)
    req = Path(args.requirements)
    rep = check(req)
    if not rep.clean and args.install:
        print(f"[deps] {rep.summary()}")
        print("[deps] installing ...")
        if install(req):
            rep = check(req)
        else:
            print("[deps] pip failed; continuing with what is installed.")
    print(f"[deps] {rep.summary()}")
    return 0 if rep.clean else 1


if __name__ == "__main__":
    sys.exit(main())
