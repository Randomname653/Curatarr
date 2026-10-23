"""Is there a newer Curatarr than this one? (2026-10-23)

The sidebar shows the running build and, for admins, "Update available"
when upstream has published something newer:

- **container:** the newest image is what the last successful run of the
  docker workflow on main pushed as ``:latest``. Compared with the commit
  baked into this image.
- **git checkout (desktop):** the newest commit on main, compared with HEAD.

"Newer" comes from GitHub's compare (the published commit is AHEAD of this
one), not from inequality: a checkout running ahead of main (a dev branch)
is not told to update. Unauthenticated GitHub API, at most every CHECK_EVERY_S
(the anonymous limit is 60 requests an hour per address), the answer kept in
app_state so a restart does not ask again. UPDATE_CHECK=false turns it off.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

REPO = "Randomname653/Curatarr"
API = "https://api.github.com"
STATE_KEY = "update_check"
CHECK_EVERY_S = 6 * 3600
RETRY_AFTER_ERROR_S = 3600
HOW = {
    "container": "Pull the new image, then redeploy: docker pull "
                 "ghcr.io/randomname653/curatarr:latest (TrueNAS shell) and edit + save the app, "
                 "or docker compose pull && docker compose up -d. With pull_policy: always "
                 "in the app's YAML, edit + save alone does both.",
    "git": "In the tray menu: Update now (a fast-forward to main, then a restart). "
           "Or git pull in the Curatarr folder and restart.",
}

_running = {"task": None}


def _settings():
    from src.config import settings
    return settings


def _parse(raw) -> dict:
    try:
        out = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def load_state() -> dict:
    from src.services.app_state import get_state
    return _parse(get_state(STATE_KEY))


async def aload_state() -> dict:
    from src.services.app_state import aget_state
    return _parse(await aget_state(STATE_KEY))


def save_state(state: dict) -> None:
    from src.services.app_state import set_state
    set_state(STATE_KEY, json.dumps(state))


def is_stale(state: dict, now: Optional[float] = None) -> bool:
    now = time.time() if now is None else now
    age = now - float(state.get("checked_at") or 0)
    return age >= (RETRY_AFTER_ERROR_S if state.get("error") else CHECK_EVERY_S)


def summarize(state: dict, build: dict) -> dict:
    """What the API tells the sidebar: only an answer about THIS build counts."""
    if not state or state.get("for_sha") != build.get("sha"):
        return {"update_available": None}
    out = {"update_available": state.get("update_available"),
           "latest": state.get("latest_sha"), "latest_short": (state.get("latest_sha") or "")[:7] or None,
           "latest_at": state.get("latest_at"), "behind_by": state.get("behind_by"),
           "checked_at": state.get("checked_at")}
    if out["update_available"]:
        out["how"] = HOW.get(build.get("channel"), "")
    return out


async def _get(client, path: str) -> dict:
    r = await client.get(f"{API}{path}", headers={"Accept": "application/vnd.github+json",
                                                  "User-Agent": "Curatarr-update-check"})
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    return r.json()


async def latest_published(client, channel: str) -> tuple:
    """(sha, when) of what upstream offers this kind of install."""
    if channel == "container":
        data = await _get(client, f"/repos/{REPO}/actions/workflows/docker.yml/runs"
                                  f"?branch=main&status=success&event=push&per_page=1")
        runs = data.get("workflow_runs") or []
        return (runs[0].get("head_sha"), runs[0].get("updated_at")) if runs else (None, None)
    data = await _get(client, f"/repos/{REPO}/commits/main")
    return data.get("sha"), ((data.get("commit") or {}).get("committer") or {}).get("date")


async def refresh(build: dict, client=None) -> dict:
    """Ask GitHub once and store the answer. Never raises."""
    state = {"checked_at": time.time(), "for_sha": build.get("sha")}
    if not build.get("sha") or build.get("channel") not in HOW:
        state["error"] = "this build has no commit to compare"
        return state
    try:
        own = client is None
        if own:
            import httpx
            from src.services.http_client import tls_context
            client = httpx.AsyncClient(verify=tls_context(), timeout=10)
        try:
            latest, when = await latest_published(client, build["channel"])
            state.update(latest_sha=latest, latest_at=when)
            if not latest:
                state["error"] = "nothing published yet"
            elif latest == build["sha"]:
                state.update(update_available=False, behind_by=0)
            else:
                cmp = await _get(client, f"/repos/{REPO}/compare/{build['sha']}...{latest}")
                # "ahead": the published commit is ahead of ours. "diverged"
                # (a local branch with its own commits) also lacks upstream's.
                status = cmp.get("status")
                state.update(update_available=status in ("ahead", "diverged"),
                             behind_by=int(cmp.get("ahead_by") or 0))
                if not cmp:
                    state.update(update_available=None, error="this build is unknown upstream")
        finally:
            if own:
                await client.aclose()
    except Exception as e:  # noqa: BLE001 - a failed check only delays the hint
        state["error"] = f"{type(e).__name__}: {e}"[:200]
        logger.info("[update] check failed: %s", state["error"])
    return state


async def refresh_and_save(build: dict) -> dict:
    from src.database.runner import run_write
    state = await refresh(build)
    await run_write(save_state, state)
    if state.get("update_available"):
        logger.info("[update] a newer Curatarr is published: %s (%s commits ahead)",
                    (state.get("latest_sha") or "")[:7], state.get("behind_by"))
    return state


def kick(build: dict, state: dict) -> None:
    """Start one background refresh when the stored answer is stale."""
    if not getattr(_settings(), "UPDATE_CHECK", True) or not is_stale(state):
        return
    t = _running["task"]
    if t is not None and not t.done():
        return
    from src.services.bg_tasks import track_task
    _running["task"] = track_task(refresh_and_save(build), name="update_check")
