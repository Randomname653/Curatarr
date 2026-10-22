"""Which Ollama models this install needs, and which of them are missing.

One list for every launcher: the tray's preflight, ``build_models.py
--check`` and start.bat through it. The embedding model is the one the
runtime ACTUALLY uses — the stored profile's — not ``settings.EMBEDDING_MODEL``,
the legacy v1 default: start.bat used to probe a hardcoded nomic-embed-text
and printed "Ollama models missing" on an install that runs v2-moe and never
had v1 (2026-09-25). An Ollama that does not answer is reported as exactly
that, not as every model missing.
"""
from __future__ import annotations

import subprocess
from typing import Callable, List, Optional


def expected_models() -> List[str]:
    """The models this install is configured to run on, in the order the
    launchers name them: curator, summarizer, embedding, pitcher when the
    two-bake split is on."""
    from src.config import settings
    try:
        from src.services.embed_service import effective_embedding_model
        emb = effective_embedding_model()
    except Exception:  # noqa: BLE001 - no profile yet (fresh install, tests)
        emb = settings.EMBEDDING_MODEL
    models = [settings.CURATOR_MODEL, settings.SUMMARIZER_MODEL, emb]
    if (settings.PITCHER_MODEL or "").strip():
        models.append(settings.PITCHER_MODEL.strip())
    return list(dict.fromkeys(m.strip() for m in models if m and m.strip()))


def ollama_answers(run: Callable = subprocess.run, **kw) -> bool:
    try:
        return run(["ollama", "list"], capture_output=True, timeout=20, **kw).returncode == 0
    except Exception:  # noqa: BLE001 - no binary, no daemon, timeout
        return False


def missing_models(run: Callable = subprocess.run, models: Optional[List[str]] = None,
                   **kw) -> Optional[List[str]]:
    """Names ``ollama show`` does not know. ``None`` when Ollama itself does
    not answer — nothing can be said about the models then, and a launcher
    must not start pulling on that basis."""
    if not ollama_answers(run, **kw):
        return None
    missing = []
    for model in (models if models is not None else expected_models()):
        try:
            if run(["ollama", "show", model], capture_output=True, timeout=20, **kw).returncode != 0:
                missing.append(model)
        except Exception:  # noqa: BLE001
            missing.append(model)
    return missing


# ── Will the curator fit the card? ──────────────────────────────────────────
# Calibrated on the one measurement on record (llm_utils, 2026-07-08): 19.9 GB
# of curator weights at a 16384-token context came to ~23.4 GB of VRAM. The
# context's share is scaled from that — a KV cache grows with the model's
# layers and heads, which track its size well enough for a warning. An
# estimate: it exists to say "this cannot work" before the first OOM, not to
# promise that a borderline setup will.
_CTX_SHARE_AT_16K = 0.15
_RUNTIME_OVERHEAD_GB = 0.5


def vram_needed_gb(weights_gb: float, num_ctx: int) -> float:
    return weights_gb * (1 + _CTX_SHARE_AT_16K * num_ctx / 16384) + _RUNTIME_OVERHEAD_GB


def vram_fit_warning(model: str, weights_bytes: Optional[int], gpu_total_mb: Optional[float],
                     num_ctx: int) -> str:
    """A sentence when ``model`` at ``num_ctx`` will not fit the card, else ""."""
    if not weights_bytes or not gpu_total_mb:
        return ""
    weights_gb = weights_bytes / 1024 ** 3
    need = vram_needed_gb(weights_gb, num_ctx)
    have = gpu_total_mb / 1024
    # No margin: the calibration point itself (a 4090, 23.96 of 24 GB) works.
    if need <= have:
        return ""
    return (f"{model} needs about {need:.1f} GB of VRAM ({weights_gb:.1f} GB of weights "
            f"plus a {num_ctx}-token context); this GPU has {have:.1f} GB. Use a smaller "
            f"BASE_CURATOR_MODEL, set LLM_PROFILE=small (8k context), or set "
            f"CURATOR_NUM_GPU=-1 to let Ollama offload part of it to the CPU (slower).")


def curator_fit_warning() -> str:
    """vram_fit_warning for the configured curator, read from Ollama and
    nvidia-smi. "" when either cannot be read — no GPU reading, no warning."""
    from src.config import settings
    from src.services.llm_utils import CURATOR_NUM_CTX
    try:
        from src.services.process_monitor import _nvidia_smi
        smi = _nvidia_smi()
    except Exception:  # noqa: BLE001 - startup hint only: no GPU reading means no VRAM-fit warning to give
        smi = None
    if not smi:
        return ""
    try:
        import httpx
        r = httpx.get(f"{settings.effective_ollama}/api/tags", timeout=5)
        r.raise_for_status()
        sizes = {(m.get("name") or m.get("model") or ""): m.get("size")
                 for m in r.json().get("models") or []}
    except Exception:  # noqa: BLE001 - startup hint only: Ollama unreadable means no model size to compare
        return ""
    model = settings.CURATOR_MODEL
    size = sizes.get(model) or sizes.get(f"{model}:latest")
    return vram_fit_warning(model, size, smi[1], CURATOR_NUM_CTX)


