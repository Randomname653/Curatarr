"""The bench ledger: every model ever benched and every measurement taken.

One JSON file (``bench_ledger.json`` next to this package), committed with
``git add -f`` like the other bench deliverables. Three sections:

``models``  keyed ``<ref>@<weights digest prefix>``. A tag is not an
            identity: ``gemma4:31b`` on the registry changed its weights
            after the 2026-08 pull, so the same tag can be two models. The
            weights blob digest is the identity; ref, source, family,
            parameter count, quantisation, architecture and sizes ride along.
``runs``    one row per measurement: phase, the full settings, the
            environment (GPU, driver, Ollama version, RAM), the metrics,
            the artifact files, status. ``fingerprint`` hashes everything
            that decides the result; a second run with the same
            fingerprint is skipped (``has_run``).
``scores``  hand/Claude scores attached to a run afterwards (the 5-axis
            rubric over monologues and chat answers).

Writes are atomic (temp file + replace) and happen after every run, so a
crash loses at most the run in flight.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import BENCH_VERSION

LEDGER_PATH = Path(__file__).resolve().parent.parent / "bench_ledger.json"
SCHEMA = 1


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def fingerprint(model_key: str, phase: str, settings: dict,
                bench_version: str = BENCH_VERSION) -> str:
    """Stable hash of what decides a result. Environment is NOT in it: the
    same model and settings on a newer driver is the same question (the
    environment is recorded on the run, and ``--force`` re-measures)."""
    blob = json.dumps({"m": model_key, "p": phase, "s": settings,
                       "v": bench_version}, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def model_key(ref: str, weights_digest: str) -> str:
    d = (weights_digest or "").removeprefix("sha256:")
    return f"{ref}@{d[:12] or 'unknown'}"


class Ledger:
    def __init__(self, path: Path | str = LEDGER_PATH):
        self.path = Path(path)
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"schema": SCHEMA, "models": {}, "runs": [], "scores": []}
        if self.data.get("schema") != SCHEMA:
            raise SystemExit(f"{self.path}: schema {self.data.get('schema')} "
                             f"!= {SCHEMA} — migrate before benching")

    # ── persistence ──────────────────────────────────────────────────────
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".ledger-",
                                   suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, self.path)

    # ── models ───────────────────────────────────────────────────────────
    def upsert_model(self, ref: str, weights_digest: str, **info) -> str:
        key = model_key(ref, weights_digest)
        row = self.data["models"].setdefault(key, {
            "ref": ref, "weights_digest": weights_digest, "first_seen": _now()})
        # Facts only ever get filled in, never blanked by a later partial look.
        for k, v in info.items():
            if v not in (None, "", [], {}):
                row[k] = v
        row["last_seen"] = _now()
        return key

    def model(self, key: str) -> dict | None:
        return self.data["models"].get(key)

    def keys_for_ref(self, ref: str) -> list[str]:
        return [k for k, m in self.data["models"].items() if m["ref"] == ref]

    # ── runs ─────────────────────────────────────────────────────────────
    def has_run(self, fp: str) -> bool:
        return any(r["fingerprint"] == fp and r["status"] == "ok"
                   for r in self.data["runs"])

    def runs(self, model_key: str | None = None, phase: str | None = None,
             status: str | None = "ok") -> list[dict]:
        return [r for r in self.data["runs"]
                if (model_key is None or r["model_key"] == model_key)
                and (phase is None or r["phase"] == phase)
                and (status is None or r["status"] == status)]

    def add_run(self, model_key: str, phase: str, settings: dict, *,
                env: dict, metrics: dict, status: str = "ok",
                error: str = "", artifacts: list[str] | None = None,
                started: str | None = None) -> dict:
        fp = fingerprint(model_key, phase, settings)
        row = {
            "id": f"{fp}-{len(self.data['runs']) + 1}",
            "fingerprint": fp,
            "model_key": model_key,
            "phase": phase,
            "bench_version": BENCH_VERSION,
            "settings": settings,
            "env": env,
            "metrics": metrics,
            "status": status,
            "error": error,
            "artifacts": artifacts or [],
            "started": started or _now(),
            "finished": _now(),
        }
        self.data["runs"].append(row)
        self.save()
        return row

    # ── scores ───────────────────────────────────────────────────────────
    def add_score(self, run_id: str, *, scorer: str, rubric: str,
                  aggregate: dict, item_file: str = "") -> None:
        if not any(r["id"] == run_id for r in self.data["runs"]):
            raise KeyError(f"no run {run_id}")
        self.data["scores"] = [s for s in self.data["scores"]
                               if not (s["run_id"] == run_id and s["scorer"] == scorer)]
        self.data["scores"].append({
            "run_id": run_id, "scorer": scorer, "rubric": rubric,
            "aggregate": aggregate, "item_file": item_file, "at": _now()})
        self.save()

    def scores_for(self, run_id: str) -> list[dict]:
        return [s for s in self.data["scores"] if s["run_id"] == run_id]
