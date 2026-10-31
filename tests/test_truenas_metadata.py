"""deploy/truenas/apply_metadata.py gives the custom app its icon,
description, links, screenshots, Web UI button and notes (2026-10-29).

Runs against a copy of a real custom app's metadata.yaml in a temp folder;
nothing on a NAS is touched.

    python tests/test_truenas_metadata.py
"""
import importlib.util
import io
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("apply_metadata", ROOT / "deploy/truenas/apply_metadata.py")
am = importlib.util.module_from_spec(spec)
spec.loader.exec_module(am)

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# The owner's file as TrueNAS wrote it after "Install via YAML".
REAL = '''"action_required": false
"custom_app": true
"human_version": "1.0.0_custom"
"metadata":
  "app_version": "custom"
  "capabilities": []
  "description": "This is a custom app where user can use his/her own docker compose\\
    \\ file for deploying services"
  "home": ""
  "host_mounts": []
  "maintainers": []
  "name": "custom-app"
  "run_as_context": []
  "sources": []
  "title": "Curatarr"
  "train": "stable"
  "version": "1.0.0"
"migrated": false
"notes": null
"portals": {}
"version": "1.0.0"
'''


def run(root, *argv):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = am.main(list(argv), root=root)
    return rc, out.getvalue()


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    app_dir = root / "app_configs" / "curatarr"
    app_dir.mkdir(parents=True)
    f = app_dir / "metadata.yaml"
    f.write_text(REAL, encoding="utf-8")
    (root / "metadata.yaml").write_text(yaml.safe_dump({"curatarr": yaml.safe_load(REAL),
                                                        "other": {"custom_app": False}}), encoding="utf-8")

    rc, out = run(root, "--dry-run")
    check("a dry run prints the new metadata and writes nothing",
          rc == 0 and "icon:" in out and f.read_text(encoding="utf-8") == REAL)

    rc, out = run(root)
    new = yaml.safe_load(f.read_text(encoding="utf-8"))
    m = new["metadata"]
    check("the icon, description, home and sources are set",
          m["icon"].endswith("/frontend/icons/icon-transparent-512.png") and "Plex" in m["description"]
          and m["home"] == am.REPO and am.REPO in m["sources"])
    check("...the screenshots point at files that exist in the repo",
          len(m["screenshots"]) == 5 and all(
              (ROOT / s.split("/main/", 1)[1]).is_file() for s in m["screenshots"]))
    check("...the icon too", (ROOT / m["icon"].split("/main/", 1)[1]).is_file())
    from PIL import Image
    ic = Image.open(ROOT / m["icon"].split("/main/", 1)[1]).convert("RGBA")
    check("...cut out like the other apps' icons (transparent corners), with the ruby diamond",
          ic.getpixel((3, 3))[3] == 0 and ic.getpixel((ic.width // 2, ic.height // 2))[:3] == (230, 44, 51))
    check("a Web UI button on the default port, and notes",
          new["portals"] == {"Web UI": "http://0.0.0.0:30800/"} and "setup wizard" in new["notes"])
    check("TrueNAS's own fields stay exactly as they were",
          m["name"] == "custom-app" and m["train"] == "stable" and m["version"] == "1.0.0"
          and m["title"] == "Curatarr" and new["custom_app"] is True
          and new["human_version"] == "1.0.0_custom" and new["version"] == "1.0.0"
          and new["action_required"] is False and new["migrated"] is False)
    check("the old file is kept as a .bak copy",
          any(p.read_text(encoding="utf-8") == REAL for p in app_dir.glob("metadata.yaml.bak-*")))
    g = yaml.safe_load((root / "metadata.yaml").read_text(encoding="utf-8"))
    check("the app's entry in the global file is filled in too, the other apps untouched",
          g["curatarr"]["metadata"]["icon"] == m["icon"] and g["other"] == {"custom_app": False})

    first = f.read_text(encoding="utf-8")
    run(root)
    check("running it again changes nothing", f.read_text(encoding="utf-8") == first)

    rc, _ = run(root, "--port", "30900")
    check("another port moves the button",
          yaml.safe_load(f.read_text(encoding="utf-8"))["portals"]["Web UI"] == "http://0.0.0.0:30900/")

    err = io.StringIO()
    old_err, sys.stderr = sys.stderr, err
    try:
        rc, _ = run(root, "notthere")
    finally:
        sys.stderr = old_err
    check("an unknown app name is a clear error, nothing written", rc == 1 and "not found" in err.getvalue())

compose = yaml.safe_load((ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8"))
host_port = int(compose["services"]["curatarr"]["ports"][0].split(":")[0])
check("the compose file's Web UI portal matches its published port, as does the script's default",
      compose["x-portals"][0]["port"] == host_port == 30800
      and "default=30800" in (ROOT / "deploy/truenas/apply_metadata.py").read_text(encoding="utf-8"))
guide = (ROOT / "deploy/truenas/README.md").read_text(encoding="utf-8")
check("the guide reads the script before running it, and dry-runs first",
      guide.index("less apply_metadata.py") < guide.index("--dry-run") < guide.index("sudo python3 apply_metadata.py  "))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