# ── Per host: the NAS and the gaming PC (2026-10-22, NAS move phase 2) ──────
# A split install runs the same bake NAMES on two machines, each built from a
# base that fits its card (build_models.py --host main|pc). The checks go
# over HTTP: the container on the NAS has no ollama binary to ask.

def host_plan(which: str = "main") -> dict:
    """What host ``which`` ("main" = OLLAMA_ENDPOINT, "pc" = OLLAMA_PC_ENDPOINT)
    is meant to run: its url and curator window, the bases to bake from, and
    the model names the runtime will ask it for. On a split install the
    pitcher lives on the PC only (deletion runs wait for it) and only the
    main host embeds."""
    from src.config import settings
    from src.services import ollama_hosts
    try:
        from src.services.embed_service import effective_embedding_model
        emb = effective_embedding_model()
    except Exception:  # noqa: BLE001 - no profile yet (fresh install, tests)
        emb = settings.EMBEDDING_MODEL
    pitcher_on = bool((settings.PITCHER_MODEL or "").strip())
    split = ollama_hosts.split()
    if which == "pc":
        pc = ollama_hosts.pc_host()
        if pc is None:
            raise ValueError("no OLLAMA_PC_ENDPOINT configured")
        plan = {"url": pc.url, "num_ctx": pc.num_ctx,
                "curator": settings.PC_BASE_CURATOR_MODEL,
                "summarizer": settings.PC_BASE_SUMMARIZER_MODEL,
                "pitcher": settings.PC_BASE_PITCHER_MODEL if pitcher_on else None,
                "embedding": None}
    else:
        main = ollama_hosts.main_host()
        plan = {"url": main.url, "num_ctx": main.num_ctx,
                "curator": settings.BASE_CURATOR_MODEL,
                "summarizer": settings.BASE_SUMMARIZER_MODEL,
                "pitcher": settings.BASE_PITCHER_MODEL if (pitcher_on and not split) else None,
                "embedding": emb}
    bakes = [settings.CURATOR_MODEL, settings.SUMMARIZER_MODEL]
    if plan["pitcher"]:
        bakes.append(settings.PITCHER_MODEL.strip())
    if plan["embedding"]:
        bakes.append(plan["embedding"])
    plan["models"] = list(dict.fromkeys(m.strip() for m in bakes if m and m.strip()))
    return plan


def host_tags(url: str, get=None) -> Optional[dict]:
    """{model name: digest} on the Ollama at ``url`` ("x:latest" also as
    "x"); None when it does not answer."""
    import httpx
    get = get or httpx.get
    try:
        r = get(f"{url.rstrip('/')}/api/tags", timeout=5)
        r.raise_for_status()
        models = r.json().get("models") or []
    except Exception:  # noqa: BLE001 - "does not answer" is the answer
        return None
    out = {}
    for m in models:
        name = m.get("name") or m.get("model") or ""
        out[name] = str(m.get("digest") or "")
        if name.endswith(":latest"):
            out.setdefault(name[:-len(":latest")], out[name])
    return out


def missing_on(url: str, models: List[str], get=None) -> Optional[List[str]]:
    tags = host_tags(url, get)
    if tags is None:
        return None
    return [m for m in models if m not in tags and f"{m}:latest" not in tags]


def embedding_identity(get=None) -> dict:
    """The embedding model's weights on the main host against the ones the
    stored vectors were made with (the profile's ``digest``). Vectors from
    other weights do not compare, even under the same name - the check the
    NAS move needs before its first embedding.

    status: "match" | "mismatch" | "unrecorded" (no digest stored yet) |
    "missing" (not on the host) | "unreachable"."""
    from src.services.embed_service import effective_embedding_model, get_profile
    from src.services.ollama_hosts import main_host
    model = effective_embedding_model()
    recorded = str(get_profile().get("digest") or "")
    tags = host_tags(main_host().url, get)
    if tags is None:
        status, digest = "unreachable", None
    else:
        digest = tags.get(model) or tags.get(f"{model}:latest")
        if not digest:
            status = "missing"
        elif not recorded:
            status = "unrecorded"
        else:
            status = "match" if digest == recorded else "mismatch"
    return {"model": model, "recorded": recorded, "host": digest, "status": status}


def record_embedding_digest(get=None) -> dict:
    """embedding_identity, after storing the main host's digest when none is
    recorded yet - the install the vectors were made on. Run it on the old
    machine before a move (build_models.py --check does)."""
    ident = embedding_identity(get)
    if ident["status"] == "unrecorded":
        from src.services.embed_service import record_digest
        record_digest(ident["host"])
        ident = {**ident, "recorded": ident["host"], "status": "match"}
    return ident
