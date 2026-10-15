"""Model identity: what a tag points at, remotely and locally, without pulling.

The registry manifest names every layer by digest; the ``image.model`` layer
is the weights blob, and its digest is the identity the ledger uses. The
local side reads Ollama's own manifest files, which newer Ollama builds
store as a manifest LIST pointing at further manifests in the blob store —
``local_manifest`` follows those.

Measured 2026-10-15: the registry's gemma4:31b carries different weights
(plus a projector and an MTP draft layer) than the copy pulled in 2026-08.
Pulling a tag that is already installed would silently replace the
production model, so the runner refuses that without ``--allow-update``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

REGISTRY = "https://registry.ollama.ai"
_ACCEPT = "application/vnd.docker.distribution.manifest.v2+json"


def split_ref(ref: str) -> tuple[str, str, str]:
    """``gemma4:31b`` -> (namespace, name, tag); ``user/x`` keeps its user."""
    name, _, tag = ref.partition(":")
    ns, _, bare = name.rpartition("/")
    return ns or "library", bare, tag or "latest"


def library_url(ref: str) -> str:
    ns, name, tag = split_ref(ref)
    base = "https://ollama.com/" + (f"library/{name}" if ns == "library" else f"{ns}/{name}")
    return f"{base}:{tag}"


def _layers(manifest: dict) -> dict:
    """mediaType suffix -> (digest, size) for the layers we care about."""
    out = {}
    for layer in manifest.get("layers") or []:
        kind = (layer.get("mediaType") or "").rsplit(".", 1)[-1]
        out.setdefault(kind, (layer.get("digest", ""), int(layer.get("size") or 0)))
    return out


def summarize_manifest(manifest: dict) -> dict:
    layers = _layers(manifest)
    weights = layers.get("model", ("", 0))
    return {
        "weights_digest": weights[0],
        "weights_bytes": weights[1],
        "total_bytes": sum(int(l.get("size") or 0) for l in manifest.get("layers") or []),
        "has_projector": "projector" in layers,   # vision tower
        "has_draft": "draft" in layers,           # MTP / speculative draft
        "layer_kinds": sorted(layers),
    }


def remote_manifest(ref: str, timeout: float = 20.0) -> dict | None:
    """The registry's current manifest summary for ``ref``; None if unknown."""
    ns, name, tag = split_ref(ref)
    url = f"{REGISTRY}/v2/{ns}/{name}/manifests/{tag}"
    try:
        r = httpx.get(url, headers={"Accept": _ACCEPT}, timeout=timeout,
                      follow_redirects=True)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        return {**summarize_manifest(r.json()), "source_url": library_url(ref)}
    except ValueError:
        return None


def models_dir() -> Path:
    return Path(os.environ.get("OLLAMA_MODELS")
                or Path.home() / ".ollama" / "models")


def _blob(digest: str, root: Path) -> Path:
    return root / "blobs" / digest.replace(":", "-")


def local_manifest(ref: str, root: Path | None = None) -> dict | None:
    """Summary of the INSTALLED copy of ``ref``; None when not installed."""
    root = root or models_dir()
    ns, name, tag = split_ref(ref)
    path = root / "manifests" / "registry.ollama.ai" / ns / name / tag
    if not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # A manifest list keeps the weights in a nested manifest: follow every
    # nested manifest layer until one carries an image.model layer.
    seen = set()
    stack = [manifest]
    while stack:
        m = stack.pop()
        summary = summarize_manifest(m)
        if summary["weights_digest"]:
            return summary
        for layer in m.get("layers") or []:
            mt = layer.get("mediaType") or ""
            d = layer.get("digest") or ""
            if "manifest" in mt and d and d not in seen:
                seen.add(d)
                try:
                    stack.append(json.loads(_blob(d, root).read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
    return summarize_manifest(manifest)


def show(endpoint: str, ref: str) -> dict:
    """Architecture facts from /api/show: family, parameter count, quant,
    context length, MoE expert counts. Empty dict when unavailable."""
    try:
        r = httpx.post(f"{endpoint}/api/show", json={"model": ref}, timeout=30)
        r.raise_for_status()
        d = r.json()
    except (httpx.HTTPError, ValueError):
        return {}
    details = d.get("details") or {}
    info = d.get("model_info") or {}

    def pick(suffix):
        for k, v in info.items():
            if k.endswith(suffix):
                return v
        return None

    experts = pick(".expert_count")
    return {
        "family": details.get("family"),
        "parameter_size": details.get("parameter_size"),
        "parameter_count": info.get("general.parameter_count"),
        "quantization": details.get("quantization_level"),
        "context_max": pick(".context_length"),
        "block_count": pick(".block_count"),
        "architecture": "moe" if experts else "dense",
        "expert_count": experts,
        "experts_used": pick(".expert_used_count"),
        "capabilities": d.get("capabilities") or [],
    }
