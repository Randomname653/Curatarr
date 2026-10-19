"""Model lifecycle for the bench: pull ahead, bake, clean up behind.

* ``Store.pull`` downloads through the bench server's /api/pull.
* ``Store.prefetch`` pulls the NEXT candidate in a background thread while
  the current one is measured, so the queue never waits on a download.
* ``Store.cleanup`` removes a model once its cells are done — but only one
  the bench pulled itself. Models installed before the run (production,
  the owner's own) are never removed, and a tag that is installed is never
  re-pulled, because a pull replaces the installed weights (registry.py).
  What the bench pulled is persisted in ``.hwbench_pulled.json`` so a
  crashed run still cleans up on the next start.
* A disk guard keeps ``min_free_gb`` free beyond the model being pulled.
* ``bake``/``unbake`` build a temporary ``hwbench-<slug>`` model carrying
  the production CURATOR_SYSTEM_PROMPT (imported, not copied) — the
  monologue call sends no system message and relies on the baked persona,
  exactly like curatarr-curator in production.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
from pathlib import Path

import httpx

from . import registry

STATE = Path(__file__).resolve().parent.parent / ".hwbench_pulled.json"
PULL_ATTEMPTS = 3


def _norm(ref: str) -> str:
    return ref if ":" in ref else f"{ref}:latest"


def installed(endpoint: str) -> set[str]:
    r = httpx.get(f"{endpoint}/api/tags", timeout=20)
    r.raise_for_status()
    return {_norm(m["name"]) for m in r.json().get("models") or []}


class Store:
    def __init__(self, endpoint: str, min_free_gb: float = 15.0, log=print):
        self.endpoint = endpoint
        self.min_free_gb = min_free_gb
        self.log = log
        self.preinstalled = installed(endpoint)
        self.pulled: set[str] = set(json.loads(STATE.read_text())) if STATE.exists() else set()
        # A model the bench pulled in an earlier run counts as the bench's,
        # not as the owner's, even though it is installed right now.
        self.preinstalled -= self.pulled
        self._prefetch: dict[str, threading.Thread] = {}
        self._results: dict[str, bool] = {}
        self._lock = threading.Lock()

    def _save(self):
        STATE.write_text(json.dumps(sorted(self.pulled)))

    def is_installed(self, ref: str) -> bool:
        return _norm(ref) in installed(self.endpoint)

    def disk_ok(self, ref: str) -> tuple[bool, str]:
        remote = registry.remote_manifest(ref) or {}
        need = (remote.get("total_bytes") or 0) / 1e9
        free = shutil.disk_usage(registry.models_dir()).free / 1e9
        ok = free - need >= self.min_free_gb
        return ok, f"{free:.0f} GB free, {need:.1f} GB needed, keep {self.min_free_gb:.0f}"

    def pull(self, ref: str) -> bool:
        ref = _norm(ref)
        if self.is_installed(ref):
            return True
        ok, why = self.disk_ok(ref)
        if not ok:
            self.log(f"  disk guard: not pulling {ref} ({why})")
            return False
        # Retried: a connection reset (WinError 10054, 2026-10-19) killed a
        # 19 GB pull half-way; Ollama resumes a retried pull where it stopped.
        for attempt in range(1, PULL_ATTEMPTS + 1):
            self.log(f"  pulling {ref} …" + (f" (attempt {attempt})" if attempt > 1 else ""))
            ok, why = self._pull_once(ref)
            if ok:
                break
            self.log(f"  pull {ref}: {why}")
            if attempt < PULL_ATTEMPTS:
                time.sleep(15 * attempt)
        else:
            return False
        with self._lock:
            self.pulled.add(ref)
            self._save()
        return True

    def _pull_once(self, ref: str) -> tuple[bool, str]:
        try:
            with httpx.stream("POST", f"{self.endpoint}/api/pull",
                              json={"model": ref, "stream": True},
                              timeout=httpx.Timeout(30, read=None)) as r:
                if r.status_code != 200:
                    return False, f"HTTP {r.status_code}"
                for line in r.iter_lines():
                    if not line:
                        continue
                    d = json.loads(line)
                    if d.get("error"):
                        return False, d["error"]
                    if d.get("status") == "success":
                        return True, ""
            return False, "stream ended before success"
        except (httpx.HTTPError, ValueError) as e:
            return False, f"{type(e).__name__}: {e}"

    def prefetch(self, ref: str) -> None:
        """Start pulling ``ref`` in the background (no-op when installed)."""
        ref = _norm(ref)
        if ref in self._prefetch or self.is_installed(ref):
            return
        t = threading.Thread(target=lambda: self._results.__setitem__(ref, self.pull(ref)),
                             daemon=True, name=f"pull {ref}")
        self._prefetch[ref] = t
        t.start()

    def ready(self, ref: str) -> bool:
        """Wait for a running prefetch, else pull now."""
        ref = _norm(ref)
        t = self._prefetch.pop(ref, None)
        if t:
            t.join()
            if self._results.pop(ref, False):
                return True
        return self.pull(ref)

    def cleanup(self, ref: str) -> None:
        ref = _norm(ref)
        if ref in self.preinstalled or ref not in self.pulled:
            return
        try:
            httpx.request("DELETE", f"{self.endpoint}/api/delete",
                          json={"model": ref}, timeout=60)
            self.log(f"  removed {ref} (pulled by the bench)")
        except httpx.HTTPError as e:
            self.log(f"  remove {ref} failed: {e}")
            return
        with self._lock:
            self.pulled.discard(ref)
            self._save()

    def cleanup_leftovers(self, keep: set[str] = frozenset()) -> None:
        """Remove what an earlier, crashed run pulled and never removed."""
        for ref in sorted(self.pulled - {_norm(k) for k in keep}):
            self.cleanup(ref)


def bake_name(ref: str) -> str:
    return "hwbench-" + re.sub(r"[^a-z0-9]+", "-", ref.lower()).strip("-")


def bake(endpoint: str, ref: str, num_ctx: int) -> str:
    """A persona bake of ``ref`` for the monologue call (production has the
    persona in curatarr-curator / curatarr-pitcher the same way)."""
    from src.services.setup_wizard import CURATOR_SYSTEM_PROMPT
    name = bake_name(ref)
    with httpx.stream("POST", f"{endpoint}/api/create", json={
            "model": name, "from": ref, "system": CURATOR_SYSTEM_PROMPT,
            "parameters": {"temperature": 0.7, "num_ctx": num_ctx}},
            timeout=300) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if line and json.loads(line).get("error"):
                raise RuntimeError(f"bake {name}: {json.loads(line)['error']}")
    return name


def unbake(endpoint: str, ref: str) -> None:
    try:
        httpx.request("DELETE", f"{endpoint}/api/delete",
                      json={"model": bake_name(ref)}, timeout=60)
    except httpx.HTTPError:
        pass
