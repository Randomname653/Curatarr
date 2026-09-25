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
