#!/usr/bin/env python3
"""Give the Curatarr custom app on TrueNAS its icon, description, links,
screenshots, a "Web UI" button and notes.

A custom app (Apps -> Custom App -> Install via YAML) shows TrueNAS's generic
icon and an empty description: the compose file has no place for them. The
app's metadata file does. This script fills it in and leaves every field it
does not know alone, so TrueNAS's own bookkeeping (version, train, the
"custom-app" name) stays as it was.

On the NAS, as root (the files belong to root):

    sudo python3 apply_metadata.py                 # app "curatarr", port 30800
    sudo python3 apply_metadata.py myapp --port 30900
    sudo python3 apply_metadata.py --dry-run       # print the result, write nothing

It writes /mnt/.ix-apps/app_configs/<app>/metadata.yaml (a .bak copy of the
old file next to it) and the app's entry in /mnt/.ix-apps/metadata.yaml,
which some TrueNAS versions read instead. TrueNAS picks the change up when
its app service next refreshes (an app saved or updated; a hard browser
reload, Ctrl+Shift+R, shows it). It may rewrite the files when the app is
edited or updated: run the script again then. It changes nothing else and
is safe to repeat.

Needs only Python 3 and PyYAML, both part of TrueNAS SCALE.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - TrueNAS ships it
    sys.exit("PyYAML is missing: run this on the TrueNAS host, where it is installed.")

REPO = "https://github.com/Randomname653/Curatarr"
RAW = "https://raw.githubusercontent.com/Randomname653/Curatarr/main"
IX_APPS = Path("/mnt/.ix-apps")

METADATA = {
    # TrueNAS shows "Custom App" here once it has rewritten the file.
    "title": "Curatarr",
    "icon": f"{RAW}/frontend/icons/icon-transparent-512.png",
    "description": ("A self-hosted AI curator for Plex and the *arr stack. It learns what the "
                    "household actually watches, recommends what is worth adding and makes a "
                    "reasoned case for what to delete, with every prompt running on your own "
                    "hardware (Ollama)."),
    "home": REPO,
    "sources": [REPO, "https://github.com/Randomname653/Curatarr/pkgs/container/curatarr"],
    "screenshots": [f"{RAW}/docs/screenshots/desktop/{n}.png"
                    for n in ("chat", "recommendations", "deletions", "wrapup", "knowledge-base")],
    "keywords": ["plex", "media", "curation", "recommendations", "ollama", "ai", "arr"],
    "categories": ["media"],
    "maintainers": [{"name": "Randomname653", "url": "https://github.com/Randomname653"}],
}
NOTES = ("Curatarr, a self-hosted AI curator for Plex.\n\n"
         "- First start: open the Web UI and follow the setup wizard.\n"
         "- Ollama runs elsewhere: point the wizard at the machine that runs it.\n"
         f"- Updates: {REPO}/blob/main/docs/NAS_MOVE.md (Updating later).\n"
         f"- Docs and issues: {REPO}\n")


def patched(doc: dict, port: int) -> dict:
    """``doc`` with Curatarr's fields filled in; every other field kept."""
    out = dict(doc or {})
    meta = dict(out.get("metadata") or {})
    meta.update(METADATA)
    out["metadata"] = meta
    # TrueNAS puts the NAS's own address in place of 0.0.0.0 on the button.
    out["portals"] = {**(out.get("portals") or {}), "Web UI": f"http://0.0.0.0:{port}/"}
    out["notes"] = NOTES
    return out


def _dump(doc: dict) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)


def _write(path: Path, text: str) -> None:
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path} (old file: {backup.name})")


def main(argv=None, root: Path = IX_APPS) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("app", nargs="?", default="curatarr", help="the app's name in TrueNAS (default: curatarr)")
    p.add_argument("--port", type=int, default=30800, help="the host port Curatarr answers on (default: 30800)")
    p.add_argument("--dry-run", action="store_true", help="print the new metadata, write nothing")
    a = p.parse_args(argv)

    app_file = root / "app_configs" / a.app / "metadata.yaml"
    if not app_file.is_file():
        print(f"{app_file} not found: is the app called '{a.app}'? (Apps page, the name in its title)",
              file=sys.stderr)
        return 1
    new = patched(yaml.safe_load(app_file.read_text(encoding="utf-8")), a.port)
    if a.dry_run:
        print(_dump(new))
        return 0
    _write(app_file, _dump(new))

    # Some versions (25.04 among them) show what the global file says.
    glob = root / "metadata.yaml"
    if glob.is_file():
        all_apps = yaml.safe_load(glob.read_text(encoding="utf-8")) or {}
        if isinstance(all_apps, dict) and isinstance(all_apps.get(a.app), dict):
            all_apps[a.app] = patched(all_apps[a.app], a.port)
            _write(glob, _dump(all_apps))
    print("Done. Reload the Apps page with Ctrl+Shift+R. Nothing yet: save any app once (Edit, Save),\n"
          "reload again, and if the generic icon came back with that save, run this script once more.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
