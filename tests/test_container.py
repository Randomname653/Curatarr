"""The container image (2026-10-22): Curatarr on a NAS.

Docker itself is not available where this battery runs; the docker workflow
builds the image and starts it. These checks pin what that build relies on:
the .env lives in the data volume, the install is the hash-checked lock, the
process is not root, and nothing secret or bulky is sent as build context.

    python tests/test_container.py
"""
from _harness import check  # noqa: I001 - first: temp stores

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from src.paths import env_file  # noqa: E402


# ── where the .env lives ────────────────────────────────────────────────────
root = Path("/app")
check("without CURATARR_ENV_FILE the .env sits next to the app (desktop installs unchanged)",
      env_file(root, {}) == root / ".env" and env_file(root, {"CURATARR_ENV_FILE": "  "}) == root / ".env")
check("CURATARR_ENV_FILE moves it (the container: into the data volume)",
      env_file(root, {"CURATARR_ENV_FILE": "/app/data/.env"}) == Path("/app/data/.env"))

# ── the Dockerfile ──────────────────────────────────────────────────────────
df = (ROOT / "Dockerfile").read_text(encoding="utf-8")
lines = [l.strip() for l in df.splitlines() if l.strip() and not l.strip().startswith("#")]
base = next(l for l in lines if l.startswith("FROM "))
check("the base image is pinned to a digest", re.search(r"@sha256:[0-9a-f]{64}$", base))
check("the install is the hash-checked lock",
      "pip install --require-hashes -r lock/requirements.txt" in df)
user = next((l for l in lines if l.startswith("USER ")), "")
check("the app does not run as root", user and user.split()[1].split(":")[0] not in ("0", "root"))
check("the .env path is inside the data volume",
      "CURATARR_ENV_FILE=/app/data/.env" in df and 'VOLUME ["/app/data"]' in df)
copied = " ".join(l for l in lines if l.startswith("COPY "))
check("everything the app reads from its root is in the image",
      all(p in copied for p in ("src/", "frontend/", "assets/", "requirements.txt", "lock/")))
# The start command: the last top-level CMD (HEALTHCHECK has a CMD of its
# own), with its continuation lines joined.
cmd = re.findall(r"^CMD (.*?)(?<!\\)$", re.sub(r"\\\n\s*", " ", df), re.M)[-1]
check("uvicorn starts the app, binding the container's interface",
      '"uvicorn"' in cmd and '"--host", "0.0.0.0"' in cmd)
check("no --reload in the container", "--reload" not in cmd)

# ── what is never sent as build context ─────────────────────────────────────
ignore = {l.strip() for l in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()}
check("data, the .env and the venv stay out of the build context",
      {".env", "data/", "venv/", ".git"} <= ignore)

# ── the compose file ────────────────────────────────────────────────────────
compose = (ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
check("compose pulls the published image (GHCR names are lower-case)",
      "image: ghcr.io/randomname653/curatarr:latest" in compose)
check("compose mounts one volume at /app/data", re.search(r"- [^:\n]+:/app/data\s*$", compose, re.M))

# ── the workflow that builds it ─────────────────────────────────────────────
wf = (ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")
check("the image is published from main only, after the smoke test",
      "github.ref == 'refs/heads/main'" in wf and wf.index("Smoke test") < wf.index("Publish main to GHCR"))


# Two tracks, run rather than read: the Build and Publish steps' scripts run
# under bash with a fake docker that logs its arguments, for a push to main
# and for a release tag, each step only when its `if` holds.
def _run_publish(ref: str) -> list:
    import shutil
    import subprocess
    import tempfile
    import types

    import yaml
    steps = yaml.safe_load(wf)["jobs"]["image"]["steps"]
    github = types.SimpleNamespace(ref=ref, event_name="push", ref_name=ref.rsplit("/", 1)[-1])
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "docker.log"
        fake = Path(tmp) / "docker"
        fake.write_text('#!/bin/sh\necho "$@" >> "$DOCKER_LOG"\n', encoding="utf-8")
        fake.chmod(0o755)
        for step in steps:
            name = step.get("name") or ""
            if not (name == "Build" or name.startswith("Publish")):
                continue
            cond = (step.get("if") or "True").replace("&&", " and ").replace("||", " or ")
            if not eval(cond, {"startsWith": lambda a, b: a.startswith(b), "github": github}):  # noqa: S307 - the workflow's own conditions
                continue
            env = {**os.environ, "PATH": tmp + os.pathsep + os.environ.get("PATH", ""), "DOCKER_LOG": str(log),
                   "GITHUB_REF": ref, "GITHUB_SHA": "a" * 40, "IMAGE": "ghcr.io/x/curatarr",
                   "GH_TOKEN": "t", "ACTOR": "a", "TAG": github.ref_name}
            subprocess.run([shutil.which("bash") or "bash", "-e", "-c", step["run"]], env=env, check=True,
                           capture_output=True, timeout=30)
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


main_log = _run_publish("refs/heads/main")
tag_log = _run_publish("refs/tags/v1.2.0")
pushed = lambda log: sorted(line.split()[-1].rsplit(":", 1)[-1] for line in log if line.startswith("push "))  # noqa: E731
check("a merge to main pushes :main and its sha tag, never :latest",
      pushed(main_log) == ["main", "sha-aaaaaaa"], main_log)
check("a release tag pushes :X.Y.Z and moves :latest", pushed(tag_log) == ["1.2.0", "latest"], tag_log)
check("each build is labelled with its track",
      any("CURATARR_BUILD_TRACK=main" in line for line in main_log)
      and any("CURATARR_BUILD_TRACK=release" in line for line in tag_log), (main_log[:1], tag_log[:1]))


def run_blocks(text):
    """The script of every ``run:`` step: its own line plus the lines indented
    deeper than the ``run:`` key."""
    out, cur, indent = [], None, 0
    for line in text.splitlines():
        stripped = line.lstrip()
        depth = len(line) - len(stripped)
        if cur is not None:
            if stripped and depth <= indent:
                out.append("\n".join(cur))
                cur = None
            else:
                cur.append(line)
                continue
        m = re.match(r"(\s*)(?:- )?run:(.*)$", line)
        if m:
            indent, cur = len(m.group(1)), [m.group(2)]
    if cur is not None:
        out.append("\n".join(cur))
    return out


scripts = run_blocks(wf)
check("no workflow expression is interpolated into a shell script (they go through env:)",
      scripts and not any("${{" in s for s in scripts))

check.done()
